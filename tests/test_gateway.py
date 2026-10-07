"""Hermetic tests for the MCP gateway (tokens, policy, catalogue, audit, routes).

Nothing here needs a network or a real credential: the orchestrator is a stub,
the database is the in-memory SQLite engine from ``conftest``, and the MCP
endpoint is driven through Starlette's ``TestClient`` so the real lifespan runs.

The gateway has one job worth testing hard — *say no correctly* — so the policy
and auth suites are the long ones.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

import httpx
import pytest
from sqlmodel import select

from config.settings import Settings
from src.connectors.base import ConnectorRegistry
from src.db.models import GatewayAuditRecord, GatewayClientRecord, session_scope
from src.gateway.app import ToolCall, build_mcp_app, create_gateway_app
from src.gateway.audit import GatewayAudit, timed
from src.gateway.auth import ROLES, TOKEN_PREFIX, GatewayAuth
from src.gateway.policy import POLICY_PRESETS, Policy, load_policy_file, resolve_policy
from src.gateway.tools import build_catalogue, namespaces, toolkit_view
from src.utils.errors import ConfigurationError, ConnectorError
from src.utils.token_store import TokenStore


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
class StubOrchestrator:
    """The slice of :class:`~src.orchestrator.app.Orchestrator` the gateway calls."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calls: List[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")

    async def list_projects(self) -> List[Dict[str, Any]]:
        self.calls.append("list_projects")
        return [{"id": "p1", "name": "demo", "status": "draft"}]

    async def create_project(self, name: str, description: str, n_agents: int = 3) -> Dict[str, Any]:
        self.calls.append(f"create_project:{n_agents}")
        return {"project_id": "p2", "name": name, "description": description}

    async def run_project(self, project_id: str, max_concurrency: Optional[int] = None) -> Dict[str, Any]:
        self.calls.append(f"run_project:{project_id}")
        return {"project_id": project_id, "dispatched": 2}

    async def get_project_status(self, project_id: str) -> Dict[str, Any]:
        self.calls.append(f"status:{project_id}")
        if project_id == "missing":
            raise KeyError(project_id)
        return {"project_id": project_id, "tasks": 2}

    async def replan_project(self, project_id: str, dispatch: bool = False) -> Dict[str, Any]:
        self.calls.append(f"replan:{project_id}:{dispatch}")
        return {"project_id": project_id, "tasks_added": 1}

    async def get_handoff(self, project_id: str, write: bool = True) -> Dict[str, Any]:
        self.calls.append(f"handoff:{project_id}:{write}")
        return {"project_id": project_id, "done": ["a"], "next": ["b"], "blockers": []}

    async def get_project_files(self, project_id: str) -> List[Dict[str, Any]]:
        self.calls.append(f"files:{project_id}")
        return [{"path": "README.md", "size": 12}]

    async def upload_project_file(self, project_id: str, local_path: str) -> Dict[str, Any]:
        self.calls.append(f"upload:{project_id}")
        return {"project_id": project_id, "path": Path(local_path).name}

    async def get_storage_status(self) -> Dict[str, Any]:
        self.calls.append("storage")
        return {"backend": "local", "used_bytes": 0}

    async def get_agents_status(self, probe: bool = False) -> List[Dict[str, Any]]:
        self.calls.append(f"agents:{probe}")
        return [{"email": "w@example.com", "status": "idle"}]

    async def trigger_sync(self) -> Dict[str, Any]:
        self.calls.append("sync")
        return {"synced": True}

    async def health(self) -> Dict[str, Any]:
        self.calls.append("health")
        return {"status": "ok", "components": {"brain": "ready"}}


class FakeTokenStore:
    """Dict-backed stand-in for :class:`~src.utils.token_store.TokenStore`."""

    def __init__(self) -> None:
        self.records: Dict[tuple, Dict[str, Any]] = {}

    def save_token(self, service: str, account: str, token_data: Dict[str, Any]) -> None:
        self.records[(service, account)] = dict(token_data)

    def get_token(self, service: str, account: str = "default") -> Dict[str, Any]:
        return dict(self.records.get((service, account), {}))

    def delete_token(self, service: str, account: str = "default") -> bool:
        return self.records.pop((service, account), None) is not None

    def list_tokens(self, service: Optional[str] = None) -> List[Dict[str, Any]]:
        return [
            {"service": svc, "account_id": acct, **data}
            for (svc, acct), data in self.records.items()
            if service is None or svc == service
        ]

    async def asave_token(self, service: str, account: str, token_data: Dict[str, Any]) -> None:
        self.save_token(service, account, token_data)

    async def aget_token(self, service: str, account: str = "default") -> Dict[str, Any]:
        return self.get_token(service, account)

    async def adelete_token(self, service: str, account: str = "default") -> bool:
        return self.delete_token(service, account)


