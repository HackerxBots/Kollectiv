"""Tests for the worker agent layer: ``ArenaClient``, ``AgentPool`` and sessions.

All HTTP is mocked; the tests cover both request shapes (OpenAI-compatible and
custom), rate limiting, retries across agents, health reporting and the
periodic session manager.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List

import httpx
import pytest

from src.agents.agent_pool import AgentPool, AgentTaskError
from src.agents.arena_client import ArenaClient
from src.agents.session_manager import SessionManager
from src.utils.errors import ArenaError, AuthenticationError, ConfigurationError, RateLimitError


# ----------------------------------------------------------------------
# ArenaClient
# ----------------------------------------------------------------------
@pytest.fixture()
def openai_router() -> Any:
    """A router answering OpenAI-style chat completions."""
    calls: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        calls.append(payload)
        return httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "model": payload.get("model", "test"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello from the agent"}}],
                "usage": {"total_tokens": 12},
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://agents.example.com/v1", transport=transport)
    client.test_calls = calls  # type: ignore[attr-defined]
    return client


async def test_send_prompt_openai_style(settings: Any, openai_router: Any) -> None:
    """An OpenAI-compatible endpoint returns the assistant message."""
    agent = ArenaClient(
        {"email": "w@example.com", "session_token": "tok", "base_url": "https://agents.example.com/v1",
         "model": "test-model"},
        settings=settings,
        client=openai_router,
    )
    try:
        reply = await agent.send_prompt("Write a function", system_prompt="be brief")
        assert reply == "Hello from the agent"
        sent = openai_router.test_calls[-1]
        assert sent["model"] == "test-model"
        assert sent["messages"][0]["role"] == "system"
        assert sent["messages"][1]["content"] == "Write a function"
        assert openai_router.test_calls[0] is sent
        assert agent.tasks_done == 1
        assert agent.busy is False
    finally:
        pass  # the injected client is owned by the test


async def test_send_prompt_custom_style(settings: Any) -> None:
    """A bespoke endpoint receives the Kollektiv prompt envelope."""
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"response": "custom reply", "session": "s-1"})

    client = httpx.AsyncClient(base_url="https://arena.example.com", transport=httpx.MockTransport(handler))
    custom_settings = settings.model_copy(update={"ARENA_CHAT_PATH": "/api/chat"}, deep=True)
    agent = ArenaClient(
        {"email": "w@example.com", "session_token": "tok", "api_style": "custom"},
        settings=custom_settings,
        client=client,
    )
    reply = await agent.send_prompt("Plan the work", use_agent_mode=True)
    assert reply == "custom reply"
    assert captured["prompt"] == "Plan the work"
    assert captured["agent_mode"] is True
    assert captured["stream"] is False


async def test_send_prompt_handles_content_arrays(settings: Any) -> None:
    """Multi-part content arrays are concatenated."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": [{"type": "text", "text": "part one "}, {"text": "part two"}]}}]},
        )

    client = httpx.AsyncClient(base_url="https://agents.example.com/v1", transport=httpx.MockTransport(handler))
    agent = ArenaClient({"email": "w@e.com", "session_token": "t", "base_url": "https://agents.example.com/v1"},
                        settings=settings, client=client)
    assert await agent.send_prompt("go") == "part one part two"


async def test_rate_limit_sets_cooldown(settings: Any) -> None:
    """A 429 puts the agent into cooldown and is not retried immediately."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(429, json={"error": "slow down"}, headers={"Retry-After": "42"})

    client = httpx.AsyncClient(base_url="https://agents.example.com/v1", transport=httpx.MockTransport(handler))
    agent = ArenaClient({"email": "w@e.com", "session_token": "t", "base_url": "https://agents.example.com/v1"},
                        settings=settings, client=client)

    with pytest.raises(RateLimitError) as excinfo:
        await agent.send_prompt("go")
    assert excinfo.value.retry_after == 42.0
    assert agent.is_rate_limited() is True
    assert agent.cooldown_until > time.time()

    # A second call short-circuits while the cooldown is active.
    with pytest.raises(RateLimitError):
        await agent.send_prompt("go again")
    assert attempts["count"] == 1


async def test_five_xx_is_retried_then_succeeds(settings: Any) -> None:
    """Transient 5xx responses are retried with backoff."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "recovered"}}]})

    client = httpx.AsyncClient(base_url="https://agents.example.com/v1", transport=httpx.MockTransport(handler))
    fast = settings.model_copy(update={"RETRY_BASE_DELAY": 0.01, "RETRY_MAX_DELAY": 0.05}, deep=True)
    agent = ArenaClient({"email": "w@e.com", "session_token": "t", "base_url": "https://agents.example.com/v1"},
                        settings=fast, client=client)
    assert await agent.send_prompt("go") == "recovered"
    assert attempts["count"] == 3


