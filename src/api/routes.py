"""FastAPI application: the HTTP surface of Kollektiv.

Run it with::

    uvicorn src.api.routes:app --port 8000

Endpoints
---------
``GET  /health``                    liveness + subsystem report
``POST /projects``                  create a project and plan it
``GET  /projects``                  list projects
``POST /projects/{id}/run``         dispatch the plan to the agents
``GET  /projects/{id}/status``      current ``PROJECT_STATE.md`` as JSON
``GET  /projects/{id}/files``       files stored for the project
``POST /projects/{id}/upload``      archive a local file to TeraBox
``GET  /agents/status``             worker pool status
``GET  /agents/health``             worker health panel (states, load share, attention)
``GET  /storage/status``            TeraBox pool quota
``POST /sync``                      trigger the cron sync manually
``POST /webhooks/github``           GitHub webhook receiver

The app builds one :class:`~src.orchestrator.app.Orchestrator` during startup
and stores it on ``app.state.orchestrator`` (the webhook router and the tests
both rely on that).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config.settings import Settings, get_settings
from src import __version__
from src.api.auth import (
    admin_dependency,
    auth_dependency,
    install_auth,
    install_ticket_redaction,
    parse_svix_headers,
    verify_svix_signature,
)
from src.github.webhook_handler import get_orchestrator as get_webhook_orchestrator
from src.github.webhook_handler import router as webhook_router
from src.github.webhook_handler import set_orchestrator as set_webhook_orchestrator
from src.orchestrator.app import Orchestrator
from src.utils.errors import BudgetError, ConfigurationError, ConnectorError, KollektivError
from src.utils.logger import configure_logging, get_logger
from src.utils.paths import safe_path_segment, safe_relative_path

LOGGER = get_logger(__name__)


# ----------------------------------------------------------------------
# Request/response models
# ----------------------------------------------------------------------
class ConnectorCall(BaseModel):
    """Body of ``POST /connectors/{name}/call``."""

    action: str = Field(..., description="Action name from GET /connectors.")
    params: Dict[str, Any] = Field(default_factory=dict, description="Action parameters.")
    confirm: bool = Field(False, description="Required for actions flagged dangerous.")
    agent_id: str = Field(
        "",
        description=(
            "Optional: the worker agent making the call. When a connector has links, "
            "only linked agents may call it. Operator surfaces (CLI, MCP, this API "
            "with the API token) leave it empty and are not constrained."
        ),
    )


class LinkCreate(BaseModel):
    """Body of ``POST /links`` — grant one agent one connector."""

    agent_id: str = Field(..., description="Account id from GET /agents/status.")
    connector: str = Field(..., description="Connector name from GET /connectors.")
    note: str = Field("", description="Optional human note.")
    created_by: str = Field("", description="Who granted it (audit trail).")


class ProjectCreateRequest(BaseModel):
    """Body for ``POST /projects``."""

    name: str = Field(default="", description="Project name")
    description: str = Field(..., description="What the project should do")
    n_agents: int = Field(
        default=3,
        ge=1,
        le=64,
        description=(
            "How many worker agents to plan for. There is no four-agent limit: the pool "
            "serves every account in ARENA_ACCOUNTS, and 64 is a sanity bound, not a cap "
            "on what the pool can hold."
        ),
    )


class ProjectCreateResponse(BaseModel):
    """Response for ``POST /projects``."""

    project_id: str
    name: str
    n_agents: int
    plan: Dict[str, Any]
    status: str


class RunResponse(BaseModel):
    """Response for ``POST /projects/{id}/run``."""

    project_id: str
    status: str
    tasks_dispatched: int
    completed: int = 0
    failed: int = 0
    artifact: Dict[str, Any] = Field(default_factory=dict)
    results: List[Dict[str, Any]] = Field(default_factory=list)


class UserKeyRequest(BaseModel):
    """Body for ``PUT /me/keys/{provider}``. The key is stored encrypted and never returned."""

    api_key: str


#: Token-store service name for keys a signed-in user stores on this server.
USER_KEY_SERVICE = "user-keys"


def _key_providers() -> List[str]:
    """Provider names that take an API key (not a session login)."""
    from src.agents.providers import PROVIDER_PRESETS

    return sorted(name for name, preset in PROVIDER_PRESETS.items() if preset.get("kind") == "key")


class UploadRequest(BaseModel):
    """Body for ``POST /projects/{id}/upload``."""

    file_path: str = Field(..., description="Local path of the file to archive")


# ----------------------------------------------------------------------
# Application factory
# ----------------------------------------------------------------------
def create_app(settings: Optional[Settings] = None, orchestrator: Optional[Orchestrator] = None) -> FastAPI:
    """Build the FastAPI application.

    Args:
        settings: Optional settings override (tests pass a stub).
        orchestrator: Optional pre-built orchestrator (tests inject fakes).

    Returns:
        A configured :class:`fastapi.FastAPI` instance.
    """
    resolved = settings or get_settings()
    if resolved.ENVIRONMENT == "production" and not resolved.SECRET_KEY:
        # Refuse to start rather than sign tokens and encrypt credentials with a public key.
        raise ConfigurationError("ENVIRONMENT=production requires SECRET_KEY to be set")
    if resolved.ENVIRONMENT == "production" and not resolved.AUTH_REQUIRED and not resolved.ALLOW_OPEN_API:
        # An open API on a server is every project, key and connector for anyone who can reach it.
        raise ConfigurationError(
            "ENVIRONMENT=production requires AUTH_REQUIRED=true (Clerk). For a single-user install "
            "on localhost only, set ALLOW_OPEN_API=true or use ENVIRONMENT=development."
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Start the orchestrator on boot and stop it on shutdown."""
        configure_logging(resolved.LOG_LEVEL)
        LOGGER.info("Starting the Kollektiv API (%s)", resolved.ENVIRONMENT)
        instance = orchestrator if orchestrator is not None else Orchestrator(resolved)
        app.state.orchestrator = instance
        set_webhook_orchestrator(instance)
        if orchestrator is None:
            try:
                report = await instance.start()
                LOGGER.info("Orchestrator ready: %s", report.get("warnings") or "no warnings")
            except Exception as exc:  # noqa: BLE001 - never block the API on startup
                LOGGER.error("Orchestrator failed to start: %s", exc, exc_info=True)
        yield
        LOGGER.info("Shutting down the Kollektiv API")
        if orchestrator is None:
            try:
                await instance.stop()
            except Exception as exc:  # noqa: BLE001 - shutdown is best effort
                LOGGER.debug("Shutdown raised: %s", exc)
        set_webhook_orchestrator(None)

    application = FastAPI(
        title="Kollektiv",
        description=(
            "Multi-agent collaborative dev team orchestrator: worker agents, Cloudflare R2/"
            "TeraBox storage, GitHub sync and an LLM planner. Optional free-tier extras: "
            "Neon (database), Clerk (auth), Resend (email)."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    dashboard = Path(__file__).resolve().parents[2] / "web"
    dashboard_available = (dashboard / "index.html").is_file()
    if dashboard_available:
        # The same static page that Cloudflare Pages/github Pages publish, so a
        # single-origin deployment needs no CORS configuration at all.
        application.mount("/ui", StaticFiles(directory=str(dashboard), html=True), name="dashboard")

    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origin_list,
        allow_credentials=resolved.cors_origin_list != ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    from src.api.ratelimit import WriteRateLimiter

    write_limiter = WriteRateLimiter(resolved.RATE_LIMIT_WRITES_PER_MINUTE)

    @application.middleware("http")
    async def _rate_limit_writes(request: Request, call_next: Any) -> Any:
        """Refuse write floods from one client address with 429."""
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            client = request.client.host if request.client else "unknown"
            if not write_limiter.allow(client):
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    content={"detail": "Too many requests. Wait a minute and try again."},
                    headers={"Retry-After": "60"},
                )
        return await call_next(request)

    @application.middleware("http")
    async def _security_headers(request: Request, call_next: Any) -> Any:
        """Send baseline security headers on every response."""
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        if resolved.ENVIRONMENT == "production":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    application.include_router(webhook_router)
    application.include_router(build_router(resolved, serve_dashboard=dashboard_available))
    # Clerk: attaches identity to every request and, when AUTH_REQUIRED=true,
    # rejects anonymous calls (webhooks and /health stay public).
    application.state.clerk_verifier_handle = install_auth(application, resolved)
    install_ticket_redaction()

    @application.exception_handler(ValueError)
    async def invalid_input_handler(request: Request, exc: ValueError) -> JSONResponse:
        """Answer invalid identifiers and paths with 400, never a stack trace.

        ``safe_path_segment``/``safe_relative_path`` raise :class:`ValueError`
        for project ids, account ids and file paths that could escape the
        workspace or a bucket prefix; connectors raise it for bad parameters.
        """
        LOGGER.warning("Rejected invalid input on %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(exc)},
        )

    return application


# ----------------------------------------------------------------------
# Dependencies
# ----------------------------------------------------------------------
def get_orchestrator(request: Request) -> Orchestrator:
    """FastAPI dependency returning the live orchestrator.

    Raises:
        HTTPException: 503 when the orchestrator is not available.
    """
    orchestrator = getattr(request.app.state, "orchestrator", None) or get_webhook_orchestrator(request)
    if orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orchestrator is not running",
        )
    return orchestrator


