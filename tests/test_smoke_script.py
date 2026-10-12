#!/usr/bin/env python3
"""Tests for the deployment smoke test (``scripts/smoke.py``).

The smoke test is what a person runs right after deploying, so it has to be
right in both directions: it must find real problems (a missing dashboard, a
planner that answers without tasks) and it must not cry wolf on a healthy
deployment. Both directions are covered here against a stub API and against the
real app, hermetically, with no network.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("kollectiv_smoke", ROOT / "scripts" / "smoke.py")
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def _stub(handler: Any, base_url: str = "http://stub") -> httpx.AsyncClient:
    """Build an httpx client with a mock transport."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base_url, timeout=5)


HEALTHY: Dict[str, Any] = {
    "/health": {"status": "ok", "warnings": ["SECRET_KEY is unset"]},
    "/ui/": {"__text__": "<html><script type=\"module\" src=\"assets/app.js\"></script></html>"},
    "/projects": {"count": 0, "projects": []},
    "/agents/status": {"agents": []},
    "/storage/status": {"accounts": 0, "total_gb": 0},
    "/connectors": {"count": 4, "connectors": []},
}


def healthy_handler(request: httpx.Request) -> httpx.Response:
    """Answer like a freshly deployed, unconfigured but working Kollektiv."""
    path = request.url.path
    # Method first, then exact paths: "/agents/status" also ends with "/status"
    # and POST /projects shares a path with GET /projects -- getting this order
    # wrong is exactly how a stub lies to its own test.
    if path == "/projects" and request.method == "POST":
        return httpx.Response(201, json={"project_id": "prj_smoke", "plan": {"tasks": [{"id": "t1"}, {"id": "t2"}]}})
    if path in HEALTHY:
        payload = HEALTHY[path]
        if "__text__" in payload:
            return httpx.Response(200, text=payload["__text__"])
        return httpx.Response(200, json=payload)
    if path.endswith("/status"):
        return httpx.Response(200, json={"tasks": [{"id": "t1", "status": "pending"}]})
    if path.endswith("/handoff"):
        return httpx.Response(200, json={"progress": {"total": 2, "completed": 0}, "markdown": "# Handoff"})
    if path.endswith("/events/stream"):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'event: state\ndata: {"status": "planned"}\n\n',
        )
    return httpx.Response(404, json={"detail": "not found"})


async def test_healthy_deployment_passes(capsys: pytest.CaptureFixture[str]) -> None:
    """A working deployment passes every check and exits 0."""
    smoke_client = _stub(healthy_handler)
    test = smoke.Smoke("http://stub", None, as_json=False)
    async with smoke_client as client:
        await smoke.check_health(client, test)
        await smoke.check_dashboard(client, test)
        await smoke.check_read_endpoints(client, test)
        await smoke.check_planner(client, test)
        await smoke.check_project_state(client, test)
        await smoke.check_event_stream(client, test)
    assert test.finished_ok(), test.failed
    assert test.project_id == "prj_smoke"
    assert test.finish() == 0
    output = capsys.readouterr().out
    assert "10/10 checks passed" in output


async def test_missing_dashboard_is_reported() -> None:
    """An install without web/ (the old packaging bug) fails the run."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ui/":
            return httpx.Response(404, text="Not Found")
        return healthy_handler(request)

    async with _stub(handler) as client:
        test = smoke.Smoke("http://stub", None, as_json=True)
        ok = await smoke.check_dashboard(client, test)
    assert ok is False
    assert test.failed == ["GET /ui/ (dashboard)"]


async def test_planner_without_tasks_is_reported() -> None:
    """A 201 with an empty plan is a failure, not a pass."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/projects" and request.method == "POST":
            return httpx.Response(201, json={"project_id": "prj_empty", "plan": {"tasks": []}})
        return healthy_handler(request)

    async with _stub(handler) as client:
        test = smoke.Smoke("http://stub", None, as_json=True)
        ok = await smoke.check_planner(client, test)
    assert ok is False
    assert "no task" in test.results[-1]["detail"] or test.results[-1]["detail"].startswith("0 task")


async def test_read_endpoint_with_wrong_shape_is_reported() -> None:
    """The dashboard's contracts are checked, not just the status codes."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/connectors":
            return httpx.Response(200, json={"unexpected": True})
        return healthy_handler(request)

    async with _stub(handler) as client:
        test = smoke.Smoke("http://stub", None, as_json=True)
        ok = await smoke.check_read_endpoints(client, test)
    assert ok is False
    assert any("connectors" in name for name in test.failed)


async def test_unreachable_api_exits_two() -> None:
    """A dead deployment is exit code 2, distinct from a failing check."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _stub(handler) as client:
        test = smoke.Smoke("http://stub", None, as_json=False)
        with pytest.raises(httpx.HTTPError):
            await smoke.check_health(client, test)


async def test_token_is_sent_when_provided() -> None:
    """A deployment behind Clerk gets the bearer token on every request."""
    seen: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return healthy_handler(request)

    async with _stub(handler) as client:
        client.headers.update(smoke._headers("test-token"))
        test = smoke.Smoke("http://stub", "test-token", as_json=True)
        await smoke.check_health(client, test)
    assert seen == ["Bearer test-token"]


async def test_json_output_is_machine_readable(capsys: pytest.CaptureFixture[str]) -> None:
    """``--json`` prints one document a CI job can parse."""
    async with _stub(healthy_handler) as client:
        test = smoke.Smoke("http://stub", None, as_json=True)
        await smoke.check_health(client, test)
        code = test.finish()
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["checks"][0]["check"] == "GET /health"
    assert set(payload) == {"base_url", "project_id", "checks"}


def test_argument_parsing_defaults() -> None:
    """The CLI defaults to localhost and accepts a token from the environment."""
    parser_defaults = smoke.main
    assert callable(parser_defaults)
    args = argparse.Namespace(base_url="http://example.test", token="t", json=True)
    assert args.base_url == "http://example.test"


def test_module_documents_its_exit_codes() -> None:
    """The docstring is the contract a supervisor reads; keep it accurate."""
    docstring = smoke.__doc__ or ""
    for fragment in ("Exit codes", "scripts/smoke.py", "--base-url"):
        assert fragment in docstring, fragment
