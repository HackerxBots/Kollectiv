"""Tests for the worker layer (bring your own key): ``ArenaClient``, ``AgentPool`` and sessions.

All HTTP is mocked. Keys come from the constructor, an environment variable or
the encrypted token store; none of them ever travels anywhere but the provider.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, cast

import httpx
import pytest

from src.agents.agent_pool import AgentPool, AgentTaskError
from src.agents.arena_client import ArenaClient
from src.agents.providers import PROVIDER_PRESETS, resolve_provider
from src.agents.session_manager import SessionManager
from src.utils.errors import (
    ArenaError,
    ArenaTransientError,
    AuthenticationError,
    ConfigurationError,
    RateLimitError,
)
from src.utils.token_store import TokenStore


def mock_client(handler: Any, base: str = "https://agents.example.com/v1") -> httpx.AsyncClient:
    """An HTTP client whose every request goes through ``handler``."""
    return httpx.AsyncClient(base_url=base, transport=httpx.MockTransport(handler))


def chat_reply(text: str = "Hello from the agent") -> httpx.Response:
    """A minimal OpenAI-style chat completion."""
    return httpx.Response(
        200,
        json={"id": "cmpl-1", "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}]},
    )


# ----------------------------------------------------------------------
# Providers
# ----------------------------------------------------------------------
def test_provider_presets_cover_hosted_and_local_options() -> None:
    """Hosted providers need a key, local ones do not, and every endpoint is https (or local)."""
    for name in ("deepseek", "groq", "openrouter", "openai", "gemini", "mistral", "together"):
        assert PROVIDER_PRESETS[name]["kind"] == "key"
        assert PROVIDER_PRESETS[name]["base_url"].startswith("https://")
        assert PROVIDER_PRESETS[name]["env"].endswith("_API_KEY")
    for name in ("ollama", "lmstudio"):
        assert PROVIDER_PRESETS[name]["kind"] == "local"
        assert PROVIDER_PRESETS[name]["env"] == ""
    assert PROVIDER_PRESETS["custom"]["base_url"] == ""


def test_resolve_provider_is_case_insensitive_and_strict() -> None:
    """Unknown names fail with the full list of choices."""
    assert resolve_provider("DeepSeek")["label"] == "DeepSeek"
    with pytest.raises(ConfigurationError) as excinfo:
        resolve_provider("arena")
    assert "deepseek" in str(excinfo.value)


# ----------------------------------------------------------------------
# ArenaClient: requests
# ----------------------------------------------------------------------
async def test_transient_failures_are_retried_with_backoff(settings: Any, monkeypatch: Any) -> None:
    """Two 503s then a success: the worker retries and the task still completes."""
    monkeypatch.setenv("KOLLEKTIV_RETRY_BASE_DELAY", "0")
    monkeypatch.setenv("KOLLEKTIV_RETRY_MAX_DELAY", "0")
    calls: List[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, text="busy")
        return chat_reply("finally")

    agent = ArenaClient({"provider": "groq"}, settings=settings, client=mock_client(flaky), api_key="k")
    assert await agent.send_prompt("hi") == "finally"
    assert len(calls) == 3


async def test_retries_stop_after_three_attempts(settings: Any, monkeypatch: Any) -> None:
    """A worker that stays down gets exactly three attempts, then the error surfaces."""
    monkeypatch.setenv("KOLLEKTIV_RETRY_BASE_DELAY", "0")
    monkeypatch.setenv("KOLLEKTIV_RETRY_MAX_DELAY", "0")
    calls: List[int] = []

    def down(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(502, text="bad gateway")

    agent = ArenaClient({"provider": "groq"}, settings=settings, client=mock_client(down), api_key="k")
    with pytest.raises(ArenaTransientError):
        await agent.send_prompt("hi")
    assert len(calls) == 3


async def test_rate_limits_and_rejected_keys_are_not_retried(settings: Any, monkeypatch: Any) -> None:
    """429 goes to the cooldown and the pool; 401 needs a new key. Neither is retried here."""
    monkeypatch.setenv("KOLLEKTIV_RETRY_BASE_DELAY", "0")
    monkeypatch.setenv("KOLLEKTIV_RETRY_MAX_DELAY", "0")
    calls: List[int] = []

    def limited(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "7"}, text="slow down")

    agent = ArenaClient({"provider": "groq"}, settings=settings, client=mock_client(limited), api_key="k")
    with pytest.raises(RateLimitError):
        await agent.send_prompt("hi")
    assert len(calls) == 1

    def unauthorised(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, text="bad key")

    calls.clear()
    agent = ArenaClient({"provider": "groq"}, settings=settings, client=mock_client(unauthorised), api_key="k")
    with pytest.raises(AuthenticationError):
        await agent.send_prompt("hi")
    assert len(calls) == 1


async def test_send_prompt_posts_chat_completion_with_the_key(settings: Any) -> None:
    """The request goes to <base>/chat/completions with the provider's model and a bearer key."""
    seen: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return chat_reply("ok")

    agent = ArenaClient(
        {"name": "Vega", "provider": "deepseek"},
        settings=settings,
        client=mock_client(handler, base="https://api.deepseek.com/v1"),
        api_key="sk-test-123",
    )
    assert await agent.send_prompt("Plan the work", system_prompt="be brief") == "ok"
    request = seen[0]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-test-123"
    body = json.loads(request.content)
    assert body["model"] == "deepseek-chat"
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    assert body["messages"][-1] == {"role": "user", "content": "Plan the work"}
    assert agent.tasks_done == 1 and agent.last_error == ""