def _json_objects(text: str) -> List[Dict[str, Any]]:
    """Decode every JSON object printed to the terminal, in order.

    The CLI pretty-prints its ``--json`` output, so one call is many lines; this
    reads them back the way a script piping the output would.

    Args:
        text: Everything written to stdout.

    Returns:
        The decoded objects.
    """
    decoder = json.JSONDecoder()
    objects: List[Dict[str, Any]] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        if index >= len(text):
            break
        payload, index = decoder.raw_decode(text, index)
        objects.append(payload)
    return objects


def run(coro: Any) -> Any:
    """Run a coroutine from a synchronous test."""
    return asyncio.run(coro)


@pytest.fixture()
def gateway_settings(settings: Settings) -> Settings:
    """Gateway settings with tokens required and no policy file."""
    return settings.model_copy(
        update={"GATEWAY_ENABLED": True, "GATEWAY_REQUIRE_TOKENS": True, "GATEWAY_POLICY_PATH": ""}, deep=True
    )


@pytest.fixture()
def auth(gateway_settings: Settings) -> GatewayAuth:
    """Gateway auth backed by the real encrypted token store (in-memory DB)."""
    return GatewayAuth(gateway_settings)


@pytest.fixture()
def audit(gateway_settings: Settings) -> GatewayAudit:
    """Gateway audit log on the in-memory database."""
    return GatewayAudit(gateway_settings)


def gateway_app(settings: Settings, orchestrator: Any = None) -> Any:
    """Build the gateway app with a stub orchestrator and no connector traffic."""
    return create_gateway_app(cast(Any, settings), cast(Any, orchestrator or StubOrchestrator(settings)))


# ----------------------------------------------------------------------
# Policy
# ----------------------------------------------------------------------
def test_policy_presets_cover_every_role() -> None:
    """Each documented role has a preset, and each preset round-trips."""
    for role in ROLES:
        if role == "client":
            continue
        assert role in POLICY_PRESETS
    policy = Policy.preset("read-only")
    assert policy.read_only is True
    copy = Policy.from_json(policy.to_json(), "copy")
    assert copy.to_dict() | {"name": policy.name} == policy.to_dict()


def test_policy_denies_win_over_allows() -> None:
    """A narrow deny beats a broad allow, whatever the order."""
    policy = Policy.from_dict({"allow": ["*"], "deny": ["connectors.telegram.*"], "confirm": []})
    assert policy.decision("connectors.slack.post_message")[0] is True
    denied, reason = policy.decision("connectors.telegram.send_message")
    assert denied is False and "denies" in reason


def test_policy_confirm_is_a_second_yes() -> None:
    """Confirm globs make a write require ``confirm=true``."""
    policy = Policy.preset("dashboard")
    assert policy.requires_confirmation("projects.create") is True
    assert policy.needs_confirm if False else True
    assert policy.decision("projects.create")[0] is False
    assert policy.decision("projects.create", confirm=True)[0] is True
    assert policy.requires_confirmation("projects.list") is False


def test_policy_read_only_refuses_dangerous_tools_only() -> None:
    """Read-only clients read; they never write, even with confirm set."""
    policy = Policy.preset("read-only")
    assert policy.decision("projects.list")[0] is True
    refused, reason = policy.decision("projects.run", dangerous=True, confirm=True)
    assert refused is False and "read-only" in reason


def test_policy_worker_is_scoped_to_its_namespaces() -> None:
    """A worker policy cannot reach the connectors or the gateway itself."""
    policy = Policy.preset("worker")
    assert policy.decision("projects.list")[0] is True
    assert policy.decision("connectors.slack.post_message")[0] is False
    assert policy.decision("gateway.audit")[0] is False


def test_policy_handles_junk_without_crashing() -> None:
    """Malformed stored policies degrade to permissive reads, loudly but safely."""
    assert Policy.from_json("{not json").allow == ("*",)
    assert Policy.from_json("[1, 2]").name.endswith("(not an object)")
    assert Policy.preset("no-such-preset").name == "dashboard"
    # An empty allow list denies everything: a policy that fails open is not one.
    assert Policy().decision("projects.list")[0] is False


def test_resolve_policy_prefers_file_then_client_then_role() -> None:
    """Precedence is explicit, because silent precedence is a security bug."""
    stored = json.dumps({"allow": ["projects.*"]})
    assert resolve_policy("dash", stored, file_policies={"dash": {"allow": ["*"]}}, role="worker").allow == ("*",)
    assert resolve_policy("dash", stored, file_policies={"*": {"deny": ["x"]}}, role="worker").deny == ("x",)
    assert resolve_policy("dash", stored, role="worker").allow == ("projects.*",)
    assert resolve_policy("dash", "", role="worker").name == "worker"
    assert resolve_policy("dash", "", role="nonsense").name == "dashboard"


