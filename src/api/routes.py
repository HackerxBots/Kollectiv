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
``GET  /storage/status``            TeraBox pool quota
``POST /sync``                      trigger the cron sync manually
``POST /webhooks/github``           GitHub webhook receiver

The app builds one :class:`~src.orchestrator.app.Orchestrator` during startup
and stores it on ``app.state.orchestrator`` (the webhook router and the tests
both rely on that).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config.settings import Settings, get_settings
from src import __version__
from src.api.auth import (
    auth_dependency,
    install_auth,
    parse_svix_headers,
    verify_svix_signature,
)
from src.github.webhook_handler import get_orchestrator as get_webhook_orchestrator
from src.github.webhook_handler import router as webhook_router
from src.github.webhook_handler import set_orchestrator as set_webhook_orchestrator
from src.orchestrator.app import Orchestrator
from src.utils.errors import ConfigurationError, KollektivError
from src.utils.logger import configure_logging, get_logger

LOGGER = get_logger(__name__)


# ----------------------------------------------------------------------
# Request/response models
# ----------------------------------------------------------------------
class ProjectCreateRequest(BaseModel):
    """Body for ``POST /projects``."""

    name: str = Field(default="", description="Project name")
    description: str = Field(..., description="What the project should do")
    n_agents: int = Field(default=3, ge=1, le=12, description="Number of worker agents")


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
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(webhook_router)
    application.include_router(build_router(resolved, serve_dashboard=dashboard_available))
    # Clerk: attaches identity to every request and, when AUTH_REQUIRED=true,
    # rejects anonymous calls (webhooks and /health stay public).
    application.state.clerk_verifier_handle = install_auth(application, resolved)
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
            LOGGER.error("Health check failed: %s", exc)
            return {"status": "degraded", "error": str(exc)}

    @router.post("/projects", response_model=ProjectCreateResponse, status_code=status.HTTP_201_CREATED, tags=["projects"])
    async def create_project(
        body: ProjectCreateRequest, orchestrator: Orchestrator = Depends(get_orchestrator)
    ) -> ProjectCreateResponse:
        """Create a project and generate its plan.

        Raises:
            HTTPException: 400 for an empty description; 502 when planning fails.
        """
        if not body.description.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="description must not be empty")
        try:
            record = await orchestrator.create_project(body.name, body.description, body.n_agents)
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
    async def list_projects(orchestrator: Orchestrator = Depends(get_orchestrator)) -> Dict[str, Any]:
        """List every project."""
        projects = await orchestrator.list_projects()
        return {"count": len(projects), "projects": projects}

    @router.post("/projects/{project_id}/run", response_model=RunResponse, tags=["projects"])
    async def run_project(
        project_id: str,
        background: bool = Query(default=False, description="Return immediately and run in the background"),
        max_concurrency: Optional[int] = Query(default=None, ge=1, le=32),
        orchestrator: Orchestrator = Depends(get_orchestrator),
    ) -> Any:
        """Dispatch the plan to the agent pool.

        With ``background=true`` the request returns as soon as the run is
        scheduled (useful for long projects); otherwise it waits for the
        results.
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

            asyncio.create_task(_run_safely(orchestrator, project_id, max_concurrency))
            return RunResponse(
                project_id=project_id,
                status="running",
                tasks_dispatched=len((project.get("plan") or {}).get("tasks") or []),
            )

        try:
            summary = await orchestrator.run_project(project_id, max_concurrency=max_concurrency)
        except ConfigurationError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except KollektivError as exc:
            LOGGER.error("Run failed for %s: %s", project_id, exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        return RunResponse(**summary)

    @router.post("/projects/{project_id}/replan", tags=["projects"])
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

    @router.get("/projects/{project_id}/status", tags=["projects"])
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

    @router.get("/projects/{project_id}/files", tags=["projects"])
    async def project_files(
        project_id: str, orchestrator: Orchestrator = Depends(get_orchestrator)
    ) -> Dict[str, Any]:
        """List every file stored for the project."""
        files = await orchestrator.get_project_files(project_id)
        return {"project_id": project_id, "count": len(files), "files": files}

    @router.post("/projects/{project_id}/upload", status_code=status.HTTP_201_CREATED, tags=["projects"])
    async def upload_project_file(
        project_id: str,
        body: UploadRequest,
        orchestrator: Orchestrator = Depends(get_orchestrator),
    ) -> Dict[str, Any]:
        """Archive a local file into the project's TeraBox folder."""
        try:
            result = await orchestrator.upload_project_file(project_id, body.file_path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - storage failures are 502s
            LOGGER.error("Upload failed for %s: %s", body.file_path, exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        return {"project_id": project_id, "upload": result}

    @router.get("/agents/status", tags=["agents"])
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

    @router.get("/storage/status", tags=["storage"])
    async def storage_status(orchestrator: Orchestrator = Depends(get_orchestrator)) -> Dict[str, Any]:
        """Return the TeraBox pool quota."""
        return await orchestrator.get_storage_status()

    @router.get("/auth/me", tags=["system"])
    async def whoami(user: Any = Depends(auth_dependency)) -> Dict[str, Any]:
        """Return the authenticated identity (or an anonymous marker)."""
        if user is None:
            return {"authenticated": False, "auth_required": request_auth_required()}
        return {"authenticated": True, "user": user.to_dict()}

    @router.get("/projects/{project_id}/files/{file_path:path}/url", tags=["storage"])
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
        remote = f"{orchestrator.pool.remote_root.rstrip('/')}/{project_id}/{file_path.lstrip('/')}"
        try:
            url = await orchestrator.pool.get_file_url(remote, expires=expires)
        except TypeError:
            url = await orchestrator.pool.get_file_url(remote)
        except Exception as exc:  # noqa: BLE001 - report storage failures as 404/502
            LOGGER.error("Could not build a URL for %s: %s", remote, exc)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
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

    @router.post("/sync", tags=["sync"])
    async def trigger_sync(orchestrator: Orchestrator = Depends(get_orchestrator)) -> Dict[str, Any]:
        """Trigger the cron sync manually."""
        try:
            return await orchestrator.trigger_sync()
        except Exception as exc:  # noqa: BLE001 - report rather than 500
            LOGGER.error("Manual sync failed: %s", exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return router


async def _run_safely(orchestrator: Orchestrator, project_id: str, max_concurrency: Optional[int]) -> None:
    """Run a project in the background without letting exceptions escape."""
    try:
        await orchestrator.run_project(project_id, max_concurrency=max_concurrency)
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
