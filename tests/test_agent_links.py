"""Tests for agent names and agent-connector links.

Two product rules are pinned here:

* **Agents have names, and there may be many of them.** Every worker gets a
  stable display name (the operator's when given, a friendly derived one
  otherwise, unique within the pool). The pool is not capped at four.
* **Linking is not calling.** A link is a grant ("Nova may use Slack"); the
  connector's actions stay in the registry, the API, the MCP server and the CLI.
  A connector with no links is open, so a fresh install and every existing
  workflow behave exactly as before; once a connector has links, a call that
  names an agent must come from a linked one.

Everything runs against a real :class:`~src.orchestrator.app.Orchestrator` with an
in-memory database — no network, no credentials.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from config.settings import Settings
from src.agents.agent_pool import AgentPool
from src.agents.names import AGENT_NAMES, assign_names, base_name, unique_name
from src.api.routes import create_app

ACCOUNTS = [
    {"email": f"worker{index}@example.com", "session_token": f"tok-{index}"} for index in range(1, 11)
]


def run(coro: Any) -> Any:
    """Run an awaitable to completion (the suite has no async plugin)."""
    return asyncio.run(coro)


def make_orchestrator(settings: Settings, accounts: int = 3) -> Any:
    """Build a real orchestrator with ``accounts`` worker accounts."""
    from src.orchestrator.app import Orchestrator

    payload = json.dumps(ACCOUNTS[:accounts])
    return Orchestrator(settings.model_copy(update={"ARENA_ACCOUNTS": payload, "AUTO_INIT_DB": True}, deep=True))


# ----------------------------------------------------------------------
# Names
# ----------------------------------------------------------------------
def test_names_are_stable_and_unique() -> None:
    """The same account is always the same name; a pool has no duplicates."""
    first = AgentPool(ACCOUNTS[:6])
    second = AgentPool(ACCOUNTS[:6])
    assert first.names == second.names, "names must not shuffle between restarts"
    assert len(set(first.names.values())) == 6
    for name in first.names.values():
        assert name in AGENT_NAMES, f"{name!r} is not a curated agent name"


def test_operator_names_win_and_duplicates_are_suffixed() -> None:
    """A chosen name is kept; two agents may not share one."""
    class Fake:
        """Minimal agent stand-in for the naming helper."""

        def __init__(self, account_id: str, name: str = "") -> None:
            self.account_id = account_id
            self.name = name

    agents = [Fake("a", "Reviewer"), Fake("b", "Reviewer"), Fake("c", "")]
    names = assign_names(agents)
    assert names["a"] == "Reviewer"
    assert names["b"] == "Reviewer 2", "a duplicate must be disambiguated"
    assert names["c"] == base_name("c")
    assert unique_name("Vega", {"Vega", "Vega 2"}) == "Vega 3"


def test_pool_is_not_capped_at_four() -> None:
    """Ten accounts means ten agents, each with its own name."""
    pool = AgentPool(ACCOUNTS)
    assert pool.size == 10
    assert len(pool.names) == 10
    assert len(set(pool.names.values())) == 10


def test_agent_status_and_health_carry_names(settings: Settings) -> None:
    """The API surface a dashboard reads reports names, not just ids."""
    orchestrator = make_orchestrator(settings, accounts=4)
    statuses = run(orchestrator.get_agents_status())
    assert len(statuses) == 4
    assert all(entry["name"] for entry in statuses), statuses
    assert len({entry["name"] for entry in statuses}) == 4

    payload = run(orchestrator.health())
    names = payload["subsystems"]["agents"]["names"]
    assert len(names) == 4
    assert payload["subsystems"]["agents"]["agents"] == 4


# ----------------------------------------------------------------------
# Links: storage and HTTP surface
# ----------------------------------------------------------------------
@pytest.fixture()
def linked_app(agent_settings: Settings) -> Any:
    """A FastAPI app backed by a real orchestrator with three agents."""
    orchestrator = make_orchestrator(agent_settings, accounts=3)
    return create_app(settings=agent_settings, orchestrator=orchestrator)


@pytest.fixture()
async def api(linked_app: Any) -> Any:
    """An httpx client bound to the real-orchestrator app."""
    transport = httpx.ASGITransport(app=linked_app)
    async with linked_app.router.lifespan_context(linked_app), httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield client


async def first_agent_id(api: Any) -> str:
    """Return the account id of the first worker agent."""
    body = (await api.get("/agents/status")).json()
    return body["agents"][0]["account_id"]


async def test_link_list_unlink_round_trip(api: Any) -> None:
    """The whole grant lifecycle over HTTP."""
    agent_id = await first_agent_id(api)
    assert (await api.get("/links")).json() == {"count": 0, "links": []}

    created = await api.post(
        "/links", json={"agent_id": agent_id, "connector": "slack", "note": "owns release notes"}
    )
    assert created.status_code == 201, created.text
    link = created.json()
    assert link["connector"] == "slack" and link["agent_name"]
    assert link["note"] == "owns release notes"

    listed = (await api.get("/links")).json()
    assert listed["count"] == 1 and listed["links"][0]["link_id"] == link["link_id"]

    removed = await api.delete(f"/links/{link['link_id']}")
    assert removed.status_code == 200 and removed.json()["removed"] is True
    assert (await api.get("/links")).json()["count"] == 0
    assert (await api.delete(f"/links/{link['link_id']}")).status_code == 404


async def test_links_reject_unknown_agents_and_connectors(api: Any) -> None:
    """A grant that does not name something real is a 404, not a silent row."""
    agent_id = await first_agent_id(api)
    assert (await api.post("/links", json={"agent_id": "ghost", "connector": "slack"})).status_code == 404
    assert (await api.post("/links", json={"agent_id": agent_id, "connector": "telepathy"})).status_code == 404


async def test_duplicate_links_are_a_visible_conflict(api: Any) -> None:
    """Linking twice tells the caller, instead of storing a second row."""
    agent_id = await first_agent_id(api)
    body = {"agent_id": agent_id, "connector": "slack"}
    assert (await api.post("/links", json=body)).status_code == 201
    duplicate = await api.post("/links", json=body)
    assert duplicate.status_code == 409
    assert "already linked" in duplicate.json()["detail"]


async def test_connectors_payload_carries_the_links(api: Any) -> None:
    """The connectors list is a linking surface: it names the linked agents."""
    agent_id = await first_agent_id(api)
    await api.post("/links", json={"agent_id": agent_id, "connector": "notion"})
    body = (await api.get("/connectors")).json()
    notion = next(entry for entry in body["connectors"] if entry["name"] == "notion")
    assert [link["agent_id"] for link in notion["linked_agents"]] == [agent_id]
    github = next(entry for entry in body["connectors"] if entry["name"] == "github")
    assert github["linked_agents"] == []
    # The actions still live in the registry payload, untouched by linking.
    assert body["actions"], "connector actions belong to the connector, not the link"


# ----------------------------------------------------------------------
# Links: what they mean
# ----------------------------------------------------------------------
async def test_connectors_are_open_until_someone_links(settings: Settings) -> None:
    """A fresh install needs no grants: no links means every caller may call."""
    orchestrator = make_orchestrator(settings, accounts=2)
    allowed, reason = await orchestrator.access_decision("slack", "whatever-agent")
    assert allowed is True and "open" in reason


async def test_linked_connector_refuses_other_agents(settings: Settings) -> None:
    """Once a connector has links, an unlinked agent is refused — with a reason."""
    orchestrator = make_orchestrator(settings, accounts=3)
    ids = [entry["account_id"] for entry in await orchestrator.get_agents_status()]
    names = {entry["account_id"]: entry["name"] for entry in await orchestrator.get_agents_status()}
    await orchestrator.link_agent(ids[0], "slack")

    allowed, reason = await orchestrator.access_decision("slack", ids[0])
    assert allowed is True
    denied, why = await orchestrator.access_decision("slack", ids[1])
    assert denied is False
    assert names[ids[0]] in why, "the reason names who *is* linked"
    # An operator call names no agent and is not constrained by worker grants.
    operator, operator_reason = await orchestrator.access_decision("slack", "")
    assert operator is True and "operator" in operator_reason


async def test_connector_call_returns_403_for_an_unlinked_agent(api: Any) -> None:
    """The refusal happens before the connector is touched (no network)."""
    body = (await api.get("/agents/status")).json()["agents"]
    linked, other = body[0]["account_id"], body[1]["account_id"]
    await api.post("/links", json={"agent_id": linked, "connector": "slack"})

    response = await api.post(
        "/connectors/slack/call", json={"action": "post_message", "params": {}, "agent_id": other}
    )
    assert response.status_code == 403
    assert "linked" in response.json()["detail"]


async def test_links_survive_a_restart(settings: Settings) -> None:
    """Links are database rows, not memory: a new orchestrator sees them."""
    from src.orchestrator.app import Orchestrator

    scoped = settings.model_copy(
        update={
            "ARENA_ACCOUNTS": json.dumps(ACCOUNTS[:2]),
            "DATABASE_URL": f"sqlite:///{Path(settings.WORKSPACE_DIR).parent / 'links.db'}",
            "AUTO_INIT_DB": True,
        },
        deep=True,
    )
    first = Orchestrator(scoped)
    agent_id = (await first.get_agents_status())[0]["account_id"]
    await first.link_agent(agent_id, "linear", note="planning board")

    second = Orchestrator(scoped)
    links = await second.list_links()
    assert len(links) == 1
    assert links[0]["agent_id"] == agent_id and links[0]["connector"] == "linear"
    assert links[0]["note"] == "planning board"


# ----------------------------------------------------------------------
# CLI and MCP
# ----------------------------------------------------------------------
def test_cli_links_round_trip(agent_settings: Settings, capsys: Any, monkeypatch: Any) -> None:
    """``kollektiv links``, ``link`` and ``unlink`` work end to end."""
    from src.api import cli as cli_module

    settings = agent_settings.model_copy(
        update={"ARENA_ACCOUNTS": json.dumps(ACCOUNTS[:2]), "AUTO_INIT_DB": True}, deep=True
    )
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    monkeypatch.setattr("src.orchestrator.app.get_settings", lambda: settings)

    def args(**kwargs: Any) -> Any:
        """Build an argparse-like namespace with defaults."""
        defaults = {"json": False, "note": "", "agent_id": "", "connector": "", "link_id": ""}
        defaults.update(kwargs)
        return type("Args", (), defaults)()

    assert run(cli_module.cmd_links(args())) == 0
    assert "none" in capsys.readouterr().out

    agent_id = run(_first_cli_agent(settings))
    assert run(cli_module.cmd_link(args(agent_id=agent_id, connector="telegram", note="notifications"))) == 0
    out = capsys.readouterr().out
    assert "linked" in out and "telegram" in out

    assert run(cli_module.cmd_links(args())) == 0
    assert "telegram" in capsys.readouterr().out

    assert run(cli_module.cmd_link(args(agent_id=agent_id, connector="telegram"))) == 1  # duplicate
    assert "already linked" in capsys.readouterr().err

    links = run(_cli_links(settings))
    assert run(cli_module.cmd_unlink(args(link_id=links[0]["link_id"]))) == 0
    assert "unlinked" in capsys.readouterr().out
    assert run(cli_module.cmd_unlink(args(link_id="lnk_missing"))) == 1


async def _first_cli_agent(settings: Settings) -> str:
    """Return the first agent id using the same settings the CLI uses."""
    from src.orchestrator.app import Orchestrator

    return (await Orchestrator(settings).get_agents_status())[0]["account_id"]


async def _cli_links(settings: Settings) -> List[Dict[str, Any]]:
    """Return the stored links, for the CLI test above."""
    from src.orchestrator.app import Orchestrator

    return await Orchestrator(settings).list_links()


async def test_mcp_exposes_the_link_tools(settings: Settings) -> None:
    """An AI client can read and edit grants through the MCP server."""
    from src.api.mcp_server import create_server

    server = create_server(settings=settings)
    names = {tool.name for tool in await server.list_tools()}
    assert {"agent_links", "link_agent", "unlink_agent"} <= names


# ----------------------------------------------------------------------
# More than four agents
# ----------------------------------------------------------------------
async def test_projects_accept_many_agents(api: Any) -> None:
    """The planner accepts a large team; twelve was a typo, not a design."""
    created = await api.post(
        "/projects",
        json={"name": "big team", "description": "Build a docs site with tests", "n_agents": 16},
    )
    assert created.status_code == 201, created.text
    assert created.json()["n_agents"] == 16