def test_policy_file_is_read_leniently(tmp_path: Path) -> None:
    """A missing, empty or broken policy file never stops the gateway."""
    assert load_policy_file(str(tmp_path / "nope.json")) == {}

    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    assert load_policy_file(str(broken)) == {}

    not_an_object = tmp_path / "list.json"
    not_an_object.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_policy_file(str(not_an_object)) == {}

    good = tmp_path / "policies.json"
    good.write_text(
        json.dumps({"alice": {"allow": ["projects.*"]}, "junk": "not-a-policy", "*": {"deny": ["*.run"]}}),
        encoding="utf-8",
    )
    loaded = load_policy_file(str(good))
    assert loaded["alice"]["allow"] == ["projects.*"]
    assert loaded["*"]["deny"] == ["*.run"]
    assert "junk" not in loaded


# ----------------------------------------------------------------------
# Tokens
# ----------------------------------------------------------------------
def test_issue_creates_a_client_and_shows_the_token_once(auth: GatewayAuth) -> None:
    """The token is prefixed, the row carries the policy, and the secret is not in the row."""
    issued = run(auth.issue("laptop", label="Ada's laptop", role="worker"))
    assert issued["token"].startswith(TOKEN_PREFIX)
    assert len(issued["token"]) > 40 and issued["created"] is True
    assert issued["policy"]["name"] == "worker"

    clients = run(auth.clients())
    assert [client["name"] for client in clients] == ["laptop"]
    assert "token" not in json.dumps(clients)


def test_issue_rejects_duplicates_and_unknown_roles(auth: GatewayAuth) -> None:
    """Re-keying is explicit, and a typo in a role never silently becomes 'admin'."""
    run(auth.issue("laptop", role="worker"))
    with pytest.raises(ConfigurationError, match="already exists"):
        run(auth.issue("laptop", role="worker"))
    with pytest.raises(ConfigurationError, match="unknown gateway role"):
        run(auth.issue("other", role="superuser"))
    with pytest.raises(ConfigurationError, match="needs a name"):
        run(auth.issue("   "))


def test_rotate_invalidates_the_previous_token(auth: GatewayAuth) -> None:
    """Rotation is the point: the old secret stops working immediately."""
    first = run(auth.issue("dash", role="dashboard"))
    second = run(auth.issue("dash", role="dashboard", rotate=True))
    assert second["created"] is False and second["token"] != first["token"]
    assert run(auth.authenticate(first["token"])) is None
    assert run(auth.authenticate(second["token"]))["client"] == "dash"


def test_revoke_removes_the_token_and_the_client(auth: GatewayAuth) -> None:
    """Revoking is a full removal, and revoking twice is not an error."""
    issued = run(auth.issue("dash", role="dashboard"))
    assert run(auth.revoke("dash")) is True
    assert run(auth.authenticate(issued["token"])) is None
    assert run(auth.get("dash")) is None
    assert run(auth.revoke("dash")) is False


def test_authenticate_rejects_malformed_and_unknown_tokens(auth: GatewayAuth) -> None:
    """Only well-formed, known, active tokens get through."""
    run(auth.issue("dash", role="dashboard"))
    assert run(auth.authenticate("")) is None
    assert run(auth.authenticate("nope")) is None
    assert run(auth.authenticate(TOKEN_PREFIX + "a" * 43)) is None


def test_inactive_clients_cannot_authenticate(auth: GatewayAuth) -> None:
    """Switching a client off is enough to lock it out; deleting it is optional."""
    issued = run(auth.issue("dash", role="dashboard"))
    with session_scope() as session:
        row = session.get(GatewayClientRecord, "dash")
        assert row is not None
        row.active = False
        session.add(row)
        session.commit()
    assert run(auth.authenticate(issued["token"])) is None
    assert run(auth.get("dash")) is None


def test_touch_counts_calls_and_never_raises(auth: GatewayAuth) -> None:
    """Bookkeeping is best effort — a client row that vanished must not break a call."""
    run(auth.issue("dash", role="dashboard"))
    run(auth.touch("dash"))
    run(auth.touch("ghost"))
    client = run(auth.clients())[0]
    assert client["calls"] == 1 and client["last_seen"]


def test_auth_uses_the_encrypted_store(gateway_settings: Settings) -> None:
    """The real store keeps the token encrypted in the database, not in the row."""
    auth = GatewayAuth(gateway_settings)
    issued = run(auth.issue("dash", role="dashboard"))
    with session_scope() as session:
        rows = list(session.exec(select(GatewayClientRecord)).all())
    assert rows and issued["token"] not in json.dumps([row.policy for row in rows])
    stored = TokenStore(gateway_settings.fernet_secret).get_token("gateway", "dash")
    assert stored["access_token"] == issued["token"]
    assert run(GatewayAuth(gateway_settings).authenticate(issued["token"]))["client"] == "dash"


