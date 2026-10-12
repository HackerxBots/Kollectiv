#!/usr/bin/env python3
"""Post-deployment smoke test for a running Kollektiv.

Run it against any deployment — localhost, a VM, a container, whichever host you
chose — and it tells you both that the process is up *and* that the parts that
usually break after an install actually work: the database, the planner, the
static dashboard, the MCP-facing API surface and the Server-Sent Events stream.

```bash
python scripts/smoke.py                                  # http://localhost:8000
python scripts/smoke.py --base-url https://kollektiv.example.com --token "$CLERK_JWT"
python scripts/smoke.py --json | jq                      # for a CI job or a cron
```

Exit codes: ``0`` when every check passes, ``1`` when a check fails (the failing
one is named), ``2`` when the API cannot be reached at all — so a supervisor can
tell "not deployed" from "deployed but broken".

The script creates one project named ``smoke test …`` and leaves it alone: it
never runs it, never calls a connector and never writes outside the API. Pass
``--cleanup`` to delete nothing (Kollektiv has no delete endpoint) but to print
the project id for your own bookkeeping.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

#: Timeout for a single request (the planner can be slow on a cold start).
REQUEST_TIMEOUT = 60.0
#: How long to wait for the first SSE frame from a project stream.
STREAM_TIMEOUT = 15.0


class Smoke:
    """Collects check results and prints them, in order, as they happen."""

    def __init__(self, base_url: str, token: Optional[str], as_json: bool) -> None:
        """Store the target and the reporting mode."""
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.as_json = as_json
        self.results: List[Dict[str, Any]] = []
        self.project_id: Optional[str] = None

    def record(self, name: str, ok: bool, detail: str = "", seconds: float = 0.0) -> bool:
        """Remember one check and print it (unless JSON output was asked for)."""
        self.results.append({"check": name, "ok": ok, "detail": detail, "seconds": round(seconds, 3)})
        if not self.as_json:
            mark = "ok  " if ok else "FAIL"
            timing = f"{seconds:5.2f}s" if seconds else "     "
            print(f"  [{mark}] {timing} {name}{f' — {detail}' if detail else ''}")
        return ok

    @property
    def failed(self) -> List[str]:
        """Names of the checks that failed."""
        return [item["check"] for item in self.results if not item["ok"]]

    def finished_ok(self) -> bool:
        """Return ``True`` when every check so far passed (empty list included)."""
        return bool(self.results) and not self.failed

    def finish(self) -> int:
        """Print the summary (or the JSON document) and return the exit code."""
        if self.as_json:
            print(json.dumps({"base_url": self.base_url, "project_id": self.project_id, "checks": self.results}, indent=2))
        else:
            passed = len(self.results) - len(self.failed)
            print(f"\n{passed}/{len(self.results)} checks passed")
            if self.project_id:
                print(f"project created for the test: {self.project_id}")
        return 1 if self.failed else 0


def _headers(token: Optional[str]) -> Dict[str, str]:
    """Build request headers, including the bearer token when one was given."""
    headers = {"content-type": "application/json", "accept": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers


async def check_health(client: httpx.AsyncClient, smoke: Smoke) -> bool:
    """``GET /health`` answers and reports a usable status.

    A deployment that boots with warnings is normal (missing optional keys); a
    ``degraded`` status is not, and is reported here rather than at run time.
    """
    started = time.perf_counter()
    response = await client.get("/health")
    elapsed = time.perf_counter() - started
    if response.status_code != 200:
        return smoke.record("GET /health", False, f"HTTP {response.status_code}", elapsed)
    body = response.json()
    status = str(body.get("status", "unknown"))
    warnings = body.get("warnings") or []
    detail = status
    if warnings:
        detail += f", {len(warnings)} configuration warning(s)"
    return smoke.record("GET /health", status in ("ok", "degraded"), detail, elapsed)


async def check_dashboard(client: httpx.AsyncClient, smoke: Smoke) -> bool:
    """The bundled dashboard is served (it ships inside the wheel)."""
    started = time.perf_counter()
    response = await client.get("/ui/")
    elapsed = time.perf_counter() - started
    if response.status_code != 200:
        return smoke.record("GET /ui/ (dashboard)", False, f"HTTP {response.status_code}", elapsed)
    has_module = "assets/app.js" in response.text
    return smoke.record("GET /ui/ (dashboard)", has_module, "index.html + assets" if has_module else "index.html has no app.js")


async def check_read_endpoints(client: httpx.AsyncClient, smoke: Smoke) -> bool:
    """The read surface the dashboard depends on answers with the right shapes."""
    ok = True
    expected: Tuple[Tuple[str, str], ...] = (
        ("/projects", "projects"),
        ("/agents/status", "agents"),
        ("/storage/status", "accounts"),
        ("/connectors", "connectors"),
    )
    for path, key in expected:
        started = time.perf_counter()
        try:
            response = await client.get(path)
        except httpx.HTTPError as exc:
            ok = smoke.record(f"GET {path}", False, str(exc)) and ok
            continue
        elapsed = time.perf_counter() - started
        if response.status_code != 200:
            ok = smoke.record(f"GET {path}", False, f"HTTP {response.status_code}", elapsed) and ok
            continue
        body = response.json()
        present = isinstance(body, dict) and key in body
        ok = smoke.record(f"GET {path}", present, key if present else f"missing '{key}'", elapsed) and ok
    return ok


async def check_planner(client: httpx.AsyncClient, smoke: Smoke) -> bool:
    """Creating a project really plans one (brain or heuristic, no workers needed)."""
    payload = {
        "name": f"smoke test {int(time.time())}",
        "description": "Deployment smoke test: verify the planner, state document and handoff.",
        "n_agents": 2,
    }
    started = time.perf_counter()
    response = await client.post("/projects", json=payload)
    elapsed = time.perf_counter() - started
    if response.status_code not in (200, 201):
        return smoke.record("POST /projects (plan)", False, f"HTTP {response.status_code}: {response.text[:120]}", elapsed)
    body = response.json()
    project_id = body.get("project_id")
    tasks = (body.get("plan") or {}).get("tasks") or []
    if not project_id:
        return smoke.record("POST /projects (plan)", False, "no project_id in the response", elapsed)
    smoke.project_id = project_id
    return smoke.record("POST /projects (plan)", bool(tasks), f"{len(tasks)} task(s), {project_id}", elapsed)


async def check_project_state(client: httpx.AsyncClient, smoke: Smoke) -> bool:
    """The shared state document and the resume briefing are readable."""
    if not smoke.project_id:
        return smoke.record("GET /projects/{id}/status", False, "skipped: no project was planned")
    ok = True
    for path, key in ((f"/projects/{smoke.project_id}/status", "tasks"), (f"/projects/{smoke.project_id}/handoff", "progress")):
        started = time.perf_counter()
        response = await client.get(path)
        elapsed = time.perf_counter() - started
        if response.status_code != 200:
            ok = smoke.record(f"GET {path.rsplit('/', 1)[-1]}", False, f"HTTP {response.status_code}", elapsed) and ok
            continue
        body = response.json()
        present = isinstance(body, dict) and key in body
        ok = smoke.record(f"GET …{path.rsplit('/', 1)[-1]}", present, key if present else f"missing '{key}'", elapsed) and ok
    return ok


async def check_event_stream(client: httpx.AsyncClient, smoke: Smoke) -> bool:
    """The Server-Sent Events stream emits a state frame for a real project.

    The stream never closes by design, so it is read with a hard deadline and
    then dropped — exactly what a browser does when the tab is closed.
    """
    if not smoke.project_id:
        return smoke.record("GET /projects/{id}/events/stream", False, "skipped: no project was planned")
    started = time.perf_counter()
    url = f"/projects/{smoke.project_id}/events/stream?interval=0.5"
    try:
        async with client.stream("GET", url, timeout=STREAM_TIMEOUT) as response:
            if response.status_code != 200:
                return smoke.record("SSE stream", False, f"HTTP {response.status_code}", time.perf_counter() - started)
            buffered = ""
            async for chunk in response.aiter_text():
                buffered += chunk
                if "event: state" in buffered:
                    break
                if time.perf_counter() - started > STREAM_TIMEOUT:
                    break
    except httpx.HTTPError as exc:
        return smoke.record("SSE stream", False, str(exc), time.perf_counter() - started)
    elapsed = time.perf_counter() - started
    return smoke.record("SSE stream", "event: state" in buffered, "event: state received" if buffered else "no frame", elapsed)


async def run(base_url: str, token: Optional[str], as_json: bool) -> int:
    """Run every check against ``base_url`` and return the exit code."""
    smoke = Smoke(base_url, token, as_json)
    if not as_json:
        print(f"Kollektiv smoke test → {smoke.base_url}")
    headers = _headers(token)
    async with httpx.AsyncClient(base_url=smoke.base_url, headers=headers, timeout=REQUEST_TIMEOUT) as client:
        try:
            await check_health(client, smoke)
        except httpx.HTTPError as exc:
            smoke.record("GET /health", False, f"unreachable: {exc}")
            if not as_json:
                print("\nThe API is not answering. Is it running, and is the URL right?")
            if as_json:
                print(json.dumps({"base_url": smoke.base_url, "checks": smoke.results}, indent=2))
            return 2
        await check_dashboard(client, smoke)
        await check_read_endpoints(client, smoke)
        await check_planner(client, smoke)
        await check_project_state(client, smoke)
        await check_event_stream(client, smoke)
    return smoke.finish()


def main(argv: Optional[List[str]] = None) -> int:
    """Parse arguments and run the smoke test."""
    parser = argparse.ArgumentParser(description="Smoke-test a running Kollektiv deployment.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("KOLLEKTIV_BASE_URL", "http://localhost:8000"),
        help="API root (default: %(default)s, or $KOLLEKTIV_BASE_URL)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("KOLLEKTIV_TOKEN", ""),
        help="Clerk/bearer token for a deployment with AUTH_REQUIRED=true (or $KOLLEKTIV_TOKEN)",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.base_url, args.token or None, args.json))


if __name__ == "__main__":  # pragma: no cover - manual entry point
    sys.exit(main())
