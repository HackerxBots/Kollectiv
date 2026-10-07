"""Tests for the validation that keeps identifiers inside the workspace.

Project ids, account ids and file paths arrive from HTTP paths, MCP tools and
the CLI, and some of them end up joined onto a workspace directory or a bucket
prefix. These tests pin the rules: a value that could escape its directory is
rejected with :class:`ValueError`, and the API answers ``400`` instead of
leaking an internal message or a stack trace.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import httpx
import pytest
from starlette.requests import Request as StarletteRequest

from config.settings import Settings
from src.api.routes import build_router, create_app
from src.storage.state_manager import StateManager
from src.utils.paths import safe_path_segment, safe_relative_path


class FakePool:
    """Minimal storage pool: records the remote paths it is asked about."""

    remote_root = "/Kollektiv"

    def __init__(self) -> None:
        self.requested: List[str] = []

    def is_configured(self) -> bool:
        """Report an unconfigured pool so nothing tries to reach the network."""
        return False

    async def get_file_url(self, remote: str, expires: Optional[int] = None) -> str:
        """Return a fake download URL for ``remote``."""
        self.requested.append(remote)
        return f"https://storage.invalid/{remote.lstrip('/')}"


class FakeOrchestrator:
    """The slice of the orchestrator the file-URL route needs."""

    def __init__(self) -> None:
        self.pool = FakePool()


# ----------------------------------------------------------------------
# The helpers
# ----------------------------------------------------------------------
@pytest.mark.parametrize("value", ["prj_2e848068a353", "global", "task-1.v2", "aB9_x-y.z", "  prj_padded  "])
def test_safe_path_segment_accepts_identifiers(value: str) -> None:
    """Real identifiers pass through unchanged (apart from surrounding space)."""
    assert safe_path_segment(value) == value.strip()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        ".",
        "..",
        "../etc/passwd",
        "../../etc/passwd",
        "a/b",
        "a\\b",
        "a\x00b",
        "a\nb",
        "/absolute",
        ".hidden",
        "name with spaces",
        "x" * 200,
    ],
)
def test_safe_path_segment_rejects_escapes_and_junk(value: str) -> None:
    """Anything that could climb out of a directory is refused."""
    with pytest.raises(ValueError):
        safe_path_segment(value, label="project id")


@pytest.mark.parametrize("value", ["src/app/main.py", "a", "docs/PLAN.md", "deep/nested/path/file.txt"])
def test_safe_relative_path_accepts_nested_files(value: str) -> None:
    """Nested repository paths are fine."""
    assert safe_relative_path(value) == value


@pytest.mark.parametrize(
    "value",
    ["../x", "/abs", "a//b", "a/../../b", "a/./b", "..", "", "a/..", "a\\..\\b", "a\x00/b"],
)
def test_safe_relative_path_rejects_traversal(value: str) -> None:
    """Traversal, absolute paths and empty segments are refused."""
    with pytest.raises(ValueError):
        safe_relative_path(value, label="file path")


# ----------------------------------------------------------------------
# The callers
# ----------------------------------------------------------------------
def test_state_manager_refuses_unsafe_project_ids(settings: Settings) -> None:
    """The state document never lands outside the workspace."""
    manager = StateManager(pool=FakePool(), settings=settings, project_id="prj_ok")
    assert manager.local_path().endswith("prj_ok-PROJECT_STATE.md")
    assert manager.remote_path() == "/Kollektiv/prj_ok/PROJECT_STATE.md"

    for bad in ("../../etc/passwd", "..", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            manager.local_path(bad)
        with pytest.raises(ValueError):
            manager.remote_path(bad)

    # "No project" (None or empty) means the global document, which is still
    # inside the workspace, so it stays allowed.
    assert manager.local_path(None).endswith("prj_ok-PROJECT_STATE.md")
    assert manager.local_path("").endswith("global-PROJECT_STATE.md")
    assert manager.remote_path("") == "/Kollektiv/PROJECT_STATE.md"


async def test_file_url_route_rejects_traversal(settings: Settings) -> None:
    """``GET /projects/{id}/files/{path}/url`` validates both path halves."""
    orchestrator = FakeOrchestrator()
    route = next(
        route
        for route in build_router(settings, serve_dashboard=False).routes
        if getattr(route, "path", "") == "/projects/{project_id}/files/{file_path:path}/url"
    )

    remote = await route.endpoint(
        project_id="prj_ok", file_path="src/app.py", expires=None, orchestrator=orchestrator
    )
    assert remote["path"] == "/Kollektiv/prj_ok/src/app.py"
    assert orchestrator.pool.requested == ["/Kollektiv/prj_ok/src/app.py"]

    with pytest.raises(ValueError):
        await route.endpoint(
            project_id="../../other", file_path="src/app.py", expires=None, orchestrator=orchestrator
        )
    with pytest.raises(ValueError):
        await route.endpoint(
            project_id="prj_ok", file_path="../../other/secret", expires=None, orchestrator=orchestrator
        )
    assert orchestrator.pool.requested == ["/Kollektiv/prj_ok/src/app.py"]


async def test_invalid_input_is_a_400_not_a_500(settings: Settings) -> None:
    """A bad identifier is a client error with a readable message."""
    app = create_app(settings, orchestrator=FakeOrchestrator())
    handler = app.exception_handlers[ValueError]

    request = StarletteRequest(
        {"type": "http", "method": "GET", "path": "/projects/..%2Fetc/status", "headers": [], "query_string": b""}
    )
    response = await handler(request, ValueError("project id contains characters that are not allowed in a path"))

    assert response.status_code == 400
    assert b"project id" in response.body


async def test_traversal_paths_never_crash_the_api(settings: Settings) -> None:
    """Encoded traversal in a URL is rejected (400/404), never a 500."""
    app = create_app(settings, orchestrator=FakeOrchestrator())
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        for path in (
            "/projects/..%2F..%2Fetc/status",
            "/projects/prj_ok/files/..%2F..%2Fetc%2Fpasswd/url",
            "/projects/%2E%2E/files/secret.txt/url",
        ):
            response = await client.get(path)
            assert response.status_code in (400, 404), (path, response.status_code, response.text)
            # No stack trace ever reaches the client, and a 400 explains the rule.
            assert "Traceback" not in response.text
            if response.status_code == 400:
                assert "not allowed in a path" in response.text or "traversal" in response.text


def test_workspace_paths_stay_inside_the_workspace(settings: Settings) -> None:
    """The end-to-end invariant: every state path is under the workspace root."""
    manager = StateManager(pool=FakePool(), settings=settings, project_id="prj_ok")
    workspace = Path(settings.workspace_path).resolve()
    candidate = Path(manager.local_path("prj_ok")).resolve()
    assert workspace in candidate.parents

    with pytest.raises(ValueError):
        Path(manager.local_path("../../outside")).resolve()


def test_helpers_are_used_by_the_storage_layer() -> None:
    """A regression guard: the callers import the helpers, not a local copy."""
    root = Path(__file__).resolve().parents[1] / "src"
    for relative, needle in (
        ("storage/state_manager.py", "safe_path_segment"),
        ("orchestrator/app.py", "safe_path_segment"),
        ("api/routes.py", "safe_relative_path"),
    ):
        assert needle in (root / relative).read_text(encoding="utf-8"), f"{relative} stopped validating paths"
