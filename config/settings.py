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
    # Storage backend
    # ------------------------------------------------------------------
    #: ``auto`` picks R2 when it is configured, then TeraBox; ``r2``/``terabox``
    #: force a backend and ``none`` keeps everything on the local filesystem.
    STORAGE_BACKEND: str = "auto"

    # ------------------------------------------------------------------
    # Cloudflare R2 (recommended: free tier, S3 compatible, no egress fees)
    # ------------------------------------------------------------------
    #: JSON list to pool several buckets into one drive (9Drive style):
    #: ``[{"name": "primary", "bucket": "kollektiv", "access_key_id": "…",
    #:    "secret_access_key": "…", "endpoint": "https://<acct>.r2.cloudflarestorage.com"}]``
    R2_ACCOUNTS: str = "[]"
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET: str = "kollektiv"
    R2_ENDPOINT: str = ""
    R2_REGION: str = "auto"
    #: Key namespace for everything Kollektiv writes.
    R2_PREFIX: str = "kollektiv"
    #: Optional public/custom-domain base (``https://files.example.com``) used
    #: instead of presigned URLs in API responses.
    R2_PUBLIC_BASE_URL: str = ""
    R2_PRESIGN_EXPIRES: int = 3600
    R2_REQUEST_TIMEOUT: float = 120.0
    #: Free storage per bucket, used for routing and quota reporting.
    R2_FREE_TIER_GB: float = 10.0

    # ------------------------------------------------------------------
    # Database (Neon Postgres in production, SQLite locally)
    # ------------------------------------------------------------------
    #: ``require``/``prefer``/``disable``; applied to Postgres URLs only.
    DATABASE_SSL_MODE: str = "require"
    #: Recycling/pooling knobs for serverless Postgres (ignored by SQLite).
    DATABASE_POOL_SIZE: int = 5
    DATABASE_MAX_OVERFLOW: int = 5
    DATABASE_POOL_RECYCLE: int = 300

    # ------------------------------------------------------------------
    # Authentication (Clerk)
    # ------------------------------------------------------------------
    CLERK_SECRET_KEY: str = ""
    CLERK_PUBLISHABLE_KEY: str = ""
    #: Optional overrides; derived from the publishable key when empty.
    CLERK_ISSUER: str = ""
    CLERK_JWKS_URL: str = ""
    #: Comma separated origins allowed to present Clerk tokens (``azp`` claim).
    CLERK_AUTHORIZED_PARTIES: str = ""
    #: ``false`` keeps the API open (handy for local development and tests).
    AUTH_REQUIRED: bool = False
    #: Svix signing secret for Clerk webhooks (``whsec_…``).
    CLERK_WEBHOOK_SECRET: str = ""

    # ------------------------------------------------------------------
    # Email notifications (Resend)
    # ------------------------------------------------------------------
    RESEND_API_KEY: str = ""
    RESEND_FROM: str = "Kollektiv <onboarding@resend.dev>"
    RESEND_BASE_URL: str = "https://api.resend.com"
    #: Comma separated recipients for run summaries and alerts.
    NOTIFY_EMAILS: str = ""
    NOTIFY_ON_RUN_COMPLETION: bool = True
    NOTIFY_ON_FAILURE_ONLY: bool = False
    #: Public URL of the dashboard, used in email links.
    APP_BASE_URL: str = "http://localhost:8000"

    # ------------------------------------------------------------------
    # Connectors (optional links to the services you already use)
    # ------------------------------------------------------------------
    #: Google Workspace OAuth client + a long-lived refresh token. One token can
    #: cover Gmail, Calendar and Drive; store it in the encrypted token store
    #: instead of here when you can (``kollektiv call google ...`` works either
    #: way: the token store takes precedence over these variables).
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REFRESH_TOKEN: str = ""
    GOOGLE_TOKEN_URL: str = "https://oauth2.googleapis.com/token"
    GOOGLE_REQUEST_TIMEOUT: float = 30.0
    #: Notion integration token (``ntn_...``) + API version header.
    NOTION_TOKEN: str = ""
    NOTION_BASE_URL: str = "https://api.notion.com"
    NOTION_VERSION: str = "2022-06-28"
    NOTION_REQUEST_TIMEOUT: float = 30.0
    #: Comma separated URLs that receive Kollektiv events (run started/finished,
    #: failures). Use this to poke Slack/Discord/Zapier/n8n or your own service.
    # ------------------------------------------------------------------
    # Chat and messaging connectors (all optional, all off by default)
    # ------------------------------------------------------------------
    #: Telegram Bot API -- create a bot with @BotFather, then talk to it.
    TELEGRAM_BOT_TOKEN: str = ""
    #: Default destination when an action does not name a chat.
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_BASE_URL: str = "https://api.telegram.org"
    TELEGRAM_REQUEST_TIMEOUT: float = 30.0
    #: Discord: a bot token (channel actions) and/or an incoming webhook URL.
    DISCORD_BOT_TOKEN: str = ""
    DISCORD_DEFAULT_CHANNEL: str = ""
    DISCORD_WEBHOOK_URL: str = ""
    DISCORD_BASE_URL: str = "https://discord.com/api"
    DISCORD_API_VERSION: str = "v10"
    DISCORD_REQUEST_TIMEOUT: float = 30.0
    #: Slack: a bot token (Web API) and/or an incoming webhook URL.
    SLACK_BOT_TOKEN: str = ""
    SLACK_DEFAULT_CHANNEL: str = ""
    SLACK_WEBHOOK_URL: str = ""
    SLACK_BASE_URL: str = "https://slack.com/api"
    SLACK_REQUEST_TIMEOUT: float = 30.0
    #: Linear GraphQL API -- personal API key (``lin_api_...``).
    LINEAR_API_KEY: str = ""
    #: Default team for ``create_issue`` when the caller does not name one.
    LINEAR_TEAM_ID: str = ""
    LINEAR_BASE_URL: str = "https://api.linear.app"
    LINEAR_REQUEST_TIMEOUT: float = 30.0
    #: WhatsApp. ``""`` keeps it off; ``cloud`` is Meta's official Business API
    #: (sanctioned, needs a verified business number); ``bridge`` talks to a
    #: local linked-device bridge (the route OpenClaw takes with Baileys /
    #: whatsapp-web.js) and additionally requires WA_ALLOW_UNOFFICIAL.
    WA_BACKEND: str = ""
    WA_DEFAULT_TO: str = ""
    WA_CLOUD_TOKEN: str = ""
    WA_PHONE_NUMBER_ID: str = ""
    WA_GRAPH_URL: str = "https://graph.facebook.com"
    WA_GRAPH_VERSION: str = "v21.0"
    WA_BRIDGE_URL: str = ""
    WA_BRIDGE_TOKEN: str = ""
    WA_BRIDGE_SEND_PATH: str = "/send"
    WA_BRIDGE_STATUS_PATH: str = "/status"
    #: The honest switch: automating a personal account is against Meta's terms
    #: and can get the number banned. Bridge mode refuses to start without it.
    WA_ALLOW_UNOFFICIAL: bool = False
    WA_REQUEST_TIMEOUT: float = 30.0

    # ------------------------------------------------------------------
    # Budget: estimates, caps and the local spend ledger
    # ------------------------------------------------------------------
    #: Estimate every project's cost before it runs, and record what each run
    #: used. Estimation is free and side-effect free; only the caps block runs.
    BUDGET_ENABLED: bool = True
    #: Refuse to run a project whose *estimate* is above this (0 = no cap).
    BUDGET_MAX_USD: float = 0.0
    #: Refuse any run when today's recorded spend is already above this (0 = off).
    BUDGET_DAILY_MAX_USD: float = 0.0
    #: Warn from this fraction of a cap (0.8 = 80%).
    BUDGET_WARN_AT: float = 0.8
    #: Prices used to turn tokens into money *for the estimate only*. They are
    #: arithmetic on your numbers, not a quote: override them with your
    #: provider's current rates. Defaults match a cheap DeepSeek-class model.
    BUDGET_PRICE_IN_PER_MTOK: float = 0.30
    BUDGET_PRICE_OUT_PER_MTOK: float = 1.20
    #: Worker endpoints are free by default (Arena accounts, local models).
    BUDGET_WORKER_PRICE_IN_PER_MTOK: float = 0.0
    BUDGET_WORKER_PRICE_OUT_PER_MTOK: float = 0.0
    #: Token heuristics behind the estimate, so it can be tuned rather than
    #: trusted blindly.
    BUDGET_PROMPT_OVERHEAD_TOKENS: int = 900
    BUDGET_OUTPUT_TOKENS_PER_TASK: int = 700
    #: Explicit path to the project's `.kollektiv.yml` (otherwise auto-found).
    PROJECT_CONFIG_PATH: str = ""

    # ------------------------------------------------------------------
    # MCP gateway (one URL, per-client tokens, local audit log)
    # ------------------------------------------------------------------
    #: Serve the gateway. Off by default: the plain MCP server and the HTTP API
    #: stay first-class and the gateway is an addition, never a requirement.
    GATEWAY_ENABLED: bool = False
    GATEWAY_HOST: str = "0.0.0.0"
    GATEWAY_PORT: int = 8010
    #: Path the MCP endpoint is mounted on (Claude Code, Codex, Cursor, ...).
    GATEWAY_MCP_PATH: str = "/mcp"
    #: Refuse unauthenticated tool calls. Turning this off is a development
    #: convenience and logs a warning at startup.
    GATEWAY_REQUIRE_TOKENS: bool = True
    #: Optional JSON policy file; per-client overrides live in the database.
    GATEWAY_POLICY_PATH: str = ""
    #: How many audit rows ``GET /audit`` returns by default.
    GATEWAY_AUDIT_LIMIT: int = 200
    #: Name of the first client ``kollektiv gateway init`` creates.
    GATEWAY_DEFAULT_CLIENT: str = "dashboard"
    GATEWAY_REQUEST_TIMEOUT: float = 120.0
    #: Host-header allowlist for the MCP mount (comma separated, e.g.
    #: ``gateway.example.com,myhost:*``). Empty (the default) disables the MCP
    #: SDK's DNS-rebinding check, which is the right call for a bearer-token
    #: endpoint and the wrong one for an open server — so set it if you expose
    #: the gateway to a browser you do not control.
    GATEWAY_ALLOWED_HOSTS: str = ""
    #: Origin allowlist for the MCP mount; defaults to the hosts above.
    GATEWAY_ALLOWED_ORIGINS: str = ""
    #: Longest request body the MCP endpoint accepts, in bytes.
    GATEWAY_MAX_BODY_BYTES: int = 4194304

    EVENT_WEBHOOKS: str = ""
    #: Declarative REST connectors: any JSON API becomes a tool without code.
    #: [{"name":"slack","category":"chat","base_url":"https://slack.com/api",
    #:   "auth":"bearer","token":"xoxb-...","actions":[{"name":"post_message",
    #:   "method":"POST","path":"/chat.postMessage","params":{"channel":"…","text":"…"}}]}]
    CUSTOM_CONNECTORS: str = "[]"

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
    # Sponsor line (optional, off by default, ledger stays on your machine)
    # ------------------------------------------------------------------
    #: Master switch. Kollektiv shows nothing -- and earns nothing -- until you
    #: flip this on. Nothing here is required to build, run or deploy.
    SPONSORS_ENABLED: bool = False
    #: JSON catalogue on disk; see ``docs/monetization.md`` for the schema.
    SPONSOR_CATALOG_PATH: str = ""
    #: Or an HTTPS endpoint returning the same JSON, for catalogues you do not
    #: want to keep in sync by hand. Fetched only when a line is actually asked
    #: for, never at import time.
    SPONSOR_CATALOG_URL: str = ""
    #: Ed25519 public key (base64, raw 32 bytes) that signs the catalogue. Set
    #: it to refuse unsigned catalogues; leave empty for a local file you wrote.
    SPONSOR_CATALOG_PUBLIC_KEY: str = ""
    #: Developer share of the gross, in basis points (7500 = 75%, the rate the
    #: CLI spinner ad networks publish).
    SPONSOR_SHARE_BP: int = 7500
    #: Fallback rate, in cents per 1000 impressions, when an entry omits one.
    SPONSOR_CPM_CENTS: int = 100
    #: A claim is only offered above this many cents.
    SPONSOR_MIN_PAYOUT_CENTS: int = 1000
    #: Self-declared interests ("databases,ai") -- the *only* targeting that
    #: exists. No prompt, no code, no history is ever read to pick a line.
    SPONSOR_CATEGORIES: str = ""
    #: Minimum seconds between two lines in one process (attention budget).
    SPONSOR_MIN_INTERVAL_SECONDS: int = 90
    SPONSOR_REQUEST_TIMEOUT: float = 15.0

    # ------------------------------------------------------------------
    # Resilience
    # ------------------------------------------------------------------
    MAX_RETRIES: int = 3
    RETRY_BASE_DELAY: float = 1.0
    RETRY_MAX_DELAY: float = 30.0
    TASK_TIMEOUT_SECONDS: int = 900
    STATE_HISTORY_LIMIT: int = 200
    DEFAULT_AGENT_COUNT: int = 3
    #: Sanity bound for a single project's plan, not a cap on the pool: add
    #: accounts to ARENA_ACCOUNTS and the pool serves them all (64 workers is
    #: already far past what one cheap brain can keep fed).
    MAX_AGENT_COUNT: int = 64

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
    def database_url(self) -> str:
        """Return ``DATABASE_URL`` normalised for SQLAlchemy + the current driver.

        Neon (and Heroku) hand out ``postgres://`` URLs; SQLAlchemy 2 wants
        ``postgresql://`` and an explicit driver. ``sslmode`` is added when the
        URL does not already carry one, because Neon requires TLS.
        """
        url = (self.DATABASE_URL or "").strip()
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://") :]
        if url.startswith("postgres") and "sslmode=" not in url:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}sslmode={self.DATABASE_SSL_MODE}"
        return url

    @property
    def is_postgres(self) -> bool:
        """True when the configured database is PostgreSQL (Neon, Supabase, RDS)."""
        return self.database_url.startswith(("postgresql", "postgres"))

    @property
    def r2_account_list(self) -> List[Dict[str, str]]:
        """Parsed ``R2_ACCOUNTS`` entries (empty when misconfigured)."""
        from src.storage.r2_client import r2_accounts_from_settings

        return r2_accounts_from_settings(self)

    @property
    def is_r2_configured(self) -> bool:
        """True when R2 credentials (single bucket or pooled) are present."""
        if self.R2_ACCESS_KEY_ID and self.R2_SECRET_ACCESS_KEY and self.R2_ENDPOINT:
            return True
        raw = (self.R2_ACCOUNTS or "").strip()
        if raw and raw not in ("[]", "{}"):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return False
            if isinstance(parsed, dict):
                parsed = [parsed]
            if isinstance(parsed, list):
                return any(
                    str(item.get("bucket") or self.R2_BUCKET)
                    and str(item.get("endpoint") or self.R2_ENDPOINT)
                    and (item.get("access_key_id") or item.get("access_key"))
                    and (item.get("secret_access_key") or item.get("secret_key"))
                    for item in parsed
                    if isinstance(item, dict)
                )
        return False

    @property
    def storage_backend(self) -> str:
        """Resolve ``STORAGE_BACKEND`` (``auto``) to a concrete backend name."""
        choice = (self.STORAGE_BACKEND or "auto").strip().lower()
        if choice in ("", "auto"):
            if self.is_r2_configured:
                return "r2"
            if self.is_terabox_configured:
                return "terabox"
            return "none"
        return choice

    @property
    def clerk_issuer(self) -> str:
        """Clerk issuer URL, derived from the publishable key when needed.

        The publishable key (``pk_test_<base64(domain)$>``) embeds the frontend
        API domain, which is exactly the token issuer.
        """
        if self.CLERK_ISSUER:
            return self.CLERK_ISSUER.rstrip("/")
        key = self.CLERK_PUBLISHABLE_KEY or ""
        for prefix in ("pk_test_", "pk_live_"):
            if key.startswith(prefix):
                try:
                    import base64

                    raw = key[len(prefix) :]
                    padded = raw + "=" * (-len(raw) % 4)
                    host = base64.urlsafe_b64decode(padded.encode()).decode().rstrip("$")
                    return f"https://{host}"
                except Exception:  # noqa: BLE001 - fall back to the secret key path
                    break
        return ""

    @property
    def clerk_jwks_url(self) -> str:
        """JWKS endpoint used to verify Clerk session tokens."""
        if self.CLERK_JWKS_URL:
            return self.CLERK_JWKS_URL
        issuer = self.clerk_issuer
        return f"{issuer}/.well-known/jwks.json" if issuer else ""

    @property
    def clerk_authorized_parties(self) -> List[str]:
        """Allowed ``azp`` (authorised party) values for Clerk tokens."""
        return [item.strip() for item in (self.CLERK_AUTHORIZED_PARTIES or "").split(",") if item.strip()]

    @property
    def is_clerk_configured(self) -> bool:
        """True when Clerk tokens can be verified."""
        return bool(self.CLERK_SECRET_KEY and (self.clerk_jwks_url or self.clerk_issuer))

    @property
    def notify_recipients(self) -> List[str]:
        """Parsed ``NOTIFY_EMAILS`` list."""
        return [item.strip() for item in (self.NOTIFY_EMAILS or "").split(",") if item.strip()]

    @property
    def is_resend_configured(self) -> bool:
        """True when email notifications can be sent."""
        return bool(self.RESEND_API_KEY and self.notify_recipients)

    @property
    def is_google_configured(self) -> bool:
        """True when a Google OAuth client and refresh token are present."""
        return bool(self.GOOGLE_CLIENT_ID and self.GOOGLE_CLIENT_SECRET and self.GOOGLE_REFRESH_TOKEN)

    @property
    def is_notion_configured(self) -> bool:
        """True when a Notion integration token is present."""
        return bool(self.NOTION_TOKEN)

    @property
    def event_webhook_urls(self) -> List[str]:
        """``EVENT_WEBHOOKS`` parsed into a list of URLs."""
        return [url.strip() for url in (self.EVENT_WEBHOOKS or "").split(",") if url.strip()]

    @property
    def custom_connector_configs(self) -> List[Dict[str, Any]]:
        """Parsed ``CUSTOM_CONNECTORS`` entries (invalid JSON yields none)."""
        raw = (self.CUSTOM_CONNECTORS or "").strip()
        if not raw or raw in ("[]", "{}"):
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            LOGGER.warning("CUSTOM_CONNECTORS is not valid JSON (%s); ignoring it", exc)
            return []
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            LOGGER.warning("CUSTOM_CONNECTORS must be a JSON list; ignoring %r", type(parsed).__name__)
            return []
        return [item for item in parsed if isinstance(item, dict) and item.get("name")]

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
        if self.storage_backend == "none":
            warnings.append(
                "No shared storage configured (set R2_* or TERABOX_ACCOUNTS); "
                "the orchestrator keeps state and artifacts in the local workspace."
            )
        elif self.storage_backend == "terabox" and not self.is_terabox_configured:
            warnings.append("STORAGE_BACKEND=terabox but no usable TERABOX_ACCOUNTS are configured.")
        if self.AUTH_REQUIRED and not self.is_clerk_configured:
            warnings.append("AUTH_REQUIRED is true but Clerk is not configured; the API will reject requests.")
        if not self.NOTIFY_EMAILS:
            warnings.append("NOTIFY_EMAILS is empty; run summaries are not emailed.")
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
            "R2_ACCOUNTS",
            "R2_SECRET_ACCESS_KEY",
            "CLERK_SECRET_KEY",
            "CLERK_WEBHOOK_SECRET",
            "RESEND_API_KEY",
            "GOOGLE_CLIENT_SECRET",
            "GOOGLE_REFRESH_TOKEN",
            "NOTION_TOKEN",
            # Declarative connectors and event URLs can carry tokens in the
            # query string or body, so they are masked wholesale.
            "CUSTOM_CONNECTORS",
            "EVENT_WEBHOOKS",
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