async def test_content_arrays_are_flattened_to_text(settings: Any) -> None:
    """Providers that return content parts still give plain text."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": [{"type": "text", "text": "part-a "}, {"text": "part-b"}]}}]},
        )

    agent = ArenaClient({"provider": "groq"}, settings=settings, client=mock_client(handler), api_key="gsk-1")
    assert await agent.send_prompt("hi") == "part-a part-b"


async def test_rate_limit_sets_a_cooldown(settings: Any) -> None:
    """A 429 raises RateLimitError and parks the worker for the Retry-After window."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "42"}, json={"error": "slow down"})

    agent = ArenaClient({"provider": "groq"}, settings=settings, client=mock_client(handler), api_key="gsk-1")
    with pytest.raises(RateLimitError) as excinfo:
        await agent.send_prompt("hi")
    assert excinfo.value.retry_after == 42
    assert agent.is_rate_limited() is True
    assert agent.stats()["status"] == "rate_limited"


async def test_server_errors_are_transient_and_client_errors_are_not(settings: Any, monkeypatch: Any) -> None:
    """5xx is retryable (ArenaTransientError); 4xx is a permanent ArenaError."""
    monkeypatch.setenv("KOLLEKTIV_RETRY_BASE_DELAY", "0")
    monkeypatch.setenv("KOLLEKTIV_RETRY_MAX_DELAY", "0")

    def server_down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="busy")

    def bad_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "nope"})

    transient = ArenaClient({"provider": "groq"}, settings=settings, client=mock_client(server_down), api_key="k")
    with pytest.raises(ArenaTransientError):
        await transient.send_prompt("hi")

    permanent = ArenaClient({"provider": "groq"}, settings=settings, client=mock_client(bad_request), api_key="k")
    with pytest.raises(ArenaError) as excinfo:
        await permanent.send_prompt("hi")
    assert not isinstance(excinfo.value, ArenaTransientError)


async def test_a_rejected_key_is_forgotten_so_a_fixed_key_is_read_again(settings: Any, monkeypatch: Any) -> None:
    """401 clears the cached key; the next authenticate() reads the environment again."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid key"})

    monkeypatch.setenv("TEST_WORKER_KEY", "sk-old")
    agent = ArenaClient(
        {"provider": "groq", "api_key_env": "TEST_WORKER_KEY"}, settings=settings, client=mock_client(handler)
    )
    with pytest.raises(AuthenticationError) as excinfo:
        await agent.send_prompt("hi")
    assert "kollektiv login --provider groq" in str(excinfo.value)
    assert agent.api_key == "" and agent.authenticated is False

    monkeypatch.setenv("TEST_WORKER_KEY", "sk-fixed")
    assert await agent.authenticate() == "sk-fixed"


# ----------------------------------------------------------------------
# ArenaClient: where the key comes from
# ----------------------------------------------------------------------
async def test_a_hosted_worker_without_a_key_says_how_to_fix_it(settings: Any, monkeypatch: Any, db_engine: Any) -> None:
    """No key in the store and no environment variable is an explicit, actionable error."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    agent = ArenaClient(
        {"name": "Vega", "provider": "deepseek", "account_id": "vega"},
        settings=settings,
        token_store=TokenStore(settings.fernet_secret, engine=db_engine),
    )
    with pytest.raises(AuthenticationError) as excinfo:
        await agent.authenticate()
    message = str(excinfo.value)
    assert "kollektiv login --provider deepseek" in message and "DEEPSEEK_API_KEY" in message