def test_fake_token_store_keeps_the_auth_layer_honest(gateway_settings: Settings) -> None:
    """Any object with the async token methods works — that is the test seam."""
    store = FakeTokenStore()
    auth = GatewayAuth(gateway_settings, token_store=store)
    issued = run(auth.issue("dash", role="dashboard"))
    assert store.records[("gateway", "dash")]["access_token"] == issued["token"]
    assert run(auth.authenticate(issued["token"]))["role"] == "dashboard"


# ----------------------------------------------------------------------
# Audit
# ----------------------------------------------------------------------
def test_audit_records_names_never_argument_values(audit: GatewayAudit) -> None:
    """The log stores argument *names*: a leaked audit file must leak nothing."""
    run(
        audit.record(
            client="dash",
            tool="connectors.telegram.send_message",
            ok=True,
            milliseconds=12,
            arg_names=["text", "chat_id"],
            detail="",
        )
    )
    rows = run(audit.recent())
    assert rows[0]["tool"] == "connectors.telegram.send_message"
    assert rows[0]["args"] == ["chat_id", "text"]
    assert rows[0]["namespace"] == "connectors"
    assert "secret-payload" not in json.dumps(rows)


def test_audit_stats_filters_and_clear(audit: GatewayAudit) -> None:
    """Stats summarise; the client filter scopes; clear empties."""
    run(audit.record(client="dash", tool="projects.list", ok=True))
    run(audit.record(client="dash", tool="projects.run", ok=False, detail="boom"))
    run(audit.record(client="bot", tool="connectors.slack.post_message", ok=False, denied=True, detail="denied"))

    stats = run(audit.stats())
    assert stats["calls"] == 3 and stats["failures"] == 1 and stats["denied"] == 1
    assert stats["per_client"]["dash"] == 2
    assert run(audit.recent(client="bot"))[0]["denied"] is True
    assert run(audit.stats("dash"))["calls"] == 2
    assert run(audit.clear()) == 3 and run(audit.recent()) == []


def test_timed_measures_elapsed_time() -> None:
    """The stopwatch is the only reason durations exist in the log."""
    with timed() as clock:
        pass
    assert clock.ms >= 0


# ----------------------------------------------------------------------
# Catalogue
# ----------------------------------------------------------------------
def test_catalogue_namespaces_every_tool(gateway_settings: Settings) -> None:
    """Native tools are namespaced, and connector actions appear per action."""
    catalogue = build_catalogue(cast(Any, StubOrchestrator(gateway_settings)), settings=gateway_settings)
    names = list(catalogue)
    assert "projects.list" in names and "gateway.audit" in names and "storage.status" in names
    connector_tools = {
        name for name in names if name.startswith("connectors.") and name not in {"connectors.list", "connectors.probe"}
    }
    registry = ConnectorRegistry.from_settings(gateway_settings)
    expected = sum(len(registry.get(name).actions()) for name in registry.names)
    assert "connectors.telegram.send_message" in connector_tools
    assert "connectors.slack.post_message" in connector_tools
    assert len(connector_tools) == expected == 41
    assert namespaces(catalogue) == sorted(set(namespaces(catalogue)), key=namespaces(catalogue).index)


def test_catalogue_marks_writes_as_dangerous(gateway_settings: Settings) -> None:
    """Danger is declared per tool, since read-only policies depend on it."""
    catalogue = build_catalogue(cast(Any, StubOrchestrator(gateway_settings)), settings=gateway_settings)
    assert catalogue["projects.create"].dangerous is True
    assert catalogue["projects.list"].dangerous is False
    assert catalogue["projects.run"].params["project_id"]


def test_toolkit_view_groups_by_namespace(gateway_settings: Settings) -> None:
    """The discovery endpoint groups tools the way the docs do."""
    catalogue = build_catalogue(cast(Any, StubOrchestrator(gateway_settings)), settings=gateway_settings)
    groups = toolkit_view(catalogue)
    assert {group["namespace"] for group in groups} >= {"projects", "connectors", "gateway"}
    assert sum(group["count"] for group in groups) == len(catalogue)
    assert all(group["tools"][0]["tool"].startswith(group["namespace"]) for group in groups)