async def test_client_error_is_permanent(settings: Any) -> None:
    """A 400 response is not retried and surfaces as an ArenaError."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(400, json={"error": "bad prompt"})

    client = httpx.AsyncClient(base_url="https://agents.example.com/v1", transport=httpx.MockTransport(handler))
    agent = ArenaClient({"email": "w@e.com", "session_token": "t", "base_url": "https://agents.example.com/v1"},
                        settings=settings, client=client)
    with pytest.raises(ArenaError):
        await agent.send_prompt("go")
    assert attempts["count"] == 1
    assert agent.tasks_failed == 1


async def test_login_flow_stores_session_token(settings: Any, db_engine: Any) -> None:
    """Email/password login stores the returned session token encrypted."""
    calls: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/api/auth/login"):
            return httpx.Response(200, json={"session_token": "session-abc", "expires_in": 7200})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = httpx.AsyncClient(base_url="https://arena.example.com", transport=httpx.MockTransport(handler))
    agent = ArenaClient(
        {"email": "w@example.com", "password": "secret"},
        settings=settings.model_copy(update={"ARENA_CHAT_PATH": "/api/chat"}, deep=True),
        client=client,
    )
    token = await agent.authenticate()
    assert token == "session-abc"
    assert agent.is_authenticated() is True
    assert calls == ["/api/auth/login"]

    from src.utils.token_store import SERVICE_ARENA, TokenStore

    stored = TokenStore(settings.fernet_secret).get_token(SERVICE_ARENA, agent.account_id)
    assert stored["session_token"] == "session-abc"


async def test_login_missing_endpoint_is_explicit(settings: Any) -> None:
    """A 404 on the login route explains exactly what to configure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "no such route"})

    client = httpx.AsyncClient(base_url="https://arena.example.com", transport=httpx.MockTransport(handler))
    agent = ArenaClient(
        {"email": "w@example.com", "password": "secret"},
        settings=settings.model_copy(update={"ARENA_CHAT_PATH": "/api/chat"}, deep=True),
        client=client,
    )
    with pytest.raises(AuthenticationError) as excinfo:
        await agent.authenticate()
    assert "ARENA_LOGIN_PATH" in str(excinfo.value) or "session_token" in str(excinfo.value)


