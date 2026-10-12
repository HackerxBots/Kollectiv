"""The orchestrator brain can run on the owner's Arena session (BRAIN_PROVIDER=arena)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from config.settings import Settings
from src.orchestrator.brain import OrchestratorBrain
from src.utils.errors import ConfigurationError, RateLimitError


class FakeArena:
    """Stands in for the brain's ArenaClient."""

    def __init__(self, *, authenticated: bool = True, reply: str = "", error: Exception | None = None) -> None:
        self.is_authenticated = authenticated
        self.reply = reply
        self.error = error
        self.prompts: List[Dict[str, Any]] = []

    async def send_prompt(self, prompt: str, **kwargs: Any) -> str:
        self.prompts.append({"prompt": prompt, **kwargs})
        if self.error is not None:
            raise self.error
        return self.reply


class FakeOpenAI:
    """Stands in for the OpenAI-style fallback client."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: List[Dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self.text)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _settings(**overrides: Any) -> Settings:
    base: Dict[str, Any] = {"BRAIN_PROVIDER": "arena", "BRAIN_API_KEY": "", "BRAIN_FALLBACK_API_KEY": ""}
    base.update(overrides)
    return Settings(**base)


def _brain(settings: Settings, arena: FakeArena, fallback: FakeOpenAI | None = None) -> OrchestratorBrain:
    def factory(api_key: str, base_url: str, _settings: Settings) -> Any:
        return fallback if fallback is not None else FakeOpenAI("from fallback")

    brain = OrchestratorBrain(settings=settings, client_factory=factory)
    brain._arena_client = arena  # the session client, injected for the test
    return brain


@pytest.mark.asyncio
async def test_arena_brain_answers_through_the_session() -> None:
    arena = FakeArena(reply="planned")
    brain = _brain(_settings(), arena)
    assert brain.is_configured is True
    assert await brain.complete("split this", system_prompt="be brief") == "planned"
    assert arena.prompts[0]["prompt"] == "split this"
    assert arena.prompts[0]["system_prompt"] == "be brief"
    assert brain.failures == 0


@pytest.mark.asyncio
async def test_arena_brain_without_a_session_is_not_configured() -> None:
    brain = _brain(_settings(), FakeArena(authenticated=False))
    assert brain.is_configured is False
    with pytest.raises(ConfigurationError, match="kollektiv login --provider arena --account brain"):
        await brain.complete("hello")


@pytest.mark.asyncio
async def test_arena_failure_uses_the_fallback_when_one_is_set() -> None:
    fallback = FakeOpenAI("from fallback")
    brain = _brain(
        _settings(BRAIN_FALLBACK_API_KEY="k", BRAIN_FALLBACK_MODEL="deepseek-chat"),
        FakeArena(error=RuntimeError("session expired")),
        fallback,
    )
    assert await brain.complete("hello") == "from fallback"
    assert fallback.calls[0]["model"] == "deepseek-chat"
    assert brain.failures == 1


@pytest.mark.asyncio
async def test_rate_limit_is_reported_as_such_without_fallback() -> None:
    brain = _brain(_settings(), FakeArena(error=RateLimitError("slow down", retry_after=5.0)))
    with pytest.raises(RateLimitError):
        await brain.complete("hello")


def test_arena_brain_sets_a_warning_not_a_missing_key_message() -> None:
    warnings = _settings().config_warnings()
    assert any("BRAIN_PROVIDER=arena" in line for line in warnings)
    assert not any("BRAIN_API_KEY is unset" in line for line in warnings)