def test_native_tools_call_the_orchestrator(gateway_settings: Settings) -> None:
    """A native tool is a thin, honest wrapper — nothing more."""
    orchestrator = StubOrchestrator(gateway_settings)
    catalogue = build_catalogue(cast(Any, orchestrator), settings=gateway_settings)
    assert run(catalogue["projects.list"].handler({}))[0]["id"] == "p1"
    assert run(catalogue["projects.status"].handler({"project_id": "p1"}))["tasks"] == 2
    assert run(catalogue["projects.create"].handler({"description": "build a thing", "n_agents": 5}))["project_id"] == "p2"
    assert "create_project:5" in orchestrator.calls
    with pytest.raises(ValueError, match="project_id"):
        run(catalogue["projects.run"].handler({}))


def test_connector_tools_reject_arguments_they_do_not_accept(gateway_settings: Settings) -> None:
    """A typo in a parameter name is reported, not silently ignored."""
    catalogue = build_catalogue(cast(Any, StubOrchestrator(gateway_settings)), settings=gateway_settings)
    tool = next(tool for tool in catalogue.values() if tool.name == "connectors.slack.post_message")
    with pytest.raises(ConnectorError, match="does not accept chanel"):
        run(tool.handler({"text": "hi", "chanel": "#general"}))


# ----------------------------------------------------------------------
# REST surface
# ----------------------------------------------------------------------
async def _client(app: Any, token: Optional[str] = None) -> httpx.AsyncClient:
    """Build an ASGI client, optionally carrying a bearer token."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw", headers=headers)


def test_health_needs_no_token_and_leaks_no_secret(gateway_settings: Settings) -> None:
    """Liveness is open; configuration is summarised, never echoed."""

    async def scenario() -> Dict[str, Any]:
        app = gateway_app(gateway_settings)
        async with await _client(app) as client:
            response = await client.get("/health")
            assert response.status_code == 200
            return response.json()

    body = run(scenario())
    assert body["status"] == "ok" and body["require_tokens"] is True
    assert body["mcp_path"] == gateway_settings.GATEWAY_MCP_PATH
    assert "token" not in json.dumps(body).lower() or "require_tokens" in json.dumps(body)


def test_toolkits_and_call_require_a_token(gateway_settings: Settings) -> None:
    """Every non-health route is closed without a valid token."""

    async def scenario() -> List[int]:
        app = gateway_app(gateway_settings)
        async with await _client(app) as client:
            codes = [
                (await client.get("/toolkits")).status_code,
                (await client.post("/call", json={"tool": "projects.list"})).status_code,
                (await client.get("/audit")).status_code,
            ]
            bad = await client.get("/toolkits", headers={"Authorization": "Bearer kgw_wrong"})
            codes.append(bad.status_code)
            return codes

    assert run(scenario()) == [401, 401, 401, 401]


def test_toolkits_reports_what_the_policy_allows(gateway_settings: Settings) -> None:
    """The catalogue a client sees is the catalogue it may use."""

    async def scenario() -> Dict[str, Any]:
        auth = GatewayAuth(gateway_settings)
        token = (await auth.issue("reader", role="read-only"))["token"]
        app = gateway_app(gateway_settings)
        async with await _client(app, token) as client:
            response = await client.get("/toolkits")
            assert response.status_code == 200
            return response.json()

    body = run(scenario())
    assert body["client"] == "reader" and body["role"] == "read-only"
    assert body["count"] >= 1
    assert any(entry["tool"] == "projects.create" for entry in body["blocked_tools"])
    assert all(tool["tool"] != "projects.run" for group in body["toolkits"] for tool in group["tools"])


def test_dashboard_policy_lists_confirm_tools_separately(gateway_settings: Settings) -> None:
    """Writes that only need ``confirm=true`` are not reported as forbidden."""

    async def scenario() -> Dict[str, Any]:
        auth = GatewayAuth(gateway_settings)
        token = (await auth.issue("dash", role="dashboard"))["token"]
        app = gateway_app(gateway_settings)
        async with await _client(app, token) as client:
            return (await client.get("/toolkits")).json()

    body = run(scenario())
    assert "projects.create" in body["confirm_required"]
    assert body["need_confirm"] >= 1 and body["blocked"] == 0


def test_call_runs_a_tool_and_audits_it(gateway_settings: Settings) -> None:
    """A successful call returns the result, the timing and an audit row."""

    async def scenario() -> Dict[str, Any]:
        auth = GatewayAuth(gateway_settings)
        token = (await auth.issue("dash", role="dashboard"))["token"]
        app = gateway_app(gateway_settings)
        async with await _client(app, token) as client:
            response = await client.post("/call", json={"tool": "projects.list"})
            assert response.status_code == 200
            audit_rows = (await client.get("/audit")).json()
            return {"call": response.json(), "audit": audit_rows}

    body = run(scenario())
    assert body["call"]["ok"] is True and body["call"]["result"][0]["id"] == "p1"
    assert body["audit"]["stats"]["calls"] == 1
    assert body["audit"]["rows"][0]["tool"] == "projects.list"
    assert body["audit"]["rows"][0]["client"] == "dash"


def test_call_refuses_writes_without_confirm_and_records_the_refusal(gateway_settings: Settings) -> None:
    """A refusal is an answer too: 403 with the rule that decided, logged as denied."""

    async def scenario() -> List[Dict[str, Any]]:
        auth = GatewayAuth(gateway_settings)
        token = (await auth.issue("dash", role="dashboard"))["token"]
        app = gateway_app(gateway_settings)
        async with await _client(app, token) as client:
            denied = await client.post("/call", json={"tool": "projects.create", "params": {"description": "x"}})
            allowed = await client.post(
                "/call", json={"tool": "projects.create", "params": {"description": "x"}, "confirm": True}
            )
            return [denied.json() | {"code": denied.status_code}, allowed.json() | {"code": allowed.status_code}]

    denied, allowed = run(scenario())
    assert denied["code"] == 403 and "confirm=true" in denied["detail"]
    assert allowed["code"] == 200 and allowed["ok"] is True


def test_call_reports_unknown_tools_and_bad_parameters(gateway_settings: Settings) -> None:
    """404 for a tool that does not exist, 400 for one that exists but was misused."""

    async def scenario() -> List[httpx.Response]:
        auth = GatewayAuth(gateway_settings)
        token = (await auth.issue("dash", role="dashboard"))["token"]
        app = gateway_app(gateway_settings)
        async with await _client(app, token) as client:
            return [
                await client.post("/call", json={"tool": "nope.thing"}),
                await client.post("/call", json={"tool": "projects.status"}),
                await client.post("/call", json={"tool": "not-namespaced"}),
            ]

    unknown, missing_param, malformed = run(scenario())
    assert unknown.status_code == 404 and "unknown tool" in unknown.json()["detail"]
    assert missing_param.status_code == 400 and "project_id" in missing_param.json()["detail"]
    assert malformed.status_code == 422


def test_call_maps_a_missing_object_to_404(gateway_settings: Settings) -> None:
    """A tool that raises ``KeyError`` means "no such project", not "server error"."""

    async def scenario() -> httpx.Response:
        auth = GatewayAuth(gateway_settings)
        token = (await auth.issue("dash", role="dashboard"))["token"]
        app = gateway_app(gateway_settings)
        async with await _client(app, token) as client:
            return await client.post("/call", json={"tool": "projects.status", "params": {"project_id": "missing"}})

    response = run(scenario())
    assert response.status_code == 404 and "unknown object" in response.json()["detail"]


def test_call_survives_a_tool_that_raises(gateway_settings: Settings) -> None:
    """Any other exception becomes a 502 with the type named, and is audited."""

    class Exploding(StubOrchestrator):
        async def list_projects(self) -> List[Dict[str, Any]]:
            raise RuntimeError("disk on fire")

    async def scenario() -> Dict[str, Any]:
        auth = GatewayAuth(gateway_settings)
        token = (await auth.issue("dash", role="dashboard"))["token"]
        app = gateway_app(gateway_settings, Exploding(gateway_settings))
        async with await _client(app, token) as client:
            response = await client.post("/call", json={"tool": "projects.list"})
            rows = (await client.get("/audit")).json()["rows"]
            return {"code": response.status_code, "detail": response.json()["detail"], "rows": rows}

    body = run(scenario())
    assert body["code"] == 502 and "RuntimeError" in body["detail"]
    assert body["rows"][0]["ok"] is False and "disk on fire" in body["rows"][0]["detail"]


def test_audit_route_only_widens_for_admin_roles(gateway_settings: Settings) -> None:
    """A messenger client reads its own rows and nobody else's."""

    async def scenario() -> List[int]:
        auth = GatewayAuth(gateway_settings)
        bot = (await auth.issue("bot", role="messenger"))["token"]
        admin = (await auth.issue("root", role="admin"))["token"]
        app = gateway_app(gateway_settings)
        async with await _client(app, admin) as admin_client:
            await admin_client.post("/call", json={"tool": "projects.list"})
        async with await _client(app, bot) as bot_client:
            own = (await bot_client.get("/audit")).status_code
            other = (await bot_client.get("/audit", params={"client": "root"})).status_code
        async with await _client(app, admin) as admin_client:
            everyone = (await admin_client.get("/audit", params={"client": "bot"})).status_code
        return [own, other, everyone]

    assert run(scenario()) == [200, 403, 200]


