"""The MCP gateway: one URL, many clients, real tokens, a local audit log.

Why it exists. `python -m src.api.mcp_server` is already a working MCP server —
add it to Claude Code, Codex, Cursor or Zed and they can drive Kollektiv. What
that server cannot do is serve *several* clients with *different* permissions
from one address, which is what a team (or one person with three editors) needs.
The gateway adds exactly that and nothing else:

```
POST {GATEWAY_MCP_PATH}     the real MCP endpoint, behind a token
GET  /toolkits              namespaced catalogue, for discovery and for humans
POST /call                  one tool call, policy-checked and audited
GET  /audit                 recent calls (the operator's own log)
GET  /health                liveness + what is configured (no auth)
```

Design rules, each one a consequence of something this project already believes:

* **Optional.** `GATEWAY_ENABLED=false` by default; the plain MCP server and the
  REST API stay first-class forever. A gateway that becomes mandatory is a
  product decision made by accident.
* **Per-client tokens.** Stored encrypted via :class:`TokenStore`, revocable,
  shown once. Never proxied to a vendor: a user's Claude Code login is theirs.
* **Policy per client.** ``read-only``, ``worker``, ``messenger``, ``dashboard``
  or ``admin``, plus per-tool globs and a ``confirm`` requirement for anything
  that writes.
* **Audit locally, without content.** Tool names, timings, argument *names*.
  Never the arguments themselves — those are the project's data.
* **No vendor seat games.** The gateway serves our tools to whatever client you
  already pay for; it does not resell, proxy or evade anyone's subscription.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, MutableMapping, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from config.settings import Settings, get_settings
from src import __version__
from src.connectors.base import ConnectorRegistry
from src.gateway.audit import GatewayAudit, timed
from src.gateway.auth import GatewayAuth
from src.gateway.policy import Policy, load_policy_file
from src.gateway.tools import Tool, build_catalogue, toolkit_view
from src.orchestrator.app import Orchestrator
from src.utils.errors import ConnectorError, KollektivError
from src.utils.logger import configure_logging, get_logger

LOGGER = get_logger(__name__)


# ----------------------------------------------------------------------
# Request models
# ----------------------------------------------------------------------
class ToolCall(BaseModel):
    """Body of ``POST /call``."""

    tool: str = Field(..., description="Namespaced tool name from GET /toolkits.")
    params: Dict[str, Any] = Field(default_factory=dict, description="Tool parameters.")
    confirm: bool = Field(False, description="Required by policy for tools that change data.")

    @field_validator("tool")
    @classmethod
    def _tool_name(cls, value: str) -> str:
        """Reject an obviously malformed tool name before the catalogue lookup."""
        clean = (value or "").strip()
        if not clean or "." not in clean:
            raise ValueError("tool must be namespaced, e.g. 'projects.list'")
        return clean


# ----------------------------------------------------------------------
# Auth plumbing
# ----------------------------------------------------------------------
def _bearer(request: Request) -> str:
    """Extract the token from ``Authorization`` or ``X-Kollektiv-Token``.

    Args:
        request: The incoming request.

    Returns:
        The raw token, or ``""`` when neither header carries one.
    """
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return (request.headers.get("x-kollektiv-token") or "").strip()


async def _run_tool(tool: Tool, params: Dict[str, Any]) -> Any:
    """Dispatch one tool call, translating errors into HTTP-friendly ones.

    Args:
        tool: The catalogue entry to run.
        params: Call parameters.

    Returns:
        The tool result.

    Raises:
        HTTPException: 400 for bad parameters or an unconfigured connector (the
            same mapping the REST API uses, so a client behaves identically
            against either surface), 404 for unknown objects, 502 for upstream
            failures.
    """
    try:
        return await tool.handler(params)
    except (ValueError, ConnectorError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown object: {exc}") from exc
    except KollektivError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


class _TokenProtectedMCP:
    """ASGI wrapper that puts a gateway token in front of the MCP endpoint.

    The MCP server itself knows nothing about our clients; this wrapper is the
    only thing that does, so `python -m src.api.mcp_server` keeps working as the
    unprotected, single-user entry point it always was.
    """

    def __init__(self, app: Any, auth: GatewayAuth, settings: Settings) -> None:
        """Wrap ``app``.

        Args:
            app: The MCP ASGI application.
            auth: The gateway auth facade.
            settings: Settings (for ``GATEWAY_REQUIRE_TOKENS``).
        """
        self.app = app
        self.auth = auth
        self.settings = settings

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        """Check the token, then hand the request to the MCP app.

        Args:
            scope: ASGI scope.
            receive: ASGI receive callable.
            send: ASGI send callable.
        """
        if scope.get("type") != "http":  # pragma: no cover - lifespan/websocket
            await self.app(scope, receive, send)
            return
        if not self.settings.GATEWAY_REQUIRE_TOKENS:
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        header = headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else headers.get("x-kollektiv-token", "")
        resolved = await self.auth.authenticate(token)
        if resolved is None:
            body = json.dumps({"error": "unauthorized", "detail": "a gateway token is required"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        scope.setdefault("state", {})["gateway_client"] = resolved["client"]
        await self.app(scope, receive, send)


def build_mcp_app(server: Any, settings: Settings) -> Any:
    """Return the MCP ASGI app, shaped to sit under ``GATEWAY_MCP_PATH``.

    The MCP SDK serves streamable HTTP on ``/mcp`` by default; mounting that app
    under ``GATEWAY_MCP_PATH`` would publish ``/mcp/mcp``. The inner route is
    therefore rewritten to ``/`` so the configured path *is* the MCP URL.

    DNS-rebinding protection is left to the operator: with
    ``GATEWAY_ALLOWED_HOSTS`` unset the SDK's host check is turned off (the
    bearer token is the gate, and a host allowlist cannot protect a header-less
    CLI client); set it and the check is enabled with exactly those hosts.

    Args:
        server: The server built by :func:`src.api.mcp_server.create_server`.
        settings: Gateway settings.

    Returns:
        An ASGI application ready to mount.
    """
    hosts = [item.strip() for item in (settings.GATEWAY_ALLOWED_HOSTS or "").split(",") if item.strip()]
    origins = [item.strip() for item in (settings.GATEWAY_ALLOWED_ORIGINS or "").split(",") if item.strip()]
    security: Any = None
    try:
        from mcp.server.transport_security import TransportSecuritySettings

        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=bool(hosts),
            allowed_hosts=hosts,
            allowed_origins=origins or hosts,
        )
    except ImportError:  # pragma: no cover - older MCP SDKs
        LOGGER.debug("MCP SDK has no transport_security module; using its defaults")
    kwargs: Dict[str, Any] = {
        "streamable_http_path": "/",
        "transport_security": security,
        "max_request_body_size": settings.GATEWAY_MAX_BODY_BYTES,
    }
    try:
        return server.streamable_http_app(**kwargs)
    except TypeError:  # pragma: no cover - SDK v1 has a zero-argument signature
        LOGGER.warning(
            "This MCP SDK does not accept a custom streamable-HTTP path; the endpoint will be "
            "mounted one level deeper (e.g. %s/mcp). Pin mcp>=2 for the tidy URL.",
            settings.GATEWAY_MCP_PATH,
        )
        return server.streamable_http_app()


# ----------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------
def create_gateway_app(
    settings: Optional[Settings] = None,
    orchestrator: Optional[Orchestrator] = None,
    *,
    registry: Optional[ConnectorRegistry] = None,
) -> FastAPI:
    """Build the gateway application.

    Args:
        settings: Optional settings override.
        orchestrator: Optional pre-built orchestrator (created and started on
            first use when omitted, so the app boots before credentials exist).
        registry: Optional connector registry.

    Returns:
        A FastAPI application with the REST surface, the mounts and the audit
        log. Starting it requires nothing but a database.
    """
    resolved = settings or get_settings()
    state: Dict[str, Any] = {"orchestrator": orchestrator, "mcp": None}
    auth = GatewayAuth(resolved)
    audit = GatewayAudit(resolved)

    async def get_orchestrator() -> Orchestrator:
        """Return the lazily started orchestrator singleton."""
        instance = state.get("orchestrator")
        if instance is None:
            instance = Orchestrator(resolved)
            await instance.start()
            state["orchestrator"] = instance
            LOGGER.info("Gateway started the orchestrator")
        return instance

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Start the orchestrator on demand and run the MCP app's lifespan."""
        mcp_app = state.get("mcp")
        if mcp_app is None:
            # Imported here so a deployment that never mounts MCP does not need
            # the SDK's HTTP stack at import time.
            from src.api.mcp_server import create_server

            server = create_server(state.get("orchestrator"), resolved)
            mcp_app = build_mcp_app(server, resolved)
            state["mcp"] = mcp_app
            app.mount(resolved.GATEWAY_MCP_PATH, _TokenProtectedMCP(mcp_app, auth, resolved))
            LOGGER.info("MCP endpoint mounted at %s", resolved.GATEWAY_MCP_PATH)
        async with mcp_app.router.lifespan_context(mcp_app):
            LOGGER.info("Kollektiv gateway ready (%s)", resolved.ENVIRONMENT)
            yield
        LOGGER.info("Kollektiv gateway stopping")

    app = FastAPI(
        title="Kollektiv gateway",
        version=__version__,
        description="One MCP endpoint, per-client tokens, per-client policy, a local audit log.",
        lifespan=lifespan,
    )
    app.state.gateway = state

    async def current_client(request: Request) -> Dict[str, Any]:
        """FastAPI dependency: resolve the calling client or refuse the request.

        Args:
            request: The incoming request.

        Returns:
            ``{client, role, policy}``.

        Raises:
            HTTPException: 401 when the feature is locked and no valid token was
                sent.
        """
        if not resolved.GATEWAY_REQUIRE_TOKENS:
            if resolved.ENVIRONMENT == "production":  # pragma: no cover - operator choice
                LOGGER.warning("GATEWAY_REQUIRE_TOKENS is off in production; every call is anonymous")
            return {"client": "anonymous", "role": "admin", "policy": Policy.preset("admin")}
        resolved_client = await auth.authenticate(_bearer(request))
        if resolved_client is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="a gateway token is required (Authorization: Bearer kgw_…)",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return resolved_client

    async def catalogue() -> Dict[str, Tool]:
        """Build the catalogue, starting the orchestrator on first use.

        Returns:
            The tool catalogue. Rebuilt per request so a connector added to the
            registry shows up without a restart.
        """
        instance = state.get("orchestrator")
        if instance is None:
            instance = await get_orchestrator()
        return build_catalogue(instance, registry, resolved)

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------
    @app.get("/health", tags=["system"])
    async def health() -> Dict[str, Any]:
        """Report liveness and what the gateway is configured to do (no auth)."""
        clients = await auth.clients()
        return {
            "status": "ok",
            "version": __version__,
            "environment": resolved.ENVIRONMENT,
            "require_tokens": bool(resolved.GATEWAY_REQUIRE_TOKENS),
            "mcp_path": resolved.GATEWAY_MCP_PATH,
            "clients": len(clients),
            "policy_file": resolved.GATEWAY_POLICY_PATH or None,
            "audit_retention": "local, unbounded until `kollektiv gateway audit --clear`",
        }

    @app.get("/toolkits", tags=["gateway"])
    async def toolkits(client: Dict[str, Any] = Depends(current_client)) -> Dict[str, Any]:
        """Return the namespaced tool catalogue this client may use."""
        tools = await catalogue()
        policy: Policy = client["policy"]
        allowed: List[Dict[str, Any]] = []
        need_confirm: List[Dict[str, Any]] = []
        blocked: List[Dict[str, Any]] = []
        for tool in tools.values():
            ok, reason = policy.decision(tool.name, dangerous=tool.dangerous)
            entry = {**tool.to_dict(), "reason": None if ok else reason}
            if ok:
                if policy.requires_confirmation(tool.name):
                    entry["confirm_required"] = True
                    need_confirm.append(entry)
                else:
                    allowed.append(entry)
            elif policy.decision(tool.name, dangerous=tool.dangerous, confirm=True)[0]:
                # Only the confirm rule stands in the way — that is a speed bump,
                # not a wall, so it is reported as usable-with-confirmation.
                entry["confirm_required"] = True
                need_confirm.append(entry)
            else:
                blocked.append(entry)
        usable = allowed + need_confirm
        grouped = toolkit_view({entry["tool"]: tools[entry["tool"]] for entry in usable}) if usable else []
        return {
            "client": client["client"],
            "role": client["role"],
            "policy": policy.to_dict(),
            "count": len(usable),
            "need_confirm": len(need_confirm),
            "blocked": len(blocked),
            "toolkits": grouped,
            "confirm_required": [entry["tool"] for entry in need_confirm],
            "blocked_tools": [{"tool": entry["tool"], "reason": entry["reason"]} for entry in blocked],
        }

    @app.post("/call", tags=["gateway"])
    async def call_tool(body: ToolCall, client: Dict[str, Any] = Depends(current_client)) -> Dict[str, Any]:
        """Run one tool call, subject to the client's policy, and audit it.

        Args:
            body: The tool call.
            client: Injected by :func:`current_client`.

        Returns:
            ``{client, tool, ok, ms, result}`` — or ``{…, denied: true}`` when
            policy refused the call (HTTP 403, with the rule that decided).
        """
        tools = await catalogue()
        tool = tools.get(body.tool)
        policy: Policy = client["policy"]
        if tool is None:
            await audit.record(client=client["client"], tool=body.tool, ok=False, detail="unknown tool")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"unknown tool {body.tool!r}; see GET /toolkits",
            )
        allowed, reason = policy.decision(body.tool, dangerous=tool.dangerous, confirm=body.confirm)
        if not allowed:
            await audit.record(
                client=client["client"],
                tool=body.tool,
                ok=False,
                denied=True,
                arg_names=list(body.params),
                detail=reason,
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=reason)

        with timed() as clock:
            try:
                result = await _run_tool(tool, body.params)
            except HTTPException as exc:
                await audit.record(
                    client=client["client"],
                    tool=body.tool,
                    ok=False,
                    milliseconds=clock.ms,
                    arg_names=list(body.params),
                    detail=str(exc.detail)[:400],
                )
                raise
            except Exception as exc:  # noqa: BLE001 - every failure is reported and logged
                LOGGER.error("Gateway tool %s failed: %s", body.tool, exc, exc_info=True)
                await audit.record(
                    client=client["client"],
                    tool=body.tool,
                    ok=False,
                    milliseconds=clock.ms,
                    arg_names=list(body.params),
                    detail=f"{type(exc).__name__}: {exc}"[:400],
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"{type(exc).__name__}: {exc}",
                ) from exc

        await audit.record(
            client=client["client"],
            tool=body.tool,
            ok=True,
            milliseconds=clock.ms,
            arg_names=list(body.params),
        )
        await auth.touch(client["client"])
        return {
            "client": client["client"],
            "tool": body.tool,
            "ok": True,
            "ms": clock.ms,
            "result": result,
        }

    @app.get("/audit", tags=["gateway"])
    async def read_audit(
        limit: int = Query(default=50, ge=1, le=1000),
        client_name: str = Query(default="", alias="client"),
        caller: Dict[str, Any] = Depends(current_client),
    ) -> Dict[str, Any]:
        """Return recent calls, and the caller's own summary.

        Args:
            limit: How many rows.
            client_name: Optional filter on the client column.
            caller: Injected by :func:`current_client`.

        Returns:
            ``{rows, stats}``. Every authenticated client may read its own rows;
            the ``admin`` and ``dashboard`` roles may read everyone's.
        """
        requested = client_name or caller["client"]
        if requested != caller["client"] and caller["role"] not in {"admin", "dashboard"}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role {caller['role']!r} may only read its own audit rows",
            )
        rows = await audit.recent(limit=limit, client=requested)
        return {"client": requested, "count": len(rows), "rows": rows, "stats": await audit.stats(requested)}

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover - safety net
        """Answer with something a client can log, never a stack trace."""
        LOGGER.error("Unhandled gateway error on %s: %s", request.url.path, exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": "internal error", "error_type": type(exc).__name__},
        )

    app.state.gateway_auth = auth
    app.state.gateway_audit = audit
    app.state.gateway_catalogue = catalogue
    _ = load_policy_file  # imported for the app's public surface (operators script it)
    return app


def main() -> int:  # pragma: no cover - process entry point
    """Run the gateway with uvicorn (``python -m src.gateway``).

    Returns:
        A process exit code.
    """
    import uvicorn

    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    LOGGER.info("Starting the Kollektiv gateway on %s:%s", settings.GATEWAY_HOST, settings.GATEWAY_PORT)
    uvicorn.run(
        create_gateway_app(settings),
        host=settings.GATEWAY_HOST,
        port=settings.GATEWAY_PORT,
        log_level=settings.LOG_LEVEL.lower(),
    )
    return 0


__all__ = ["ToolCall", "build_mcp_app", "create_gateway_app", "main"]