async def project_access(
    project_id: str,
    user: Any = Depends(auth_dependency),
    orchestrator: Orchestrator = Depends(get_orchestrator),
) -> None:
    """Let a project route run only for the project's owner.

    With no signed-in user (``AUTH_REQUIRED=false``, single-user local mode)
    every project is open. With a user, anyone who does not own the project gets
    404, so the response does not reveal that the project exists. A project with
    no owner (created before sign-in was on) is therefore visible to no one
    while a user is signed in.
    """
    if user is None:
        return
    found = await orchestrator.project_owner(project_id)
    if not found["exists"] or found["owner_id"] != user.subject:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
def build_router(settings: Optional[Settings] = None, serve_dashboard: bool = False) -> APIRouter:
    """Build the main API router (kept separate so tests can mount it alone).

    Args:
        settings: The settings the app was built with. Passing them explicitly
            keeps routes independent of the ambient ``.env`` (important when a
            process hosts more than one configuration, e.g. in tests).
        serve_dashboard: True when ``/ui`` is mounted, so ``/`` can point at it.
    """
    router = APIRouter()
    resolved = settings or get_settings()

    @router.get("/", include_in_schema=False)
    async def root() -> Any:
        """Send browsers to the dashboard, machines to the API description."""
        if serve_dashboard:
            return RedirectResponse(url="/ui/")
        return {
            "name": "Kollektiv",
            "version": __version__,
            "docs": "/docs",
            "health": "/health",
            "projects": "/projects",
        }

    def request_auth_required() -> bool:
        """Return ``AUTH_REQUIRED`` for this app instance."""
        return bool(resolved.AUTH_REQUIRED)

    @router.get("/sponsors/line", tags=["sponsors"])
    async def sponsor_line(response: Response) -> Dict[str, Any]:
        """Return the one sponsor line for this moment, or ``{"line": null}``.

        Takes no input, sets no cookie and stores nothing about the caller.
        """
        from src.sponsors.line import current_line

        response.headers["Cache-Control"] = "no-store"
        return {"line": current_line(resolved)}

    @router.get("/health", tags=["system"])
    async def health(request: Request) -> Dict[str, Any]:
        """Return liveness plus a per-subsystem health report."""
        orchestrator = getattr(request.app.state, "orchestrator", None) or get_webhook_orchestrator(request)
        if orchestrator is None:
            return {
                "status": "degraded",
                "detail": "orchestrator not initialised",
                "warnings": get_settings().config_warnings(),
            }
        try:
            return await orchestrator.health()
        except Exception as exc:  # noqa: BLE001 - health must answer something
            LOGGER.error("Health check failed: %s", exc, exc_info=True)
            return {
                "status": "degraded",
                "error": "the health check raised; see the server logs",
                "error_type": type(exc).__name__,
            }

    @router.post("/projects", response_model=ProjectCreateResponse, status_code=status.HTTP_201_CREATED, tags=["projects"])
    async def create_project(
        body: ProjectCreateRequest,
        orchestrator: Orchestrator = Depends(get_orchestrator),
        user: Any = Depends(auth_dependency),
    ) -> ProjectCreateResponse:
        """Create a project and generate its plan.

        Raises:
            HTTPException: 400 for an empty description; 502 when planning fails.
        """
        if not body.description.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="description must not be empty")
        try:
            record = await orchestrator.create_project(
                body.name, body.description, body.n_agents, owner_id=user.subject if user else None
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except KollektivError as exc:
            LOGGER.error("Planning failed: %s", exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        return ProjectCreateResponse(
            project_id=record["project_id"],
            name=record["name"],
            n_agents=record["n_agents"],
            plan=record["plan"],
            status=record["status"],
        )

    @router.get("/projects", tags=["projects"])
    async def list_projects(
        orchestrator: Orchestrator = Depends(get_orchestrator),
        user: Any = Depends(auth_dependency),
    ) -> Dict[str, Any]:
        """List the projects the caller owns (every project in single-user mode)."""
        projects = await orchestrator.list_projects(owner_id=user.subject if user else None)
        return {"count": len(projects), "projects": projects}

    @router.post("/projects/{project_id}/run", response_model=RunResponse, tags=["projects"], dependencies=[Depends(project_access)])
    async def run_project(
        project_id: str,
        background: bool = Query(default=False, description="Return immediately and run in the background"),
        max_concurrency: Optional[int] = Query(default=None, ge=1, le=32),
        allow_over_budget: bool = Query(
            default=False, description="Run even when the estimate exceeds a configured cost cap"
        ),
        orchestrator: Orchestrator = Depends(get_orchestrator),
    ) -> Any:
        """Dispatch the plan to the agent pool.

        With ``background=true`` the request returns as soon as the run is
        scheduled (useful for long projects); otherwise it waits for the
        results. The run is estimated first (``GET /projects/{id}/estimate``)
        and refused with ``402`` when it would exceed a configured cap, unless
        ``allow_over_budget=true``.
        """
        try:
            project = await orchestrator.get_project(project_id)
        except Exception as exc:  # noqa: BLE001 - surface a clean error
            LOGGER.error("Could not load project %s: %s", project_id, exc)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown project: {project_id}")

        if background:
            import asyncio

            asyncio.create_task(
                _run_safely(orchestrator, project_id, max_concurrency, allow_over_budget=allow_over_budget)
            )
            return RunResponse(
                project_id=project_id,
                status="running",
                tasks_dispatched=len((project.get("plan") or {}).get("tasks") or []),
            )

        try:
            summary = await orchestrator.run_project(
                project_id, max_concurrency=max_concurrency, allow_over_budget=allow_over_budget
            )
        except BudgetError as exc:
            # 402 Payment Required is the honest code: the work is fine, the
            # budget is the problem, and the body carries the numbers.
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=exc.message,
                headers={"X-Kollektiv-Budget": "exceeded"},
            ) from exc
        except ConfigurationError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except KollektivError as exc:
            LOGGER.error("Run failed for %s: %s", project_id, exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        return RunResponse(**summary)

    @router.get("/projects/{project_id}/estimate", tags=["projects"], dependencies=[Depends(project_access)])
    async def estimate_project(
        project_id: str,
        n_agents: Optional[int] = Query(default=None, ge=1, le=64, description="Estimate a different agent count"),
        orchestrator: Orchestrator = Depends(get_orchestrator),
    ) -> Dict[str, Any]:
        """Estimate what running this project will cost, before running it.

        The number is arithmetic on the plan (task count and description length),
        the configured prices and what the project has already spent — never a
        prediction dressed up as a fact: ``estimate_only`` is in the response.
        """
        try:
            estimate = await orchestrator.estimate_project_cost(project_id, n_agents=n_agents)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return estimate.to_dict()

    @router.get("/budget", tags=["system"], dependencies=[Depends(admin_dependency)])
    async def budget_report(orchestrator: Orchestrator = Depends(get_orchestrator)) -> Dict[str, Any]:
        """Return the local spend ledger, today's position and the caps in force."""
        return await orchestrator.budget_report()

    @router.post("/projects/{project_id}/replan", tags=["projects"], dependencies=[Depends(project_access)])
    async def replan_project(
        project_id: str,
        dispatch: bool = Query(default=False, description="Run the corrective tasks immediately"),
        max_new_tasks: int = Query(default=3, ge=1, le=10),
        orchestrator: Orchestrator = Depends(get_orchestrator),
    ) -> Dict[str, Any]:
        """Replace failed tasks with corrective ones (optionally run them).

        Returns the new plan revision, the corrective tasks and, when
        ``dispatch=true``, their results.
        """
        try:
            return await orchestrator.replan_project(
                project_id, dispatch=dispatch, max_new_tasks=max_new_tasks
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ConfigurationError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except KollektivError as exc:
            LOGGER.error("Replan failed for %s: %s", project_id, exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/status", tags=["projects"], dependencies=[Depends(project_access)])
    async def project_status(
        project_id: str, orchestrator: Orchestrator = Depends(get_orchestrator)
    ) -> Dict[str, Any]:
        """Return the project's ``PROJECT_STATE.md`` as JSON."""
        try:
            return await orchestrator.get_project_status(project_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown project: {project_id}"
            ) from exc
        except KollektivError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    @router.get("/projects/{project_id}/events/stream", tags=["projects"], dependencies=[Depends(project_access)])
    async def project_event_stream(
        project_id: str,
        request: Request,
        interval: float = Query(3.0, ge=0.5, le=30.0, description="Poll interval in seconds"),
        orchestrator: Orchestrator = Depends(get_orchestrator),
    ) -> StreamingResponse:
        """Stream a project's state as Server-Sent Events (live dashboards).

        Emits ``event: state`` whenever the project's tasks, files, status or
        history change, and a comment heartbeat otherwise, so a browser can show
        progress without polling. The stream ends when the client disconnects or
        the project disappears.
        """

        async def generator() -> AsyncIterator[str]:
            last_digest = ""
            while True:
                if await request.is_disconnected():
                    LOGGER.debug("SSE client disconnected from %s", project_id)
                    return
                try:
                    status = await orchestrator.get_project_status(project_id)
                except KeyError as exc:
                    yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
                    return
                except Exception as exc:  # noqa: BLE001 - report, then stop cleanly
                    LOGGER.error("SSE stream for %s failed: %s", project_id, exc, exc_info=True)
                    failure = {
                        "error": "internal error while reading the project state",
                        "error_type": type(exc).__name__,
                    }
                    yield f"event: error\ndata: {json.dumps(failure)}\n\n"
                    return
                payload = {key: status.get(key) for key in ("status", "tasks", "files", "history", "last_commit", "queued_tasks")}
                digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
                if digest != last_digest:
                    last_digest = digest
                    yield f"event: state\ndata: {json.dumps(payload, default=str)}\n\n"
                else:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(interval)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    @router.get("/projects/{project_id}/handoff", tags=["projects"], dependencies=[Depends(project_access)])
    async def project_handoff(
        project_id: str,
        markdown: bool = Query(False, description="Return the rendered briefing instead of JSON"),
        orchestrator: Orchestrator = Depends(get_orchestrator),
    ) -> Any:
        """Resume briefing for a project: next actions, blockers, recent history.

        Generated from the shared state document, so a new session (Arena chat,
        teammate, CI job) continues where the previous one stopped.
        """
        try:
            handoff = await orchestrator.get_handoff(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        if markdown:
            return Response(content=handoff["markdown"], media_type="text/markdown")
        return {key: value for key, value in handoff.items() if key != "markdown"}

    @router.get("/projects/{project_id}/files", tags=["projects"], dependencies=[Depends(project_access)])
    async def project_files(
        project_id: str, orchestrator: Orchestrator = Depends(get_orchestrator)
    ) -> Dict[str, Any]:
        """List every file stored for the project."""
        files = await orchestrator.get_project_files(project_id)
        return {"project_id": project_id, "count": len(files), "files": files}

    @router.post("/projects/{project_id}/upload", status_code=status.HTTP_201_CREATED, tags=["projects"], dependencies=[Depends(project_access)])
    async def upload_project_file(
        project_id: str,
        body: UploadRequest,
        orchestrator: Orchestrator = Depends(get_orchestrator),
    ) -> Dict[str, Any]:
        """Archive a local file into the project's TeraBox folder."""
        try:
            result = await orchestrator.upload_project_file(project_id, body.file_path)
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - storage failures are 502s
            LOGGER.error("Upload failed for %s: %s", body.file_path, exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        return {"project_id": project_id, "upload": result}

    @router.get("/agents/status", tags=["agents"], dependencies=[Depends(admin_dependency)])
    async def agents_status(
        probe: bool = Query(default=False, description="Perform a liveness probe per agent"),
        orchestrator: Orchestrator = Depends(get_orchestrator),
    ) -> Dict[str, Any]:
        """Return the worker agent pool status."""
        agents = await orchestrator.get_agents_status(probe=probe)
        return {
            "count": len(agents),
            "available": len([agent for agent in agents if agent.get("status") == "idle"]),
            "agents": agents,
        }

    @router.get("/agents/health", tags=["agents"], dependencies=[Depends(admin_dependency)])
    async def agents_health(orchestrator: Orchestrator = Depends(get_orchestrator)) -> Dict[str, Any]:
        """Return the health panel: each worker's state, cooldown, session expiry and load share.

        Nothing here is a key or a token, and no call is made to a provider.
        """
        return await orchestrator.get_agents_health()

    @router.get("/storage/status", tags=["storage"], dependencies=[Depends(admin_dependency)])
    async def storage_status(orchestrator: Orchestrator = Depends(get_orchestrator)) -> Dict[str, Any]:
        """Return the TeraBox pool quota."""
        return await orchestrator.get_storage_status()

    @router.get("/me/keys", tags=["account"])
    async def list_my_keys(user: Any = Depends(auth_dependency)) -> Dict[str, Any]:
        """Which providers you have a key stored for. Keys are never returned."""
        from src.utils.token_store import TokenStore

        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to manage your keys.")
        store = TokenStore()
        return {
            "providers": [
                {"provider": name, "stored": store.has_token(USER_KEY_SERVICE, f"{user.subject}:{name}")}
                for name in _key_providers()
            ]
        }

    @router.put("/me/keys/{provider}", tags=["account"])
    async def store_my_key(
        provider: str, body: UserKeyRequest, user: Any = Depends(auth_dependency)
    ) -> Dict[str, Any]:
        """Store your API key for a provider, encrypted. It is never shown again."""
        from src.utils.token_store import TokenStore

        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to store a key.")
        if provider not in _key_providers():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown key provider: {provider}")
        key = body.api_key.strip()
        if len(key) < 8 or len(key) > 512 or any(char.isspace() for char in key):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That does not look like an API key.")
        TokenStore().save_token(USER_KEY_SERVICE, f"{user.subject}:{provider}", {"api_key": key})
        return {"provider": provider, "stored": True}

    @router.delete("/me/keys/{provider}", tags=["account"])
    async def delete_my_key(provider: str, user: Any = Depends(auth_dependency)) -> Dict[str, Any]:
        """Remove your stored key for a provider."""
        from src.utils.token_store import TokenStore

        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to manage your keys.")
        removed = TokenStore().delete_token(USER_KEY_SERVICE, f"{user.subject}:{provider}")
        return {"provider": provider, "deleted": bool(removed)}

    @router.get("/connectors", tags=["connectors"])
    async def list_connectors(orchestrator: Orchestrator = Depends(get_orchestrator)) -> Dict[str, Any]:
        """List every connector, its status and the actions it exposes."""
        registry = getattr(orchestrator, "connectors", None)
        if registry is None:
            return {"count": 0, "configured": [], "actions": [], "connectors": []}
        links = await orchestrator.links_by_connector()
        connectors = []
        for status_entry in registry.statuses():
            entry = dict(status_entry)
            entry["linked_agents"] = links.get(str(entry.get("name")), [])
            connectors.append(entry)
        return {
            "count": len(registry.names),
            "configured": registry.configured_names(),
            "connectors": connectors,
            # The actions stay here on purpose: linking and calling are different
            # jobs, and a connector's actions belong to the connector, not to the
            # grant that lets an agent reach it.
            "actions": registry.catalog(),
        }

    @router.get("/links", tags=["links"])
    async def list_links(orchestrator: Orchestrator = Depends(get_orchestrator)) -> Dict[str, Any]:
        """List agent-connector grants.

        Linking is *not* calling: ``GET /connectors`` holds the actions, this
        holds who is allowed to use them. A connector with no links is open.
        """
        links = await orchestrator.list_links()
        return {"count": len(links), "links": links}

    @router.post("/links", status_code=status.HTTP_201_CREATED, tags=["links"], dependencies=[Depends(admin_dependency)])
    async def create_link(
        body: LinkCreate, orchestrator: Orchestrator = Depends(get_orchestrator)
    ) -> Dict[str, Any]:
        """Grant an agent access to a connector.

        Raises:
            HTTPException: 404 for an unknown agent or connector, 409 when the
                pair is already linked.
        """
        try:
            return await orchestrator.link_agent(
                body.agent_id, body.connector, note=body.note, created_by=body.created_by
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @router.delete("/links/{link_id}", tags=["links"], dependencies=[Depends(admin_dependency)])
    async def delete_link(
        link_id: str, orchestrator: Orchestrator = Depends(get_orchestrator)
    ) -> Dict[str, Any]:
        """Revoke one link.

        Raises:
            HTTPException: 404 when the link does not exist.
        """
        try:
            return await orchestrator.unlink_agent(link_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @router.post("/connectors/{name}/call", tags=["connectors"], dependencies=[Depends(admin_dependency)])
    async def call_connector(
        name: str, payload: ConnectorCall, orchestrator: Orchestrator = Depends(get_orchestrator)
    ) -> Dict[str, Any]:
        """Run one connector action (``confirm=true`` for dangerous actions)."""
        registry = getattr(orchestrator, "connectors", None)
        if registry is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The connector registry is not available yet.",
            )
        allowed, reason = await orchestrator.access_decision(name, payload.agent_id)
        if not allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=reason)
        try:
            result = await registry.call(name, payload.action, payload.params, confirm=payload.confirm)
        except ConnectorError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surface upstream failures as 502
            LOGGER.error("Connector %s.%s failed: %s", name, payload.action, exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        return {"connector": name, "action": payload.action, "result": result}

    @router.post("/connectors/{name}/probe", tags=["connectors"], dependencies=[Depends(admin_dependency)])
    async def probe_connector(name: str, orchestrator: Orchestrator = Depends(get_orchestrator)) -> Dict[str, Any]:
        """Run the connector's read-only probe and report reachability + latency."""
        registry = getattr(orchestrator, "connectors", None)
        if registry is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The connector registry is not available yet.",
            )
        try:
            report = await registry.probe(name)
        except ConnectorError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return report

    @router.post("/auth/stream-ticket", tags=["system"])
    async def issue_stream_ticket(request: Request, user: Any = Depends(auth_dependency)) -> Dict[str, Any]:
        """Issue a short-lived ticket for opening a project event stream from a browser.

        ``EventSource`` cannot send the Authorization header, so the dashboard calls this
        with its Bearer token and passes the ticket as ``?ticket=`` on the stream URL.
        """
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to open a live stream.")
        store = request.app.state.stream_tickets
        return {"ticket": store.issue(user.subject), "expires_in": int(store.ttl)}

    @router.get("/auth/me", tags=["system"])
    async def whoami(user: Any = Depends(auth_dependency)) -> Dict[str, Any]:
        """Return the authenticated identity (or an anonymous marker)."""
        if user is None:
            return {"authenticated": False, "auth_required": request_auth_required()}
        return {"authenticated": True, "user": user.to_dict()}

    @router.get("/projects/{project_id}/files/{file_path:path}/url", tags=["storage"], dependencies=[Depends(project_access)])
    async def file_url(
        project_id: str,
        file_path: str,
        expires: Optional[int] = Query(default=None, ge=60, le=604800),
        orchestrator: Orchestrator = Depends(get_orchestrator),
    ) -> Dict[str, Any]:
        """Return a time-limited download URL for a stored project file.

        On R2 this is a presigned S3 URL (no credentials leak); on TeraBox it is
        the API's own download URL.
        """
        project_segment = safe_path_segment(project_id, label="project id")
        file_segment = safe_relative_path(file_path, label="file path")
        remote = f"{orchestrator.pool.remote_root.rstrip('/')}/{project_segment}/{file_segment}"
        try:
            url = await orchestrator.pool.get_file_url(remote, expires=expires)
        except TypeError:
            url = await orchestrator.pool.get_file_url(remote)
        except Exception as exc:  # noqa: BLE001 - report storage failures as 404/502
            LOGGER.error("Could not build a URL for %s: %s", remote, exc, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="the file is not available in shared storage",
            ) from exc
        return {"project_id": project_id, "path": remote, "url": url, "expires_in": expires}

    @router.post("/webhooks/clerk", tags=["webhooks"])
    async def clerk_webhook(request: Request) -> Dict[str, Any]:
        """Receive Clerk webhooks (``user.created``, ``session.created``, ...).

        The payload is verified with the Svix scheme before it is parsed. Clerk
        is Kollektiv's identity provider, so this endpoint only records the
        event — it never mutates project data.
        """
        raw = await request.body()
        settings_used: Settings = request.app.state.settings_used_for_auth
        secret = settings_used.CLERK_WEBHOOK_SECRET
        if not secret:
            LOGGER.warning("Received a Clerk webhook but CLERK_WEBHOOK_SECRET is unset; ignoring it.")
            return {"received": True, "verified": False, "reason": "webhook secret not configured"}
        headers = parse_svix_headers(request.scope.get("headers") or [])
        if not verify_svix_signature(raw, headers, secret):
            LOGGER.warning("Rejected a Clerk webhook with an invalid signature")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON: {exc}") from exc
        event_type = str(payload.get("type") or "")
        data = payload.get("data") or {}
        LOGGER.info(
            "Clerk webhook %s for %s",
            event_type or "(unknown)",
            data.get("email_addresses", [{}])[0].get("email_address", data.get("id", "?")),
        )
        return {"received": True, "verified": True, "type": event_type}

    @router.post("/sync", tags=["sync"], dependencies=[Depends(admin_dependency)])
    async def trigger_sync(orchestrator: Orchestrator = Depends(get_orchestrator)) -> Dict[str, Any]:
        """Trigger the cron sync manually."""
        try:
            return await orchestrator.trigger_sync()
        except Exception as exc:  # noqa: BLE001 - report rather than 500
            LOGGER.error("Manual sync failed: %s", exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return router


async def _run_safely(
    orchestrator: Orchestrator, project_id: str, max_concurrency: Optional[int], *, allow_over_budget: bool = False
) -> None:
    """Run a project in the background without letting exceptions escape."""
    try:
        await orchestrator.run_project(
            project_id, max_concurrency=max_concurrency, allow_over_budget=allow_over_budget
        )
    except Exception as exc:  # noqa: BLE001 - background tasks must not raise
        LOGGER.error("Background run of %s failed: %s", project_id, exc, exc_info=True)


app = create_app()


__all__ = ["app", "create_app", "build_router", "get_orchestrator", "main"]


def main() -> int:
    """Run the API with uvicorn (``kollektiv-api`` console script).

    Returns:
        A process exit code.
    """
    import uvicorn

    resolved = get_settings()
    configure_logging(resolved.LOG_LEVEL)
    uvicorn.run(
        "src.api.routes:app",
        host=resolved.API_HOST,
        port=resolved.API_PORT,
        log_level=resolved.LOG_LEVEL.lower(),
        proxy_headers=True,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