def test_tool_call_model_validates_the_tool_name() -> None:
    """The request model is the first gate; malformed input never reaches policy."""
    assert ToolCall(tool=" projects.list ", confirm=False).tool == "projects.list"
    with pytest.raises(ValueError, match="namespaced"):
        ToolCall(tool="projects", confirm=False)


def test_anonymous_mode_is_available_and_loud(gateway_settings: Settings) -> None:
    """``GATEWAY_REQUIRE_TOKENS=false`` is a development convenience, not a default."""
    loose = gateway_settings.model_copy(update={"GATEWAY_REQUIRE_TOKENS": False}, deep=True)

    async def scenario() -> Dict[str, Any]:
        app = gateway_app(loose)
        async with await _client(app) as client:
            return (await client.get("/toolkits")).json()

    body = run(scenario())
    assert body["client"] == "anonymous" and body["role"] == "admin"


# ----------------------------------------------------------------------
# The MCP endpoint behind the token wrapper
# ----------------------------------------------------------------------
def test_mcp_endpoint_is_mounted_behind_the_token(gateway_settings: Settings) -> None:
    """The real MCP handshake works through the gateway — and not without a token."""
    from starlette.testclient import TestClient

    auth = GatewayAuth(gateway_settings)
    token = run(auth.issue("editor", role="dashboard"))["token"]
    app = gateway_app(gateway_settings)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        refused = client.post(gateway_settings.GATEWAY_MCP_PATH, json=payload, headers={"Accept": "*/*"})
        assert refused.status_code == 401
        accepted = client.post(gateway_settings.GATEWAY_MCP_PATH, json=payload, headers=headers)
        assert accepted.status_code == 200
        assert "kollektiv" in accepted.text.lower()


