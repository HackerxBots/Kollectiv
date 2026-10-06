"""Central configuration for Kollektiv.

Every runtime knob is declared here and loaded from environment variables
and/or a ``.env`` file (see ``.env.example``). Two fields deserve special
attention because they hold JSON encoded lists:

``TERABOX_ACCOUNTS``
    JSON list of ``{"email", "password", "access_token", "refresh_token"}``.

``ARENA_ACCOUNTS``
    JSON list of ``{"email", "password", "session_token"}``.

They are kept as raw strings by pydantic-settings and parsed on demand by
:meth:`Settings.terabox_account_list` / :meth:`Settings.arena_account_list`.
Parsing lazily means a malformed entry produces a clear log line and a
partially usable configuration instead of preventing the process from
starting at all.

Usage::

    from config.settings import get_settings

    settings = get_settings()
    for account in settings.terabox_account_list():
        print(account.email, account.account_id)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

LOGGER = logging.getLogger(__name__)

#: Repository root (the folder containing ``config/`` and ``src/``).
PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

#: Secret used when ``SECRET_KEY`` is left empty. It is *not* safe for
#: production: tokens encrypted with it are readable by anyone holding the
#: source. Kollektiv logs a loud warning when this fallback is in use.
DEFAULT_DEV_SECRET: str = "kollektiv-insecure-development-secret-key"

#: File name of the shared agent memory document stored on TeraBox.
PROJECT_STATE_FILENAME: str = "PROJECT_STATE.md"


def _stable_id(*parts: str) -> str:
    """Return a short, stable, non-reversible identifier for the given parts.

    Args:
        *parts: Values that identify an account (email, name, ...).

    Returns:
        A 16 character hex digest.
    """
    seed = "|".join(part.strip().lower() for part in parts if part) or "default"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


class TeraBoxAccount(BaseModel):
    """One TeraBox account participating in the pooled storage layer.

    Attributes:
        email: Account login (used for display and as an identifier).
        password: Account password. Only needed for interactive flows --
            the API itself authenticates with OAuth tokens.
        access_token: OAuth access token, valid for roughly two days.
        refresh_token: Long lived OAuth refresh token.
        name: Optional friendly label; falls back to the email.
        app_id: Per-account Open Platform app id (overrides the global one).
        app_key: Per-account Open Platform app key/secret.
        quota: Optional cached quota override in bytes (used by tests/mocks).
    """

    email: str = ""
    password: str = ""
    access_token: str = ""
    refresh_token: str = ""
    name: str = ""
    app_id: str = ""
    app_key: str = ""
    quota: Optional[Dict[str, int]] = None

    @property
    def account_id(self) -> str:
        """Stable identifier used for token storage, logs and routing."""
        return _stable_id(self.email, self.name)

    @property
    def label(self) -> str:
        """Human readable label (never the password)."""
        return self.name or self.email or self.account_id


class ArenaAccount(BaseModel):
    """One worker agent account.

    Kollektiv is provider agnostic here: an "Arena account" is simply an
    HTTP endpoint that accepts a prompt and returns text. Point
    :attr:`base_url` at any OpenAI-compatible or custom chat endpoint --
    including self-hosted models or official API keys -- and the pool will
    use it exactly the same way.

    Attributes:
        email: Account login.
        password: Account password (only used for the interactive login flow).
        session_token: Pre-existing bearer/session token, if you have one.
        name: Optional friendly label.
        base_url: Per-account endpoint override.
        model: Per-account model name override.
        max_concurrency: How many prompts this account may run at once.
    """

    email: str = ""
    password: str = ""
    session_token: str = ""
    name: str = ""
    base_url: str = ""
    model: str = ""
    max_concurrency: int = 1

    @property
    def account_id(self) -> str:
        """Stable identifier used for token storage, logs and routing."""
        return _stable_id(self.email, self.name)

    @property
    def label(self) -> str:
        """Human readable label (never the password)."""
        return self.name or self.email or self.account_id


class Settings(BaseSettings):
    """All Kollektiv runtime settings.

    Values are read from the environment first, then from the ``.env`` file
    at the repository root. Unknown variables are ignored so the process can
    share a ``.env`` with other tools.
    """

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    APP_NAME: str = "Kollektiv"
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    LOG_LEVEL: str = "INFO"
    SECRET_KEY: str = ""
    DATABASE_URL: str = Field(default="sqlite:///./data/kollektiv.db")
    WORKSPACE_DIR: str = Field(default="./data/workspace")
    CORS_ORIGINS: str = "*"
    AUTO_INIT_DB: bool = True
    #: TLS verification for outgoing HTTP calls. Disable only for local
    #: debugging; see src/utils/net.py for the CA bundle resolution order.
    HTTP_SSL_VERIFY: bool = True
    SSL_CA_BUNDLE: str = ""

    # ------------------------------------------------------------------
    # TeraBox (shared storage)
    # ------------------------------------------------------------------
    TERABOX_ACCOUNTS: str = "[]"
    TERABOX_APP_ID: str = ""
    TERABOX_APP_KEY: str = ""
    TERABOX_BASE_URL: str = "https://openapi.terabox.com"
    TERABOX_OAUTH_PATH: str = "/oauth/2.0/token"
    TERABOX_REMOTE_ROOT: str = "/Kollektiv"
    TERABOX_CHUNK_SIZE: int = 4 * 1024 * 1024
    TERABOX_REQUEST_TIMEOUT: float = 60.0
    TERABOX_UPLOAD_TIMEOUT: float = 600.0

    # ------------------------------------------------------------------
    # Worker agents
    # ------------------------------------------------------------------
    ARENA_ACCOUNTS: str = "[]"
    ARENA_BASE_URL: str = "https://arena.ai"
    # NOTE: these two paths are provider specific. See
    # ``src/agents/arena_client.py`` for what to set them to.
    ARENA_LOGIN_PATH: str = "/api/auth/login"
    ARENA_CHAT_PATH: str = "/api/chat"
    ARENA_MODEL: str = ""
    ARENA_REQUEST_TIMEOUT: float = 300.0
    ARENA_MAX_CONCURRENCY: int = 1
    ARENA_RATE_LIMIT_COOLDOWN: int = 300
    SESSION_REFRESH_INTERVAL: int = 1800
    AGENT_MAX_RETRIES: int = 2

    # ------------------------------------------------------------------
    # GitHub (real-time code sync layer)
    # ------------------------------------------------------------------
    GITHUB_TOKEN: str = ""
    GITHUB_REPO: str = "owner/repo"
    GITHUB_WEBHOOK_SECRET: str = ""
    GITHUB_API_URL: str = "https://api.github.com"
    GITHUB_DEFAULT_BRANCH: str = "main"
    GITHUB_REQUEST_TIMEOUT: float = 30.0
    GITHUB_PUSH_AGENT_OUTPUT: bool = False
    GITHUB_AGENT_BRANCH_PREFIX: str = "kollektiv/agent"

    # ------------------------------------------------------------------
    # Brain (DeepSeek by default, Groq as fallback)
    # ------------------------------------------------------------------
    BRAIN_PROVIDER: str = "deepseek"
    BRAIN_API_KEY: str = ""
    BRAIN_BASE_URL: str = "https://api.deepseek.com"
    BRAIN_MODEL: str = "deepseek-chat"
    BRAIN_FALLBACK_PROVIDER: str = "groq"
    BRAIN_FALLBACK_API_KEY: str = ""
    BRAIN_FALLBACK_BASE_URL: str = "https://api.groq.com/openai/v1"
    BRAIN_FALLBACK_MODEL: str = "llama-3.3-70b-versatile"
    BRAIN_TEMPERATURE: float = 0.2
    BRAIN_MAX_TOKENS: int = 4096
    BRAIN_TIMEOUT: float = 120.0

    # ------------------------------------------------------------------
    # Scheduling / servers
    # ------------------------------------------------------------------
    CRON_INTERVAL_MINUTES: int = 15
    CRON_ENABLED: bool = True
    API_PORT: int = 8000
    API_HOST: str = "0.0.0.0"
    MCP_PORT: int = 8001
    MCP_HOST: str = "0.0.0.0"
    MCP_TRANSPORT: str = "sse"

    # ------------------------------------------------------------------
    # Resilience
    # ------------------------------------------------------------------
    MAX_RETRIES: int = 3
    RETRY_BASE_DELAY: float = 1.0
    RETRY_MAX_DELAY: float = 30.0
    TASK_TIMEOUT_SECONDS: int = 900
    STATE_HISTORY_LIMIT: int = 200
    DEFAULT_AGENT_COUNT: int = 3
    MAX_AGENT_COUNT: int = 12

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def project_root(self) -> str:
        """Absolute path of the repository root as a string."""
        return str(PROJECT_ROOT)

    @property
    def workspace_path(self) -> Path:
        """Absolute workspace directory, created on demand."""
        path = Path(self.WORKSPACE_DIR)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def sqlite_path(self) -> Optional[str]:
        """Filesystem path of the SQLite database, when SQLite is in use."""
        prefix = "sqlite:///"
        if self.DATABASE_URL.startswith(prefix):
            raw = self.DATABASE_URL[len(prefix) :]
            path = Path(raw)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            return str(path)
        return None

    @property
    def fernet_secret(self) -> str:
        """Secret material for Fernet encryption, with dev fallback."""
        if self.SECRET_KEY:
            return self.SECRET_KEY
        LOGGER.warning(
            "SECRET_KEY is not set; falling back to the insecure development "
            "key. Set SECRET_KEY in .env before storing real credentials."
        )
        return DEFAULT_DEV_SECRET

    @property
    def github_owner(self) -> str:
        """Repository owner part of ``GITHUB_REPO``."""
        return self.GITHUB_REPO.split("/", 1)[0] if "/" in self.GITHUB_REPO else ""

    @property
    def github_repo_name(self) -> str:
        """Repository name part of ``GITHUB_REPO``."""
        return self.GITHUB_REPO.split("/", 1)[1] if "/" in self.GITHUB_REPO else self.GITHUB_REPO

    @property
    def cors_origin_list(self) -> List[str]:
        """``CORS_ORIGINS`` parsed into a list."""
        if self.CORS_ORIGINS.strip() in {"", "*"}:
            return ["*"]
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def max_agent_concurrency(self) -> int:
        """Maximum number of prompts that may run at the same time in total."""
        return max(1, self.ARENA_MAX_CONCURRENCY) * max(1, len(self.arena_account_list()) or 1)

    # ------------------------------------------------------------------
    # Account parsing
    # ------------------------------------------------------------------
    def _parse_account_json(self, raw: str, model: type[BaseModel]) -> List[Any]:
        """Parse a JSON list of account objects, skipping malformed entries.

        Args:
            raw: Raw JSON string from the environment.
            model: Pydantic model class used to validate each entry.

        Returns:
            A list of validated account models (possibly empty).
        """
        text = (raw or "").strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            LOGGER.error(
                "Could not parse %s as JSON (%s). Fix the value in .env -- "
                "it must be a JSON array of objects.",
                "account list",
                exc,
            )
            return []
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            LOGGER.error("Account configuration must be a JSON array, got %s.", type(data).__name__)
            return []

        accounts: List[Any] = []
        for index, entry in enumerate(data):
            if not isinstance(entry, dict):
                LOGGER.warning("Skipping account #%s: expected an object.", index)
                continue
            try:
                accounts.append(model.model_validate(entry))
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.warning("Skipping account #%s: %s", index, exc)
        return accounts

    def terabox_account_list(self) -> List[TeraBoxAccount]:
        """Return the configured TeraBox accounts."""
        return self._parse_account_json(self.TERABOX_ACCOUNTS, TeraBoxAccount)

    def arena_account_list(self) -> List[ArenaAccount]:
        """Return the configured worker agent accounts."""
        return self._parse_account_json(self.ARENA_ACCOUNTS, ArenaAccount)

    # ------------------------------------------------------------------
    # Capability checks
    # ------------------------------------------------------------------
    @property
    def is_terabox_configured(self) -> bool:
        """True when at least one TeraBox account is usable."""
        return any(a.access_token or a.refresh_token for a in self.terabox_account_list())

    @property
    def is_arena_configured(self) -> bool:
        """True when at least one worker agent account is configured."""
        return bool(self.arena_account_list())

    @property
    def is_github_configured(self) -> bool:
        """True when a GitHub token and a *real* ``owner/repo`` are present.

        The ``owner/repo`` placeholder shipped in ``.env.example`` counts as
        unconfigured so the sync engine reports "not configured" instead of
        hammering the API with 404s.
        """
        if not self.GITHUB_TOKEN or "/" not in (self.GITHUB_REPO or ""):
            return False
        return self.GITHUB_REPO.strip().lower() != "owner/repo"

    @property
    def is_brain_configured(self) -> bool:
        """True when an LLM API key is present."""
        return bool(self.BRAIN_API_KEY)

    def config_warnings(self) -> List[str]:
        """Return a list of human readable configuration problems.

        Used at start-up and by ``GET /health`` so operators can see *why* a
        subsystem is degraded without reading the log file.
        """
        warnings: List[str] = []
        if not self.SECRET_KEY:
            warnings.append("SECRET_KEY is unset; using the insecure development key.")
        if not self.is_brain_configured:
            warnings.append("BRAIN_API_KEY is unset; the orchestrator brain is in heuristic mode.")
        if not self.is_terabox_configured:
            warnings.append("No usable TERABOX_ACCOUNTS; shared storage is disabled.")
        if not self.is_arena_configured:
            warnings.append("No ARENA_ACCOUNTS; the agent pool has no workers.")
        if not self.is_github_configured:
            warnings.append("GITHUB_TOKEN/GITHUB_REPO incomplete; GitHub sync is disabled.")
        if not self.GITHUB_WEBHOOK_SECRET:
            warnings.append("GITHUB_WEBHOOK_SECRET is unset; webhook signature checks are skipped.")
        if self.GITHUB_REPO.strip().lower() == "owner/repo":
            warnings.append("GITHUB_REPO is still the placeholder 'owner/repo'; set your real repository.")
        return warnings

    def redacted(self) -> Dict[str, Any]:
        """Return the settings as a dict with secrets replaced by ``***``.

        Safe to log or return from ``GET /health``.
        """
        secret_fields = {
            "SECRET_KEY",
            "BRAIN_API_KEY",
            "BRAIN_FALLBACK_API_KEY",
            "GITHUB_TOKEN",
            "GITHUB_WEBHOOK_SECRET",
            "TERABOX_APP_KEY",
            "TERABOX_ACCOUNTS",
            "ARENA_ACCOUNTS",
        }
        data: Dict[str, Any] = {}
        for name, value in self.model_dump().items():
            if name in secret_fields and value:
                data[name] = "***redacted***"
            elif name in secret_fields:
                data[name] = ""
            else:
                data[name] = value
        return data


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    The result is cached; call :func:`reset_settings_cache` (or
    ``get_settings.cache_clear()``) in tests after mutating the environment.
    """
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached settings object (mainly useful in tests)."""
    get_settings.cache_clear()


def override_settings(**values: Any) -> Settings:
    """Return fresh settings with ``values`` applied on top of the environment.

    Args:
        **values: Field overrides, e.g. ``BRAIN_MODEL="deepseek-reasoner"``.

    Returns:
        A new :class:`Settings` instance (the cache is left untouched).
    """
    current = get_settings()
    data = current.model_dump()
    data.update(values)
    data.pop("project_root", None)
    return Settings(**data)


def env_flag(name: str, default: bool = False) -> bool:
    """Interpret an environment variable as a boolean flag.

    Args:
        name: Environment variable name.
        default: Value returned when the variable is unset.

    Returns:
        ``True`` for ``1/true/yes/on`` (case insensitive), else ``False``.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "Settings",
    "TeraBoxAccount",
    "ArenaAccount",
    "get_settings",
    "reset_settings_cache",
    "override_settings",
    "env_flag",
    "PROJECT_ROOT",
    "PROJECT_STATE_FILENAME",
    "DEFAULT_DEV_SECRET",
]