async def test_key_from_an_environment_variable(settings: Any, monkeypatch: Any) -> None:
    """``api_key_env`` names the variable; the key is read at call time and never stored."""
    monkeypatch.setenv("VEGA_GROQ_KEY", "gsk-from-env")
    agent = ArenaClient({"provider": "groq", "api_key_env": "VEGA_GROQ_KEY"}, settings=settings)
    assert await agent.authenticate() == "gsk-from-env"
    assert agent.key_source == "env"


async def test_key_from_the_encrypted_store_is_never_plaintext_at_rest(settings: Any, db_engine: Any) -> None:
    """A key saved with ``kollektiv login`` is read back, and the database holds only ciphertext."""
    store = TokenStore(settings.fernet_secret, engine=db_engine)
    store.save_token("groq", "vega", {"api_key": "gsk-stored-secret", "provider": "groq"})
    agent = ArenaClient(
        {"name": "Vega", "provider": "groq", "account_id": "vega"},
        settings=settings,
        token_store=TokenStore(settings.fernet_secret, engine=db_engine),
    )
    assert await agent.authenticate() == "gsk-stored-secret"
    assert agent.key_source == "store"
    with db_engine.connect() as connection:
        raw = connection.exec_driver_sql("SELECT payload FROM token_records").fetchall()
    assert raw and "gsk-stored-secret" not in str(raw)


async def test_local_providers_run_without_any_key(settings: Any) -> None:
    """Ollama and LM Studio need no key, so no Authorization header is sent."""
    seen: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return chat_reply("local answer")

    agent = ArenaClient({"name": "Terra", "provider": "ollama"}, settings=settings, client=mock_client(handler, "http://127.0.0.1:11434/v1"))
    assert agent.is_authenticated() is True
    assert await agent.send_prompt("hi") == "local answer"
    assert "Authorization" not in seen[0].headers


async def test_a_custom_worker_needs_a_base_url(settings: Any) -> None:
    """A worker with no endpoint is a configuration error, not a crash later."""
    agent = ArenaClient({"name": "Nowhere", "provider": "custom"}, settings=settings)
    with pytest.raises(ConfigurationError) as excinfo:
        await agent.authenticate()
    assert "base_url" in str(excinfo.value)


async def test_reset_session_picks_up_a_rotated_key(settings: Any, monkeypatch: Any) -> None:
    """After a key is rotated, reset_session() loads the new one without a restart."""
    monkeypatch.setenv("ROTATING_KEY", "sk-one")
    agent = ArenaClient({"provider": "openai", "api_key_env": "ROTATING_KEY"}, settings=settings)
    await agent.authenticate()
    monkeypatch.setenv("ROTATING_KEY", "sk-two")
    assert await agent.reset_session() is True
    assert agent.api_key == "sk-two"


async def test_status_probe_checks_the_models_route(settings: Any) -> None:
    """``get_session_status(probe=True)`` asks /models and reports the answer."""
    seen: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"data": []})

    agent = ArenaClient({"provider": "groq"}, settings=settings, client=mock_client(handler), api_key="gsk-1")
    status = await agent.get_session_status(probe=True)
    assert seen == ["/openai/v1/models"]  # Groq serves its OpenAI-compatible API under /openai/v1
    assert status["probed"] is True and status["http_status"] == 200
    assert status["alive"] is True and status["key_source"] == "argument"


async def test_is_ready_is_false_while_rate_limited(settings: Any) -> None:
    """A worker in cooldown is not offered new work."""
    agent = ArenaClient({"provider": "ollama"}, settings=settings)
    assert await agent.is_ready() is True
    agent.apply_rate_limit(60)
    assert await agent.is_ready() is False