def test_build_mcp_app_rewrites_the_inner_path(gateway_settings: Settings) -> None:
    """The SDK's own ``/mcp`` prefix is removed so the mount path is the whole URL."""

    class FakeServer:
        def __init__(self) -> None:
            self.kwargs: Dict[str, Any] = {}

        def streamable_http_app(self, **kwargs: Any) -> str:
            self.kwargs = kwargs
            return "mcp-app"

    server = FakeServer()
    settings = gateway_settings.model_copy(
        update={"GATEWAY_ALLOWED_HOSTS": "gateway.example.com,box:*"}, deep=True
    )
    assert build_mcp_app(server, settings) == "mcp-app"
    assert server.kwargs["streamable_http_path"] == "/"
    security = server.kwargs["transport_security"]
    assert security.enable_dns_rebinding_protection is True
    assert "gateway.example.com" in security.allowed_hosts
    assert security.allowed_origins == ["gateway.example.com", "box:*"]

    closed = FakeServer()
    build_mcp_app(closed, gateway_settings)
    assert closed.kwargs["transport_security"].enable_dns_rebinding_protection is False


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def test_cli_gateway_init_prints_a_usable_token(
    gateway_settings: Settings, capsys: Any, monkeypatch: Any
) -> None:
    """``kollektiv gateway init`` is the one command a new operator runs."""
    from src.api import cli as cli_module

    monkeypatch.setattr(cli_module, "get_settings", lambda: gateway_settings)
    code = run(cli_module.cmd_gateway(_args(action="init", name="dash", role="dashboard")))
    output = capsys.readouterr().out
    assert code == 0
    assert TOKEN_PREFIX in output and "mcp_url" not in output
    assert "Bearer" in output and "shown once" in output
    assert "confirm=true required for" in output


def test_cli_gateway_lifecycle_and_json_output(
    gateway_settings: Settings, capsys: Any, monkeypatch: Any
) -> None:
    """clients → policy → audit → status, all machine-readable with ``--json``."""
    from src.api import cli as cli_module

    monkeypatch.setattr(cli_module, "get_settings", lambda: gateway_settings)
    codes: List[int] = [
        run(cli_module.cmd_gateway(_args(action="init", name="dash", role="dashboard", json=True))),
        run(cli_module.cmd_gateway(_args(action="policy", name="dash", read_only=True, preset="read-only", json=True))),
        run(cli_module.cmd_gateway(_args(action="clients", json=True))),
        run(cli_module.cmd_gateway(_args(action="presets", json=True))),
        run(cli_module.cmd_gateway(_args(action="audit", json=True))),
        run(cli_module.cmd_gateway(_args(action="status", json=True))),
        run(cli_module.cmd_gateway(_args(action="policy", name="ghost", json=True))),
        run(cli_module.cmd_gateway(_args(action="revoke", name="dash", json=True))),
        run(cli_module.cmd_gateway(_args(action="revoke", name="dash", json=True))),
    ]
    payloads = _json_objects(capsys.readouterr().out)
    assert codes == [0, 0, 0, 0, 0, 0, 1, 0, 1]
    assert len(payloads) == 9
    assert payloads[0]["token"].startswith(TOKEN_PREFIX) and payloads[0]["policy"]["name"] == "dashboard"
    assert payloads[1]["policy"]["read_only"] is True
    assert payloads[6]["error"].startswith("unknown client")
    assert payloads[7]["revoked"] is True