async def test_authenticate_requires_credentials_when_custom_endpoint(settings: Any) -> None:
    """A custom endpoint with no token and no login route is a config error."""
    client = httpx.AsyncClient(base_url="https://arena.example.com", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    agent = ArenaClient(
        {"email": "w@example.com"},
        settings=settings.model_copy(update={"ARENA_CHAT_PATH": "/api/chat"}, deep=True),
        client=client,
    )
    with pytest.raises(AuthenticationError):
        await agent.authenticate()


async def test_get_session_status_and_reset(settings: Any, db_engine: Any) -> None:
    """Status reports health, and reset re-authenticates."""
    logged_in = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/auth/login"):
            logged_in["count"] += 1
            return httpx.Response(200, json={"session_token": f"token-{logged_in['count']}"})
        if request.url.path == "/models":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = httpx.AsyncClient(base_url="https://arena.example.com", transport=httpx.MockTransport(handler))
    agent = ArenaClient(
        {"email": "w@example.com", "password": "pw", "api_style": "custom"},
        settings=settings.model_copy(update={"ARENA_CHAT_PATH": "/api/chat"}, deep=True),
        client=client,
    )
    await agent.authenticate()
    status = await agent.get_session_status(probe=True)
    assert status["alive"] is True and status["token_valid"] is True
    assert status["probed"] is True and status["http_status"] == 200

    assert await agent.reset_session() is True
    assert agent.session_token == "token-2"
    assert logged_in["count"] == 2


async def test_is_ready_false_when_rate_limited(settings: Any) -> None:
    """``is_ready`` reflects busy and rate-limited states."""
    agent = ArenaClient({"email": "w@e.com", "session_token": "t"}, settings=settings)
    assert await agent.is_ready() is True
    agent.busy = True
    assert await agent.is_ready() is False
    agent.busy = False
    agent.apply_rate_limit(60)
    assert await agent.is_ready() is False
    assert agent.stats()["status"] == "rate_limited"


# ----------------------------------------------------------------------
# AgentPool
# ----------------------------------------------------------------------
async def test_pool_initialises_and_reports_status(settings: Any, agent_settings: Any) -> None:
    """Accounts from settings become authenticated workers."""
    created: List[ArenaClient] = []

    def factory(account: Any, cfg: Any) -> ArenaClient:
        agent = ArenaClient.from_config(account, settings=cfg)
        created.append(agent)
        return agent

    pool = AgentPool(agent_settings.arena_account_list(), settings=agent_settings, client_factory=factory)
    report = await pool.initialize()
    assert report["agents"] == 2
    assert report["ready"] == 2
    statuses = await pool.get_pool_status()
    assert {status["account_id"] for status in statuses} == {agent.account_id for agent in created}
    assert all(status["status"] == "idle" for status in statuses)
    await pool.close()


async def test_pool_assign_task_builds_prompt_and_returns_output(settings: Any) -> None:
    """The dispatch prompt carries the context, task and output contract."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "```python path=a.py\nx=1\n```"}}]})

    def factory(account: Any, cfg: Any) -> ArenaClient:
        client = httpx.AsyncClient(base_url="https://agents.example.com/v1", transport=httpx.MockTransport(handler))
        data = account if isinstance(account, dict) else account.model_dump()
        data["base_url"] = "https://agents.example.com/v1"
        return ArenaClient(data, settings=cfg, client=client)

    pool = AgentPool(
        [{"email": "w@example.com", "session_token": "t"}],
        settings=settings,
        client_factory=factory,
    )
    await pool.initialize()
    output = await pool.assign_task(
        {"id": "t1", "title": "Write the parser", "description": "Parse CSV files"},
        context="## Shared project context\n\n- Project: demo",
    )
    assert "a.py" in output
    stats = await pool.get_pool_status()
    assert stats[0]["tasks_done"] == 1


async def test_pool_falls_back_to_second_agent(settings: Any) -> None:
    """A failing agent is skipped and the task is retried on another one."""
    attempts: List[str] = []

    def factory(account: Any, cfg: Any) -> ArenaClient:
        data = account if isinstance(account, dict) else account.model_dump()
        data["base_url"] = "https://agents.example.com/v1"
        email = data.get("email")

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(email)
            if email == "bad@example.com":
                return httpx.Response(400, json={"error": "cannot help"})
            return httpx.Response(200, json={"choices": [{"message": {"content": "done by good agent"}}]})

        client = httpx.AsyncClient(base_url="https://agents.example.com/v1", transport=httpx.MockTransport(handler))
        return ArenaClient(data, settings=cfg, client=client)

    pool = AgentPool(
        [{"email": "bad@example.com", "session_token": "t"}, {"email": "good@example.com", "session_token": "t"}],
        settings=settings.model_copy(update={"RETRY_BASE_DELAY": 0.01}, deep=True),
        client_factory=factory,
    )
    await pool.initialize()
    output = await pool.assign_task({"id": "t1", "title": "Work"}, context="ctx", max_attempts=2)
    assert output == "done by good agent"
    assert "bad@example.com" in attempts and "good@example.com" in attempts


async def test_pool_raises_when_every_agent_fails(settings: Any) -> None:
    """Exhausting the pool raises AgentTaskError with per-agent detail."""

    def factory(account: Any, cfg: Any) -> ArenaClient:
        data = account if isinstance(account, dict) else account.model_dump()
        data["base_url"] = "https://agents.example.com/v1"
        client = httpx.AsyncClient(
            base_url="https://agents.example.com/v1",
            transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "nope"})),
        )
        return ArenaClient(data, settings=cfg, client=client)

    pool = AgentPool([{"email": "w@example.com", "session_token": "t"}], settings=settings, client_factory=factory)
    await pool.initialize()
    with pytest.raises(AgentTaskError) as excinfo:
        await pool.assign_task({"id": "t9", "title": "Doomed"}, context="", max_attempts=2)
    assert excinfo.value.details["task_id"] == "t9"
    assert excinfo.value.details["errors"]


async def test_pool_raises_configuration_error_when_empty(settings: Any) -> None:
    """An empty pool explains how to configure agents."""
    pool = AgentPool([], settings=settings)
    with pytest.raises(ConfigurationError):
        await pool.assign_task({"id": "t1", "title": "x"})


async def test_pool_assign_many_runs_jobs_concurrently(db_engine: Any) -> None:
    """``assign_many`` returns one result per job and records failures."""
    from tests.conftest import FakeAgent, FakeAgentPool

    pool = FakeAgentPool([FakeAgent("agent-1", ["out-1", "out-2"])])
    results = await pool.assign_many(
        [
            {"task": {"id": "t1", "title": "One"}},
            {"task": {"id": "t2", "title": "Two"}},
        ]
    )
    assert [result["task_id"] for result in results] == ["t1", "t2"]
    assert all(result["success"] for result in results)


async def test_pool_assign_many_with_real_agents(settings: Any) -> None:
    """Two real agents execute two jobs in parallel."""

    def factory(account: Any, cfg: Any) -> ArenaClient:
        data = account if isinstance(account, dict) else account.model_dump()
        data["base_url"] = "https://agents.example.com/v1"

        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.01)
            payload = json.loads(request.content)
            title = payload["messages"][-1]["content"].split("**Title:** ")[1].split("\n")[0]
            return httpx.Response(
                200, json={"choices": [{"message": {"content": f"```python path={title}.py\npass\n```"}}]}
            )

        client = httpx.AsyncClient(
            base_url="https://agents.example.com/v1", transport=httpx.MockTransport(handler)
        )
        return ArenaClient(data, settings=cfg, client=client)

    pool = AgentPool(
        [{"email": "a@example.com", "session_token": "t"}, {"email": "b@example.com", "session_token": "t"}],
        settings=settings,
        client_factory=factory,
    )
    await pool.initialize()
    results = await pool.assign_many(
        [{"task": {"id": "t1", "title": "alpha"}}, {"task": {"id": "t2", "title": "beta"}}]
    )
    assert {result["task_id"] for result in results} == {"t1", "t2"}
    assert all(result["success"] for result in results)


# ----------------------------------------------------------------------
# SessionManager
# ----------------------------------------------------------------------
async def test_session_manager_refreshes_unhealthy_agents(db_engine: Any) -> None:
    """The maintenance pass resets only unhealthy agents."""
    from tests.conftest import FakeAgent, FakeAgentPool

    healthy = FakeAgent("healthy")
    broken = FakeAgent("broken")
    resets: List[str] = []

    async def fake_status(probe: bool = False) -> Dict[str, Any]:
        return [
            {"account_id": "healthy", "alive": True, "token_valid": True},
            {"account_id": "broken", "alive": False, "token_valid": False},
        ]

    async def fake_reset() -> bool:
        resets.append("broken")
        return True

    broken.reset_session = fake_reset  # type: ignore[assignment]
    pool = FakeAgentPool([healthy, broken])
    pool.get_pool_status = fake_status  # type: ignore[assignment]

    manager = SessionManager(pool, interval_seconds=1)
    try:
        summary = await manager.check_and_refresh()
        assert summary["agents"] == 2
        assert summary["unhealthy"] == 1
        assert summary["refreshed"] == 1
        assert resets == ["broken"]
    finally:
        await manager.stop()


async def test_session_manager_start_and_stop(db_engine: Any) -> None:
    """Start/stop toggles the background loop and reports status."""
    from tests.conftest import FakeAgent, FakeAgentPool

    manager = SessionManager(FakeAgentPool([FakeAgent("a")]), interval_seconds=3600)
    assert await manager.start(use_scheduler=False) is True
    assert manager.is_running is True
    status = manager.status()
    assert status["running"] is True and status["backend"] == "asyncio"
    await manager.stop()
    assert manager.is_running is False


async def test_session_manager_ensure_ready_sessions(db_engine: Any) -> None:
    """``ensure_ready_sessions`` forces a refresh when nobody is ready."""
    from tests.conftest import FakeAgent, FakeAgentPool

    agent = FakeAgent("a")
    calls = {"reset": 0}
    ready_state = {"ready": False}

    async def fake_ready() -> bool:
        return ready_state["ready"]

    async def fake_reset() -> bool:
        calls["reset"] += 1
        ready_state["ready"] = True
        return True

    agent.is_ready = fake_ready  # type: ignore[assignment]
    agent.reset_session = fake_reset  # type: ignore[assignment]

    pool = FakeAgentPool([agent])

    async def fake_status(probe: bool = False) -> List[Dict[str, Any]]:
        return [{"account_id": "a", "alive": False, "token_valid": False}]

    pool.get_pool_status = fake_status  # type: ignore[assignment]

    manager = SessionManager(pool, interval_seconds=1)
    try:
        assert await manager.ensure_ready_sessions(minimum=1) == 1
        assert calls["reset"] >= 1
    finally:
        await manager.stop()
