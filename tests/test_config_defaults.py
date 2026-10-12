"""Defaults that have broken a real deployment before. Each test pins one of them.

* The compose file once defaulted ``ENVIRONMENT`` to production, so a ``.env``
  without that line crash-looped the containers.
* The brain once defaulted to a paid provider. The free path is Groq.
* The TeraBox template once suggested storing a password. Tokens only.
"""

from __future__ import annotations

import json
from pathlib import Path

from config.settings import Settings, override_settings

REPO = Path(__file__).resolve().parents[1]


def test_brain_defaults_to_the_groq_free_tier() -> None:
    fields = Settings.model_fields
    assert fields["BRAIN_PROVIDER"].default == "groq"
    assert "groq" in fields["BRAIN_BASE_URL"].default
    assert fields["BRAIN_API_KEY"].default == ""


def test_fallback_brain_is_inert_without_its_own_key() -> None:
    """The fallback runs only when BRAIN_FALLBACK_API_KEY is set, so the default is free."""
    assert Settings.model_fields["BRAIN_FALLBACK_API_KEY"].default == ""


def test_compose_never_defaults_to_production() -> None:
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert "${ENVIRONMENT:-production}" not in compose
    assert compose.count("ENVIRONMENT: ${ENVIRONMENT:-development}") == 2


def test_env_example_terabox_template_has_no_password() -> None:
    line = next(
        raw for raw in (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
        if raw.startswith("TERABOX_ACCOUNTS=")
    )
    assert "password" not in line
    entry = json.loads(line.split("=", 1)[1].strip("'"))[0]
    assert set(entry) == {"email", "access_token", "refresh_token"}


def test_terabox_is_the_storage_backend_when_accounts_are_configured() -> None:
    from src.storage.factory import build_storage
    from src.storage.pool_manager import TeraBoxPoolManager

    accounts = [{"email": "ops@example.com", "access_token": "a" * 20, "refresh_token": "r" * 20}]
    settings = override_settings(STORAGE_BACKEND="auto", TERABOX_ACCOUNTS=json.dumps(accounts))
    pool = build_storage(settings)
    assert isinstance(pool, TeraBoxPoolManager)
    assert pool.is_configured() is True