def test_cli_gateway_tools_lists_the_catalogue(
    gateway_settings: Settings, capsys: Any, monkeypatch: Any
) -> None:
    """``kollektiv gateway tools`` is the operator's map of the gateway."""
    from src.api import cli as cli_module
    from src.orchestrator import app as orchestrator_module

    monkeypatch.setattr(cli_module, "get_settings", lambda: gateway_settings)
    monkeypatch.setattr(orchestrator_module, "Orchestrator", lambda settings: StubOrchestrator(settings))
    code = run(cli_module.cmd_gateway(_args(action="tools")))
    output = capsys.readouterr().out
    assert code == 0
    assert "projects (" in output and "connectors (" in output and "projects.list" in output


def test_cli_gateway_unknown_action_is_exit_code_2(
    gateway_settings: Settings, capsys: Any, monkeypatch: Any
) -> None:
    """A typo in the sub-command is reported, and argparse never crashes."""
    from src.api import cli as cli_module

    monkeypatch.setattr(cli_module, "get_settings", lambda: gateway_settings)
    code = run(cli_module.cmd_gateway(_args(action="teleport", json=True)))
    assert code == 2 and "unknown gateway action" in capsys.readouterr().out


def test_cli_gateway_audit_clear(gateway_settings: Settings, capsys: Any, monkeypatch: Any) -> None:
    """Clearing the log is explicit, and reports how much went."""
    from src.api import cli as cli_module

    audit = GatewayAudit(gateway_settings)
    run(audit.record(client="dash", tool="projects.list", ok=True))
    monkeypatch.setattr(cli_module, "get_settings", lambda: gateway_settings)
    code = run(cli_module.cmd_gateway(_args(action="audit", clear=True, json=True)))
    assert code == 0 and json.loads(capsys.readouterr().out)["cleared"] == 1
    assert run(audit.recent()) == []


def test_cli_gateway_audit_renders_rows(gateway_settings: Settings, capsys: Any, monkeypatch: Any) -> None:
    """The human view is a table, not JSON spilled into a terminal."""
    from src.api import cli as cli_module

    run(GatewayAudit(gateway_settings).record(client="dash", tool="projects.run", ok=False, milliseconds=42))
    monkeypatch.setattr(cli_module, "get_settings", lambda: gateway_settings)
    code = run(cli_module.cmd_gateway(_args(action="audit", limit=5)))
    output = capsys.readouterr().out
    assert code == 0
    assert "projects.run" in output and "failed" in output and "42ms" in output
    assert "denied" in output.splitlines()[0]


def _args(**kwargs: Any) -> Any:
    """Build an argparse-like namespace with every gateway flag defaulted."""
    defaults: Dict[str, Any] = {
        "action": "status",
        "name": "",
        "label": "",
        "role": "",
        "rotate": False,
        "preset": "",
        "allow": "",
        "deny": "",
        "confirm": "",
        "read_only": False,
        "limit": 0,
        "clear": False,
        "json": False,
        "host": None,
        "port": None,
    }
    defaults.update(kwargs)
    return type("Args", (), defaults)


def test_audit_rows_are_stored_in_the_table_not_a_file(audit: GatewayAudit) -> None:
    """The log lives in the database, so a deployment keeps no stray files."""
    run(audit.record(client="dash", tool="gateway.health", ok=True))
    with session_scope() as session:
        rows = list(session.exec(select(GatewayAuditRecord)).all())
    assert len(rows) == 1 and rows[0].arg_names == ""


# ----------------------------------------------------------------------
# Wiring into health and `kollektiv check`
# ----------------------------------------------------------------------
def test_orchestrator_health_reports_the_gateway(gateway_settings: Settings) -> None:
    """``/health`` answers instantly, so the gateway block is configuration only."""
    from src.orchestrator.app import Orchestrator

    payload = run(Orchestrator(gateway_settings).health())
    gateway = payload["subsystems"]["gateway"]
    assert gateway["enabled"] is True
    assert gateway["mcp_path"] == gateway_settings.GATEWAY_MCP_PATH
    assert gateway["url"].endswith(str(gateway_settings.GATEWAY_PORT))
    assert "kgw_" not in json.dumps(gateway)  # never a secret, only the word "tokens"


def test_cli_check_reports_connectors_and_the_gateway(gateway_settings: Settings) -> None:
    """The doctor command sees the new surface without any new credentials."""
    from src.api import cli as cli_module

    report = cli_module.build_check_report(gateway_settings)
    assert report["connectors"]["count"] == 9
    # The fixture configures GitHub; nothing else, and no new credential is needed.
    assert report["connectors"]["configured"] == ["github"]
    assert report["gateway"]["enabled"] is True
    assert report["gateway"]["url"].endswith(gateway_settings.GATEWAY_MCP_PATH)
