"""Tests for the resume briefing (handoff), the login flow and connector probes.

Everything is hermetic: state dicts are built in memory, the token store uses a
temporary SQLite database and connector probes use the fakes from
``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from config.settings import Settings
from src.orchestrator.handoff import (
    age_seconds,
    blockers,
    build_handoff,
    next_actions,
    render_markdown,
    write_handoff_file,
)


def run(coro: Any) -> Any:
    """Run a coroutine from a synchronous test."""
    return asyncio.run(coro)


def state_fixture() -> Dict[str, Any]:
    """A state document with one completed, one failed and one blocked task."""
    return {
        "project_name": "shortener",
        "description": "Build a URL shortener with FastAPI and tests",
        "status": "running",
        "revision": 2,
        "last_commit": "abc1234",
        "tasks": [
            {"id": "t1", "title": "Scaffold the package", "status": "completed", "assigned_agent": "w1"},
            {"id": "t2", "title": "Implement storage", "status": "in_progress", "assigned_agent": "w2"},
            {"id": "t3", "title": "Add tests", "status": "failed", "assigned_agent": "w3", "error": "timeout"},
            {"id": "t4", "title": "Add the CLI", "status": "pending", "depends_on": ["t1"]},
            {"id": "t5", "title": "Publish docs", "status": "pending", "depends_on": ["t3"]},
        ],
        "files": ["src/app.py", "tests/test_app.py"],
        "history": [
            {"timestamp": "2026-01-01T00:00:00Z", "agent_id": "w1", "action": "dispatch", "result": "ok"},
            {"timestamp": "2026-01-01T00:05:00Z", "agent_id": "w3", "action": "review", "result": "failed"},
        ],
        "recent_commits": [{"sha": "abc1234", "message": "feat: scaffold", "author": "w1"}],
    }


# ----------------------------------------------------------------------
# Ordering logic
# ----------------------------------------------------------------------
def test_next_actions_resumes_in_flight_before_starting_new_work() -> None:
    """A fresh session should pick up unfinished work first."""
    actions = next_actions(state_fixture())
    assert [action["id"] for action in actions[:2]] == ["t2", "t3"]
    assert actions[0]["reason"] == "already in progress"
    assert "retry or replan" in actions[1]["reason"]


def test_next_actions_respects_dependencies() -> None:
    """A pending task with unfinished dependencies is reported as blocked."""
    actions = {action["id"]: action for action in next_actions(state_fixture())}
    assert actions["t4"]["status"] == "pending"          # t1 is completed
    assert actions["t5"]["status"] == "blocked"          # t3 failed
    assert "t3" in actions["t5"]["reason"]


def test_next_actions_without_a_plan_explains_itself() -> None:
    """An empty state yields no actions and a blocker that says why."""
    handoff = build_handoff({"project_name": "empty"}, "prj_empty")
    assert handoff["next_actions"] == []
    assert "no plan recorded" in handoff["blockers"][0]
    assert "Nothing actionable" in handoff["markdown"]


def test_blockers_reports_failed_tasks_with_their_error() -> None:
    """The blocker list names the task and the recorded error."""
    found = blockers(state_fixture())
    assert any("t3" in item and "timeout" in item for item in found)


# ----------------------------------------------------------------------
# Briefing
# ----------------------------------------------------------------------
def test_build_handoff_counts_progress() -> None:
    """Counters, percentage and dashboard links are part of the briefing."""
    handoff = build_handoff(state_fixture(), "prj_1")
    assert handoff["project_name"] == "shortener"
    assert handoff["progress"] == {
        "total": 5,
        "completed": 1,
        "failed": 1,
        "in_progress": 1,
        "pending": 2,
        "percent": 20,
    }
    assert handoff["queued_tasks"] == 2
    assert handoff["dashboard"]["api"] is None  # no APP_BASE_URL configured
    assert age_seconds(handoff) is not None


def test_render_markdown_has_the_sections_a_session_needs() -> None:
    """The briefing carries actions, blockers, files, history and how to continue."""
    markdown = render_markdown(build_handoff(state_fixture(), "prj_1"))
    for heading in ("# Handoff — shortener", "## Next actions", "## Blockers", "## Recent history", "## How to continue"):
        assert heading in markdown
    assert "kollektiv run --project-id prj_1" in markdown
    assert "`src/app.py`" in markdown
    assert "1/5 tasks completed (20%)" in markdown


def test_build_handoff_uses_settings_links(settings: Settings) -> None:
    """APP_BASE_URL turns into clickable dashboard/API links."""
    resolved = settings.model_copy(update={"APP_BASE_URL": "https://kollektiv.example.com/"})
    handoff = build_handoff(state_fixture(), "prj_1", settings=resolved)
    assert handoff["dashboard"]["url"] == "https://kollektiv.example.com/ui"
    assert handoff["dashboard"]["api"] == "https://kollektiv.example.com/projects/prj_1/status"


def test_write_handoff_file(tmp_path: Path) -> None:
    """HANDOFF.md is written into the project workspace."""
    handoff = build_handoff(state_fixture(), "prj_1")
    path = write_handoff_file(handoff, tmp_path / "prj_1")
    assert Path(path).name == "HANDOFF.md"
    assert "Handoff — shortener" in Path(path).read_text(encoding="utf-8")


async def test_orchestrator_handoff_end_to_end(settings: Settings, tmp_path: Path) -> None:
    """Creating a project then asking for a handoff yields a usable briefing."""
    from tests.test_orchestrator import build_fake_orchestrator

    resolved = settings.model_copy(update={"WORKSPACE_DIR": str(tmp_path / "ws")})
    orchestrator = build_fake_orchestrator(resolved)
    created = await orchestrator.create_project("demo", "Build a pastebin with tests", 2)
    handoff = await orchestrator.get_handoff(created["project_id"])
    assert handoff["progress"]["total"] >= 1
    assert handoff["next_actions"], "a fresh plan always has something to do"
    assert Path(handoff["written_to"]).name == "HANDOFF.md"
    assert Path(handoff["written_to"]).exists()


def test_handoff_endpoint(settings: Settings, tmp_path: Path) -> None:
    """GET /projects/{id}/handoff returns JSON and the markdown variant."""
    from src.api.routes import create_app
    from tests.test_orchestrator import build_fake_orchestrator

    resolved = settings.model_copy(update={"WORKSPACE_DIR": str(tmp_path / "ws")})
    orchestrator = build_fake_orchestrator(resolved)
    app = create_app(resolved, orchestrator=orchestrator)
    app.state.orchestrator = orchestrator
    transport = httpx.ASGITransport(app=app)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/projects", json={"name": "handoff-demo", "description": "Build a URL shortener with tests", "n_agents": 2}
            )
            project_id = created.json()["project_id"]

            briefing = await client.get(f"/projects/{project_id}/handoff")
            assert briefing.status_code == 200
            body = briefing.json()
            assert "next_actions" in body and "markdown" not in body
            assert body["progress"]["total"] >= 1

            markdown = await client.get(f"/projects/{project_id}/handoff?markdown=true")
            assert markdown.status_code == 200
            assert markdown.headers["content-type"].startswith("text/markdown")
            assert "# Handoff" in markdown.text

            missing = await client.get("/projects/prj_nope/handoff")
            assert missing.status_code == 404

    run(scenario())


# ----------------------------------------------------------------------
# Login / credentials
# ----------------------------------------------------------------------
def _login_env(settings: Settings, tmp_path: Path, monkeypatch: Any) -> Settings:
    """Point the CLI at an isolated database and secret for one test."""
    import src.api.cli as cli_module

    resolved = settings.model_copy(
        update={"DATABASE_URL": f"sqlite:///{tmp_path / 'tokens.db'}", "SECRET_KEY": "unit-test-secret"}
    )
    monkeypatch.setattr(cli_module, "get_settings", lambda: resolved)
    return resolved


def _args(**overrides: Any) -> Any:
    """A namespace shaped like the parsed `kollektiv login` / `logout` arguments."""
    base: Dict[str, Any] = {
        "provider": "deepseek",
        "name": "",
        "account": "",
        "token": "",
        "key_env": "",
        "base_url": "",
        "model": "",
        "env_file": ".env",
        "json": True,
    }
    base.update(overrides)
    return type("Args", (), base)()


def test_login_stores_an_encrypted_key_and_registers_the_worker(
    settings: Settings, tmp_path: Path, monkeypatch: Any
) -> None:
    """`kollektiv login` encrypts the key, writes the worker into the env file and never leaks the key."""
    from src.api.cli import cmd_login
    from src.db.models import bind_engine
    from src.utils.token_store import TokenStore

    resolved = _login_env(settings, tmp_path, monkeypatch)
    env_file = tmp_path / ".env"
    key = "sk-test-abcdef1234"
    assert run(cmd_login(_args(name="Vega", token=key, env_file=str(env_file)))) == 0

    env_text = env_file.read_text(encoding="utf-8")
    assert "ARENA_ACCOUNTS=" in env_text and '"name":"Vega"' in env_text and '"provider":"deepseek"' in env_text
    assert key not in env_text, "keys must never be written to the env file"

    engine = bind_engine(resolved)
    store = TokenStore("unit-test-secret", engine=engine)
    assert store.get_token("deepseek", "vega")["api_key"] == key
    with engine.connect() as connection:
        raw = connection.exec_driver_sql("SELECT payload FROM token_records").fetchall()
    assert raw and key not in str(raw)


def test_login_with_key_env_stores_nothing(settings: Settings, tmp_path: Path, monkeypatch: Any) -> None:
    """With --key-env the key stays in the environment; only the variable name is recorded."""
    from src.api.cli import cmd_login
    from src.db.models import bind_engine
    from src.utils.token_store import TokenStore

    resolved = _login_env(settings, tmp_path, monkeypatch)
    monkeypatch.setenv("VEGA_DEEPSEEK_KEY", "sk-from-environment")
    env_file = tmp_path / ".env"
    assert run(cmd_login(_args(name="Vega", key_env="VEGA_DEEPSEEK_KEY", env_file=str(env_file)))) == 0
    assert '"api_key_env":"VEGA_DEEPSEEK_KEY"' in env_file.read_text(encoding="utf-8")
    store = TokenStore("unit-test-secret", engine=bind_engine(resolved))
    assert store.get_token("deepseek", "vega") in ({}, None)


def test_local_providers_need_no_key(settings: Settings, tmp_path: Path, monkeypatch: Any) -> None:
    """Ollama registers without asking for anything."""
    from src.api.cli import cmd_login

    _login_env(settings, tmp_path, monkeypatch)
    env_file = tmp_path / ".env"
    assert run(cmd_login(_args(provider="ollama", name="Terra", env_file=str(env_file)))) == 0
    assert '"provider":"ollama"' in env_file.read_text(encoding="utf-8")


def test_login_validates_inputs(settings: Settings, tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    """Unknown providers and missing keys fail with exit code 2 and the fix in the message."""
    from src.api.cli import cmd_login

    _login_env(settings, tmp_path, monkeypatch)
    assert run(cmd_login(_args(provider="nope", token="x"))) == 2
    assert "Unknown provider" in capsys.readouterr().out
    assert run(cmd_login(_args(provider="groq", token="", env_file=str(tmp_path / ".env")))) == 2
    assert "No API key supplied" in capsys.readouterr().out


def test_logout_removes_the_key_and_the_worker(settings: Settings, tmp_path: Path, monkeypatch: Any) -> None:
    """`kollektiv logout` deletes the stored key and drops the worker from the env file."""
    import src.api.cli as cli_module
    from src.api.cli import cmd_login, cmd_logout

    resolved = _login_env(settings, tmp_path, monkeypatch)
    env_file = tmp_path / ".env"
    assert run(cmd_login(_args(name="Vega", token="sk-one-two-three", env_file=str(env_file)))) == 0

    # The worker is now listed in the env file; logout must see that list in settings.
    listed = json.loads(env_file.read_text(encoding="utf-8").split("ARENA_ACCOUNTS=", 1)[1].splitlines()[0])
    assert [w["account_id"] for w in listed] == ["vega"]
    monkeypatch.setattr(cli_module, "get_settings", lambda: resolved.model_copy(update={"ARENA_ACCOUNTS": json.dumps(listed)}))

    assert run(cmd_logout(_args(account="vega", env_file=str(env_file)))) == 0
    assert "ARENA_ACCOUNTS=[]" in env_file.read_text(encoding="utf-8")


def test_login_presets_are_the_shared_provider_table() -> None:
    """The CLI and the worker use one provider table, so `login` and the runtime never disagree."""
    from src.agents.providers import PROVIDER_PRESETS as SHARED
    from src.api.cli import PROVIDER_PRESETS

    assert PROVIDER_PRESETS is SHARED
    assert "arena" not in PROVIDER_PRESETS, "Arena web sessions are not an API and are not supported"


# ----------------------------------------------------------------------
# Connector probes and parameter validation
# ----------------------------------------------------------------------
def test_registry_probe_reports_reachability(settings: Settings) -> None:
    """A configured connector probes its read-only action; others report why."""
    from src.connectors.base import ConnectorRegistry

    payloads: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append({"url": str(request.url)})
        return httpx.Response(200, text="ok")

    resolved = settings.model_copy(update={"EVENT_WEBHOOKS": "https://hooks.test/x"})
    registry = ConnectorRegistry.from_settings(resolved, token_store=None)
    registry.get("webhook")._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[attr-defined]
    probes = {probe["connector"]: probe for probe in run(registry.probe_all())}
    assert probes["webhook"]["ok"] is True
    assert probes["webhook"]["action"] == "list_targets"
    assert probes["webhook"]["seconds"] >= 0
    assert probes["google"]["ok"] is False
    assert "not configured" in probes["google"]["error"]


def test_probe_survives_a_failing_service(settings: Settings) -> None:
    """A probe never raises: it reports the error instead."""
    from src.connectors.base import ConnectorRegistry

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    resolved = settings.model_copy(update={"GITHUB_TOKEN": "t", "GITHUB_REPO": "a/b"})
    registry = ConnectorRegistry.from_settings(resolved, token_store=None)
    connector = registry.get("github")
    connector._github._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=resolved.GITHUB_API_URL)  # type: ignore[attr-defined]
    report = run(registry.probe("github"))
    assert report["ok"] is False and report["error"]


def test_unknown_parameters_are_rejected_with_the_accepted_list(settings: Settings) -> None:
    """Typos are caught before any request leaves the process."""
    from src.connectors.base import ConnectorRegistry
    from src.utils.errors import ConnectorError

    resolved = settings.model_copy(update={"EVENT_WEBHOOKS": "https://hooks.test/x"})
    registry = ConnectorRegistry.from_settings(resolved, token_store=None)
    with pytest.raises(ConnectorError, match="does not accept tex"):
        run(registry.call("webhook", "notify", {"tex": "typo", "text": "hi"}))


def test_api_probe_endpoint(settings: Settings) -> None:
    """POST /connectors/{name}/probe exposes the probe over HTTP."""
    from src.api.routes import create_app
    from src.connectors.base import ConnectorRegistry
    from src.orchestrator.app import Orchestrator

    resolved = settings.model_copy(update={"EVENT_WEBHOOKS": "https://hooks.test/x"})
    orchestrator = Orchestrator(resolved)
    orchestrator.connectors = ConnectorRegistry.from_settings(resolved, token_store=None)
    app = create_app(resolved, orchestrator=orchestrator)
    app.state.orchestrator = orchestrator
    transport = httpx.ASGITransport(app=app)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            report = await client.post("/connectors/webhook/probe")
            assert report.status_code == 200
            assert report.json()["action"] == "list_targets"
            unknown = await client.post("/connectors/nope/probe")
            assert unknown.status_code == 404

    run(scenario())


def test_cli_connectors_probe_flag(settings: Settings, capsys: Any, tmp_path: Path) -> None:
    """`kollektiv connectors --probe` prints a per-service result."""
    from src.api.cli import cmd_connectors

    resolved = settings.model_copy(
        update={"DATABASE_URL": f"sqlite:///{tmp_path / 'probe.db'}", "EVENT_WEBHOOKS": "https://hooks.test/x"}
    )
    import src.api.cli as cli_module
    from config import settings as settings_module

    original = settings_module.get_settings
    settings_module.get_settings = lambda: resolved  # type: ignore[assignment]
    cli_module.get_settings = lambda: resolved  # type: ignore[assignment]
    try:
        args = type("Args", (), {"json": False, "probe": True})()
        code = run(cmd_connectors(args))
    finally:
        settings_module.get_settings = original  # type: ignore[assignment]
    output = capsys.readouterr().out
    assert "Connector probes" in output
    assert "webhook" in output and "google" in output
    assert code in (0, 1)  # webhook fails (no listener) -> 1, but output is complete
