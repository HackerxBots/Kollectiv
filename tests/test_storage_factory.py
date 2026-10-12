"""Storage backend selection from environment variables. No network: the backends are stand-ins."""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from config.settings import Settings
from src.storage import factory, r2_pool

ENV_NAMES = (
    "STORAGE_BACKEND",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_ENDPOINT",
    "R2_ACCOUNTS",
    "TERABOX_ACCOUNTS",
)


@pytest.fixture(autouse=True)
def stand_ins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace both backends with recorders so build_storage only reports its choice."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(factory, "TeraBoxPoolManager", lambda **kw: ("terabox", kw))
    monkeypatch.setattr(r2_pool, "R2Storage", lambda *a, **kw: ("r2", kw))


def _built(monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    settings = Settings()
    return factory.build_storage(settings)


def test_terabox_is_chosen_when_only_terabox_tokens_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    accounts = json.dumps([{"email": "a@example.com", "access_token": "tok"}])
    backend, kwargs = _built(monkeypatch, TERABOX_ACCOUNTS=accounts)
    assert backend == "terabox"
    assert kwargs["settings"].storage_backend == "terabox"


def test_r2_wins_over_terabox_in_auto_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    accounts = json.dumps([{"email": "a@example.com", "access_token": "tok"}])
    backend, _ = _built(
        monkeypatch,
        TERABOX_ACCOUNTS=accounts,
        R2_ACCESS_KEY_ID="k",
        R2_SECRET_ACCESS_KEY="s",
        R2_ENDPOINT="https://example.r2.cloudflarestorage.com",
    )
    assert backend == "r2"


def test_no_credentials_means_degraded_mode_with_an_empty_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    backend, kwargs = _built(monkeypatch)
    assert backend == "terabox"  # the empty pool is the degraded backend
    assert kwargs["accounts"] == []


def test_an_explicit_backend_is_not_overridden_by_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    accounts = json.dumps([{"email": "a@example.com", "access_token": "tok"}])
    backend, _ = _built(monkeypatch, STORAGE_BACKEND="r2", TERABOX_ACCOUNTS=accounts)
    assert backend == "r2"


def test_a_password_alone_does_not_configure_terabox(monkeypatch: pytest.MonkeyPatch) -> None:
    # OAuth tokens only: an account with an email and password but no token is not usable.
    accounts = json.dumps([{"email": "a@example.com", "password": "not-used"}])
    monkeypatch.setenv("TERABOX_ACCOUNTS", accounts)
    assert Settings().storage_backend == "none"
    assert not Settings().is_terabox_configured


def test_the_stand_in_records_the_settings_it_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}

    def fake(**kw: Any) -> str:
        captured.update(kw)
        return "terabox"

    monkeypatch.setattr(factory, "TeraBoxPoolManager", fake)
    _built(monkeypatch, STORAGE_BACKEND="terabox")
    assert isinstance(captured["settings"], Settings)
