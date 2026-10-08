"""Tests for the HTTP API and the MCP tool surface.

The API is exercised through ``httpx.ASGITransport`` (no sockets), with a stub
orchestrator so the tests stay hermetic. The MCP tests build the real server
and call the registered tools directly.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, cast

import httpx
import pytest
from starlette.requests import Request as StarletteRequest

from src.api.mcp_server import create_server
from src.api.routes import create_app
from src.orchestrator.brain import OrchestratorBrain
from src.orchestrator.collector import Collector
from src.orchestrator.planner import Planner
from src.storage.state_manager import StateManager
from src.utils.errors import ConfigurationError
from tests.conftest import FakeAgent, FakeAgentPool, FakeTeraBoxPool


class StubOrchestrator:
    """A stand-in with the same surface the routes depend on."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.pool = FakeTeraBoxPool()
        self.state = StateManager(self.pool, settings=settings, project_id="prj_api")
        self.brain = OrchestratorBrain(settings)
        self.planner = Planner(self.brain, settings=settings)
        self.agent_pool = FakeAgentPool(
            [FakeAgent("agent-1", ['```python path=src/app.py\nprint("ok")\n```'])]
        )
        self.collector = Collector(settings=settings, project_id="prj_api", write_files=True)
        self.projects: Dict[str, Dict[str, Any]] = {}
        self.sync_runs = 0
        self._counter = 0

    # -- projects ------------------------------------------------------
    async def create_project(self, name: str, description: str, n_agents: int = 3) -> Dict[str, Any]:
        """Plan a project and remember it."""
        if not description.strip():
            raise ValueError("description must not be empty")
        self._counter += 1
        project_id = f"prj_stub{self._counter}"
        plan = await self.planner.create_plan(description, n_agents)
        record = {
            "project_id": project_id,
            "name": name or plan["project_name"],
            "description": description,
            "n_agents": n_agents,
            "plan": plan,
            "status": "planned",
        }
        self.projects[project_id] = record
        return record

    async def get_project(self, project_id: str) -> Dict[str, Any] | None:
        """Look up a project."""
        return self.projects.get(project_id)

    async def list_projects(self) -> List[Dict[str, Any]]:
        """List the stub projects."""
        return [
            {"project_id": record["project_id"], "name": record["name"], "status": record["status"]}
            for record in self.projects.values()
        ]

    async def run_project(
        self, project_id: str, max_concurrency: int | None = None, *, allow_over_budget: bool = False
    ) -> Dict[str, Any]:
        """Pretend to run the project."""
        if project_id not in self.projects:
            raise KeyError(project_id)
        record = self.projects[project_id]
        if not self.agent_pool.is_configured():
            raise ConfigurationError("no agents")
        tasks = record["plan"]["tasks"]
        record["status"] = "completed"
        return {
            "project_id": project_id,
            "status": "completed",
            "tasks_dispatched": len(tasks),
            "completed": len(tasks),
            "failed": 0,
            "results": [],
            "artifact": {"file_count": 1, "total_bytes": 10, "conflicts": [], "missing_dependencies": []},
        }

    async def get_project_status(self, project_id: str) -> Dict[str, Any]:
        """Return the state document for a project."""
        if project_id not in self.projects:
            raise KeyError(project_id)
        state = await self.state.read_state(project_id)
        state["project_name"] = self.projects[project_id]["name"]
        state["queued_tasks"] = len(
            [task for task in state.get("tasks", []) if task.get("status") == "pending"]
        )
        return state

    async def replan_project(
        self, project_id: str, dispatch: bool = False, max_new_tasks: int = 3
    ) -> Dict[str, Any]:
        """Return a stub corrective plan."""
        if project_id not in self.projects:
            raise KeyError(project_id)
        record = self.projects[project_id]
        plan = dict(record["plan"])
        plan["revision"] = int(plan.get("revision", 1)) + 1
        corrective = {
            "id": f"r{plan['revision']}",
            "title": "Fix the failed task",
            "description": "Repair what failed",
            "dependencies": [],
            "priority": 1,
        }
        plan["tasks"] = list(plan.get("tasks", [])) + [corrective]
        record["plan"] = plan
        return {
            "project_id": project_id,
            "revision": plan["revision"],
            "new_tasks": [corrective],
            "plan": plan,
            "results": [],
        }

    async def get_project_files(self, project_id: str) -> List[Dict[str, Any]]:
        """Return the fake storage listing."""
        return await self.pool.list_project_files(project_id)

    async def upload_project_file(self, project_id: str, local_path: str) -> Dict[str, Any]:
        """Archive a file through the fake pool."""
        import os

        if not os.path.isfile(local_path):
            raise FileNotFoundError(local_path)
        return await self.pool.upload_file(local_path, f"/Kollektiv/{project_id}/uploads/x")

    # -- status --------------------------------------------------------
    async def get_agents_status(self, probe: bool = False) -> List[Dict[str, Any]]:
        """Return agent statuses."""
        return await self.agent_pool.get_pool_status(probe=probe)

    async def get_storage_status(self) -> Dict[str, Any]:
        """Return the fake quota."""
        return await self.pool.get_total_quota()

    async def trigger_sync(self) -> Dict[str, Any]:
        """Count sync runs."""
        self.sync_runs += 1
        return {"started_at": "now", "commits": 0, "errors": []}

    async def on_pr_merged(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Accept a merge notification."""
        return {"handled": "pr_merged"}

    async def health(self) -> Dict[str, Any]:
        """Report health."""
        return {
            "status": "ok",
            "app": "Kollektiv",
            "subsystems": {"agents": {"agents": 1}, "brain": self.brain.stats()},
            "warnings": [],
        }


@pytest.fixture()
def api_app(settings: Any) -> Any:
    """A FastAPI app wired to the stub orchestrator."""
    settings = settings.model_copy(update={"AUTO_INIT_DB": True}, deep=True)
    return create_app(settings=settings, orchestrator=cast(Any, StubOrchestrator(settings)))


@pytest.fixture()
async def api_client(api_app: Any) -> Any:
    """An httpx client bound to the ASGI app, with lifespan started."""
    transport = httpx.ASGITransport(app=api_app)
    async with api_app.router.lifespan_context(api_app), httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield client


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
async def test_health(api_client: Any) -> None:
    """``GET /health`` reports subsystem status."""
    response = await api_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["subsystems"]["agents"]["agents"] == 1


async def test_project_lifecycle(api_client: Any) -> None:
    """Create, list, run and inspect a project."""
    created = await api_client.post(
        "/projects",
        json={"name": "shortener", "description": "Build a URL shortener with FastAPI and tests", "n_agents": 3},
    )
    assert created.status_code == 201
    project = created.json()
    assert project["project_id"].startswith("prj_stub")
    assert len(project["plan"]["tasks"]) == 3

    listed = await api_client.get("/projects")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1

    run = await api_client.post(f"/projects/{project['project_id']}/run")
    assert run.status_code == 200
    assert run.json()["tasks_dispatched"] == 3

    status = await api_client.get(f"/projects/{project['project_id']}/status")
    assert status.status_code == 200
    assert "queued_tasks" in status.json()

    files = await api_client.get(f"/projects/{project['project_id']}/files")
    assert files.status_code == 200
    assert files.json()["count"] == 0


async def test_project_validation_errors(api_client: Any) -> None:
    """Empty descriptions and unknown projects produce clean 4xx answers."""
    bad = await api_client.post("/projects", json={"name": "x", "description": "   ", "n_agents": 1})
    assert bad.status_code == 400

    missing = await api_client.get("/projects/prj_missing/status")
    assert missing.status_code == 404

    missing_run = await api_client.post("/projects/prj_missing/run")
    assert missing_run.status_code == 404


async def test_agents_storage_and_sync_endpoints(api_client: Any) -> None:
    """Operational endpoints expose pool, quota and sync data."""
    agents = await api_client.get("/agents/status")
    assert agents.status_code == 200
    assert agents.json()["count"] == 1

    storage = await api_client.get("/storage/status")
    assert storage.status_code == 200
    assert storage.json()["total_gb"] == 10.0

    sync = await api_client.post("/sync")
    assert sync.status_code == 200
    assert sync.json()["started_at"] == "now"


async def test_upload_endpoint(api_client: Any, tmp_path: Any) -> None:
    """A local file can be archived into a project."""
    local = tmp_path / "artifact.txt"
    local.write_text("payload", encoding="utf-8")
    response = await api_client.post("/projects/prj_stub1/upload", json={"file_path": str(local)})
    assert response.status_code == 201
    assert response.json()["upload"]["size"] == 7

    missing = await api_client.post("/projects/prj_stub1/upload", json={"file_path": str(tmp_path / "nope.txt")})
    assert missing.status_code == 404


async def test_openapi_schema_documents_routes(api_app: Any) -> None:
    """The OpenAPI schema includes the documented endpoints."""
    schema = api_app.openapi()
    paths = set(schema["paths"])
    assert {"/health", "/projects", "/sync", "/agents/status", "/storage/status"} <= paths
    assert "/webhooks/github" in paths


# ----------------------------------------------------------------------
# MCP
# ----------------------------------------------------------------------
async def test_mcp_tools_are_registered(settings: Any) -> None:
    """Every documented tool is exposed by the MCP server."""
    server = create_server(settings=settings)
    tools = await server.list_tools()
    names = {tool.name for tool in tools}
    assert {
        "list_projects",
        "get_project_status",
        "create_project",
        "run_project",
        "list_files",
        "upload_file",
        "get_agent_pool_status",
        "get_storage_status",
        "trigger_sync",
    } <= names


def tool_payload(result: Any) -> Dict[str, Any]:
    """Extract the JSON body from an MCP tool result (SDK v1 and v2 shapes)."""
    import json

    content = getattr(result, "content", None)
    if content:
        for item in content:
            text = getattr(item, "text", None)
            if text:
                return json.loads(text)
    if isinstance(result, str):
        return json.loads(result)
    raise AssertionError(f"unexpected tool result: {result!r}")


async def test_mcp_tools_return_json(settings: Any) -> None:
    """Tool calls return JSON the calling model can parse."""
    stub = StubOrchestrator(settings)
    server = create_server(orchestrator=stub, settings=settings)  # type: ignore[arg-type]

    created = tool_payload(
        await server.call_tool("create_project", {"name": "demo", "description": "Build a demo", "n_agents": 2})
    )
    assert created["task_count"] == 2
    project_id = created["project_id"]

    status = tool_payload(await server.call_tool("get_project_status", {"project_id": project_id}))
    assert status["project_name"] in {"demo", "Build A Demo"}

    run = tool_payload(await server.call_tool("run_project", {"project_id": project_id}))
    assert run["status"] == "completed"

    agents = tool_payload(await server.call_tool("get_agent_pool_status", {}))
    assert agents["count"] == 1

    storage = tool_payload(await server.call_tool("get_storage_status", {}))
    assert storage["total_gb"] == 10.0

    projects = tool_payload(await server.call_tool("list_projects", {}))
    assert projects["count"] == 1

    unknown = tool_payload(await server.call_tool("get_project_status", {"project_id": "nope"}))
    assert "error" in unknown


async def test_replan_endpoint(api_client: Any) -> None:
    """POST /projects/{id}/replan returns the corrective plan."""
    created = await api_client.post(
        "/projects",
        json={"name": "demo", "description": "Build a demo service with tests", "n_agents": 2},
    )
    project_id = created.json()["project_id"]

    response = await api_client.post(f"/projects/{project_id}/replan", params={"dispatch": False})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project_id"] == project_id
    assert body["revision"] >= 2
    assert body["new_tasks"] and body["new_tasks"][0]["id"].startswith("r")
    assert body["results"] == []

    missing = await api_client.post("/projects/prj_missing/replan")
    assert missing.status_code == 404


# ----------------------------------------------------------------------
# Server-Sent Events
# ----------------------------------------------------------------------
async def test_project_event_stream_emits_state(api_client: Any, api_app: Any, settings: Any) -> None:
    """``GET /projects/{id}/events/stream`` frames state as Server-Sent Events.

    The endpoint is driven directly rather than through ``httpx.ASGITransport``:
    the transport buffers the whole body before returning, which never happens
    for a stream that stays open on purpose. Every other assertion still goes
    through the real route function and the real orchestrator call.
    """
    created = await api_client.post(
        "/projects",
        json={"name": "stream", "description": "Stream a demo project with tests", "n_agents": 2},
    )
    project_id = created.json()["project_id"]

    from src.api.routes import build_router

    route = next(
        route
        for route in build_router(settings, serve_dashboard=False).routes
        if getattr(route, "path", "") == "/projects/{project_id}/events/stream"
    )

    async def receive() -> Dict[str, Any]:
        """Only ever reached if ``is_disconnected()`` does not cancel the wait."""
        return {"type": "http.request", "body": b"", "more_body": False}

    request = StarletteRequest(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/projects/{project_id}/events/stream",
            "raw_path": f"/projects/{project_id}/events/stream".encode(),
            "query_string": b"",
            "headers": [],
            "client": ("test", 1),
            "server": ("test", 80),
        },
        receive,
    )
    endpoint = cast(Any, route).endpoint  # BaseRoute types this dynamically
    response = await endpoint(
        project_id=project_id, request=request, interval=0.5, orchestrator=api_app.state.orchestrator
    )

    # Proxies (nginx, Cloudflare) must not buffer the stream.
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.media_type == "text/event-stream"

    iterator = response.body_iterator
    try:
        first = await anext(iterator)
        assert first.startswith("event: state\ndata: ")
        payload = json.loads(first.split("data: ", 1)[1].strip())
        keys = {"status", "tasks", "files", "history", "last_commit", "queued_tasks"}
        assert set(payload) == keys
        # The frame is a faithful copy of what GET /projects/{id}/status returns.
        status = await api_app.state.orchestrator.get_project_status(project_id)
        assert payload == {key: status.get(key) for key in keys}

        # An unchanged project sends a heartbeat comment instead of the state.
        heartbeat = await anext(iterator)
        assert heartbeat.strip() == ": keep-alive"
    finally:
        await iterator.aclose()


async def test_project_event_stream_reports_missing_projects(api_client: Any) -> None:
    """An unknown project yields one ``event: error`` frame and then closes."""
    async with api_client.stream(
        "GET", "/projects/prj_missing/events/stream", params={"interval": 0.5}
    ) as response:
        assert response.status_code == 200
        frames = "".join([chunk async for chunk in response.aiter_text()])

    assert "event: error" in frames
    assert "prj_missing" in frames