# ----------------------------------------------------------------------
# AgentPool
# ----------------------------------------------------------------------
def factory_for(handler: Any, api_key: str = "sk-pool") -> Any:
    """A client factory whose workers all talk to ``handler``."""

    def factory(account: Any, cfg: Any) -> ArenaClient:
        data = account if isinstance(account, dict) else account.model_dump()
        return ArenaClient(data, settings=cfg, client=mock_client(handler), api_key=api_key)

    return factory


async def test_pool_initialises_named_workers_and_reports_them(settings: Any) -> None:
    """Named workers keep their names; the pool reports every one of them."""
    accounts = [
        {"name": "Vega", "provider": "ollama"},
        {"name": "Terra", "provider": "ollama", "model": "qwen2.5-coder:14b"},
    ]
    pool = AgentPool(accounts, settings=settings, client_factory=factory_for(lambda r: chat_reply()))
    report = await pool.initialize()
    assert report["agents"] == 2 and report["ready"] == 2
    statuses = await pool.get_pool_status()
    assert {status["label"] for status in statuses} == {"Vega", "Terra"}
    assert all(status["status"] == "idle" for status in pool.snapshot())
    await pool.close()


async def test_pool_assign_task_builds_a_prompt_and_returns_the_output(settings: Any) -> None:
    """A task is sent to a worker and its reply comes back as the task output."""
    pool = AgentPool([{"name": "Vega", "provider": "ollama"}], settings=settings,
                     client_factory=factory_for(lambda r: chat_reply("def add(a, b): return a + b")))
    await pool.initialize()
    result = await pool.assign_task({"id": "t1", "title": "Write add()"}, context="repo is empty")
    assert "def add" in str(result)
    await pool.close()


async def test_pool_falls_back_to_a_second_worker(settings: Any) -> None:
    """When one worker fails transiently, the task goes to the next worker."""

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["model"] == "broken-model":
            return httpx.Response(500, text="overloaded")
        return chat_reply("recovered")

    accounts = [
        {"name": "Broken", "provider": "ollama", "model": "broken-model"},
        {"name": "Healthy", "provider": "ollama", "model": "good-model"},
    ]
    pool = AgentPool(accounts, settings=settings, client_factory=factory_for(handler))
    await pool.initialize()
    result = await pool.assign_task({"id": "t2", "title": "Recover"}, context="", max_attempts=3)
    assert "recovered" in str(result)
    await pool.close()


async def test_pool_raises_with_per_worker_detail_when_all_fail(settings: Any) -> None:
    """Exhausting the pool raises AgentTaskError naming the task and each failure."""
    pool = AgentPool([{"name": "Only", "provider": "ollama"}], settings=settings,
                     client_factory=factory_for(lambda r: httpx.Response(400, json={"error": "nope"})))
    await pool.initialize()
    with pytest.raises(AgentTaskError) as excinfo:
        await pool.assign_task({"id": "t9", "title": "Doomed"}, context="", max_attempts=2)
    assert excinfo.value.details["task_id"] == "t9"
    assert excinfo.value.details["errors"]
    await pool.close()


async def test_empty_pool_is_a_configuration_error(settings: Any) -> None:
    """No workers means a clear message that names the fix."""
    pool = AgentPool([], settings=settings)
    with pytest.raises(ConfigurationError) as excinfo:
        await pool.assign_task({"id": "t0", "title": "Nobody home"}, context="")
    assert "kollektiv login" in str(excinfo.value)


async def test_session_manager_refreshes_unhealthy_agents(db_engine: Any) -> None:
    """The maintenance pass resets only unhealthy agents."""
    from tests.conftest import FakeAgent, FakeAgentPool

    healthy = FakeAgent("healthy")
    broken = FakeAgent("broken")
    resets: List[str] = []

    async def fake_status(probe: bool = False) -> List[Dict[str, Any]]:
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

    manager = SessionManager(cast(Any, pool), interval_seconds=1)
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

    manager = SessionManager(cast(Any, FakeAgentPool([FakeAgent("a")])), interval_seconds=3600)
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

    manager = SessionManager(cast(Any, pool), interval_seconds=1)
    try:
        assert await manager.ensure_ready_sessions(minimum=1) == 1
        assert calls["reset"] >= 1
    finally:
        await manager.stop()
