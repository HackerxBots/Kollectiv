"""SQLModel tables backing Kollektiv's persistent state.

The database stores everything that must survive a restart:

* :class:`TokenRecord` -- encrypted credentials (see :mod:`src.utils.token_store`).
* :class:`Project` / :class:`Task` / :class:`PlanRecord` -- orchestration state.
* :class:`ProjectFile` -- the file inventory mirrored to TeraBox.
* :class:`EventLog` -- append-only history appended to ``PROJECT_STATE.md``.
* :class:`AgentRecord` -- per-account worker statistics.

Usage::

    from src.db.models import init_db, session_scope, Project

    init_db()
    with session_scope() as session:
        session.add(Project(name="demo", description="..."))
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, Session, SQLModel, create_engine, select

from src.utils.logger import get_logger

LOGGER = get_logger(__name__)


def utcnow() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def new_id(prefix: str = "") -> str:
    """Return a short unique identifier, optionally prefixed."""
    value = uuid.uuid4().hex[:12]
    return f"{prefix}{value}" if prefix else value


def _json_dumps(value: Any) -> str:
    """Serialise ``value`` to compact JSON (tolerant of datetimes)."""
    return json.dumps(value, default=str, separators=(",", ":"))


def _json_loads(raw: Optional[str], default: Any) -> Any:
    """Deserialise ``raw`` returning ``default`` on any failure."""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


class TokenRecord(SQLModel, table=True):
    """An encrypted credential blob for one ``(service, account_id)`` pair."""

    __tablename__ = "token_records"
    __table_args__ = (UniqueConstraint("service", "account_id", name="uq_token_service_account"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    service: str = Field(index=True, max_length=64)
    account_id: str = Field(index=True, max_length=128)
    payload: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    expires_at: Optional[datetime] = Field(default=None)


class Project(SQLModel, table=True):
    """A Kollektiv project: one orchestrated piece of work."""

    __tablename__ = "projects"

    id: str = Field(default_factory=lambda: new_id("prj_"), primary_key=True, max_length=64)
    name: str = Field(index=True, max_length=200)
    description: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    status: str = Field(default="created", index=True, max_length=32)
    n_agents: int = Field(default=3)
    plan: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow)

    def plan_dict(self) -> Dict[str, Any]:
        """Return the stored plan as a dict."""
        return _json_loads(self.plan, {})


class Task(SQLModel, table=True):
    """A single subtask assigned to one worker agent.

    Plan task ids (``t1``, ``t2``, ...) are only unique *within* a project, so
    the primary key is the ``(project_id, id)`` pair. Every other task id in the
    system (``tsk_...``) is globally unique.
    """

    __tablename__ = "tasks"

    id: str = Field(default_factory=lambda: new_id("tsk_"), primary_key=True, max_length=64)
    project_id: str = Field(primary_key=True, max_length=64)
    title: str = Field(default="", max_length=300)
    description: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    priority: int = Field(default=3)
    dependencies: str = Field(default="[]", sa_column=Column(Text, nullable=False, default="[]"))
    status: str = Field(default="pending", index=True, max_length=32)
    assigned_agent: Optional[str] = Field(default=None, max_length=128)
    attempts: int = Field(default=0)
    max_attempts: int = Field(default=3)
    result: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    score: Optional[float] = Field(default=None)
    feedback: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    error: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def dependency_list(self) -> List[str]:
        """Return the task's dependency ids."""
        return list(_json_loads(self.dependencies, []))

    def result_dict(self) -> Dict[str, Any]:
        """Return the structured result produced by the collector."""
        return _json_loads(self.result, {})


class PlanRecord(SQLModel, table=True):
    """Snapshot of a plan (and its revisions) for a project."""

    __tablename__ = "plan_records"

    id: str = Field(default_factory=lambda: new_id("pln_"), primary_key=True, max_length=64)
    project_id: str = Field(index=True, max_length=64)
    revision: int = Field(default=1)
    plan: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    notes: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    created_at: datetime = Field(default_factory=utcnow)

    def plan_dict(self) -> Dict[str, Any]:
        """Return the stored plan as a dict."""
        return _json_loads(self.plan, {})


class ProjectFile(SQLModel, table=True):
    """A file tracked for a project (path on TeraBox + local mirror)."""

    __tablename__ = "project_files"

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: str = Field(index=True, max_length=64)
    path: str = Field(index=True, max_length=1024)
    local_path: str = Field(default="", max_length=1024)
    remote_path: str = Field(default="", max_length=1024)
    account_id: str = Field(default="", max_length=128)
    size: int = Field(default=0)
    checksum: str = Field(default="", max_length=128)
    source: str = Field(default="agent", max_length=64)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class EventLog(SQLModel, table=True):
    """Append-only event history, also written to ``PROJECT_STATE.md``."""

    __tablename__ = "event_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: str = Field(default="global", index=True, max_length=64)
    agent_id: str = Field(default="", max_length=128)
    action: str = Field(default="", max_length=128)
    result: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    payload: str = Field(default="{}", sa_column=Column(Text, nullable=False, default="{}"))
    created_at: datetime = Field(default_factory=utcnow, index=True)

    def payload_dict(self) -> Dict[str, Any]:
        """Return the structured payload attached to the event."""
        return _json_loads(self.payload, {})

    def to_event(self) -> Dict[str, Any]:
        """Return the event in the shape used by ``PROJECT_STATE.md``."""
        return {
            "timestamp": self.created_at.isoformat() if self.created_at else None,
            "agent_id": self.agent_id,
            "action": self.action,
            "result": self.result,
            "payload": self.payload_dict(),
        }


class AgentRecord(SQLModel, table=True):
    """Persistent statistics for one worker account."""

    __tablename__ = "agent_records"

    account_id: str = Field(primary_key=True, max_length=128)
    email: str = Field(default="", max_length=320)
    label: str = Field(default="", max_length=320)
    status: str = Field(default="unknown", max_length=32)
    tasks_done: int = Field(default=0)
    tasks_failed: int = Field(default=0)
    total_latency_ms: float = Field(default=0.0)
    last_error: str = Field(default="", sa_column=Column(Text, nullable=False, default=""))
    last_seen: datetime = Field(default_factory=utcnow)
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def average_latency_ms(self) -> float:
        """Mean response latency across successful tasks."""
        if self.tasks_done <= 0:
            return 0.0
        return round(self.total_latency_ms / self.tasks_done, 1)


class AgentLinkRecord(SQLModel, table=True):
    """A grant: one worker agent may use one connector.

    Links are how a team says "Nova may post to Slack, Atlas may read the repo".
    They are deliberately *not* a second copy of the connector's actions: the
    actions stay in the connector registry (`GET /connectors`), the MCP server and
    the CLI, and this table only records who is allowed to call them.

    Semantics, kept boring on purpose:

    * a connector with **no links** is open — a fresh install behaves exactly as
      before, and no existing workflow needs a new grant to keep working;
    * once a connector has links, a call that names an agent must come from a
      linked agent (``403`` otherwise);
    * operator surfaces (CLI, MCP, the dashboard with the API token) do not name
      an agent and are not constrained — they *are* the operator.

    The pair ``(agent_id, connector)`` is unique, so linking twice is an error
    the caller can see rather than a silent duplicate.
    """

    __tablename__ = "agent_links"
    __table_args__ = (UniqueConstraint("agent_id", "connector", name="uq_link_agent_connector"),)

    link_id: str = Field(primary_key=True, max_length=64)
    agent_id: str = Field(max_length=128, index=True)
    agent_name: str = Field(default="", max_length=120)
    connector: str = Field(max_length=64, index=True)
    note: str = Field(default="", max_length=280)
    created_by: str = Field(default="", max_length=120)
    created_at: datetime = Field(default_factory=utcnow)


class ProjectStateRecord(SQLModel, table=True):
    """Local mirror of the ``PROJECT_STATE.md`` document kept on TeraBox."""

    __tablename__ = "project_states"

    project_id: str = Field(primary_key=True, max_length=64)
    state: str = Field(default="{}", sa_column=Column(Text, nullable=False, default="{}"))
    etag: str = Field(default="", max_length=128)
    synced_at: datetime = Field(default_factory=utcnow)

    def state_dict(self) -> Dict[str, Any]:
        """Return the mirrored state as a dict."""
        return _json_loads(self.state, {})


class BudgetRecord(SQLModel, table=True):
    """Spend recorded for one project on one day (the local cost ledger).

    Keyed by ``(project_id, day)`` so both questions an operator asks are cheap:
    "what has this project cost?" (sum its rows) and "what have I spent today?"
    (sum today's rows). Token counts come from the provider's ``usage`` block
    when it is available and from the caller's estimate when it is not; the
    ``estimated`` flag says which, so nobody mistakes one for the other.
    """

    __tablename__ = "budget_ledger"

    project_id: str = Field(primary_key=True, max_length=64)
    day: str = Field(primary_key=True, max_length=10)  # YYYY-MM-DD, UTC
    runs: int = Field(default=0)
    tasks: int = Field(default=0)
    brain_calls: int = Field(default=0)
    brain_tokens_in: int = Field(default=0)
    brain_tokens_out: int = Field(default=0)
    worker_tokens_in: int = Field(default=0)
    worker_tokens_out: int = Field(default=0)
    usd: float = Field(default=0.0)
    estimated: bool = Field(default=True)
    updated_at: datetime = Field(default_factory=utcnow)


class GatewayClientRecord(SQLModel, table=True):
    """One client allowed to call tools through the MCP gateway.

    The secret itself is never stored here: it lives encrypted in
    ``token_records`` (service ``gateway``, account = ``name``) and is verified
    by comparing decrypted values in constant time. This row carries the
    *policy* and the bookkeeping -- which client, what it may do, when it was
    last seen -- so revoking access or narrowing a policy never touches a
    secret.
    """

    __tablename__ = "gateway_clients"

    name: str = Field(primary_key=True, max_length=64)
    label: str = Field(default="", max_length=200)
    role: str = Field(default="client", max_length=32)
    #: JSON: {"allow": [...], "deny": [...], "confirm": [...], "read_only": bool}
    policy: str = Field(default="{}", sa_column=Column(Text, nullable=False, default="{}"))
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utcnow)
    last_seen: Optional[datetime] = Field(default=None)
    calls: int = Field(default=0)


class GatewayAuditRecord(SQLModel, table=True):
    """One tool call, logged locally for the operator's own debugging.

    Deliberately says nothing about the *content* of a call: no arguments, no
    prompt, no payload -- only the tool name, how long it took, whether it
    worked and the argument *names* that were used. An audit log that can leak
    the data it audits is not worth having.
    """

    __tablename__ = "gateway_audit"

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    client: str = Field(default="", index=True, max_length=64)
    tool: str = Field(default="", index=True, max_length=200)
    namespace: str = Field(default="", max_length=64)
    ok: bool = Field(default=True)
    denied: bool = Field(default=False)
    milliseconds: int = Field(default=0)
    arg_names: str = Field(default="", max_length=300)
    detail: str = Field(default="", max_length=500)


# ----------------------------------------------------------------------
# Engine / session management
# ----------------------------------------------------------------------
_engine = None


def sqlite_path(url: str) -> Optional[str]:
    """Return the filesystem path behind a SQLite URL, or ``None``.

    In-memory URLs (``sqlite://`` and ``sqlite:///:memory:``) have no path.
    """
    if not url.startswith("sqlite") or "memory" in url:
        return None
    _, _, tail = url.partition("///")
    if not tail or tail.startswith(":"):
        return None
    return tail


def is_memory_url(url: str) -> bool:
    """Return ``True`` for SQLite in-memory URLs (including bare ``sqlite://``)."""
    if not url.startswith("sqlite"):
        return False
    if "memory" in url:
        return True
    tail = url.split("///", 1)[1] if "///" in url else url.split("//", 1)[-1]
    return tail.strip("/") == ""


def same_database(url_a: str, url_b: str) -> bool:
    """Return ``True`` when two database URLs address the same database."""
    if is_memory_url(url_a) and is_memory_url(url_b):
        return True
    path_a, path_b = sqlite_path(url_a), sqlite_path(url_b)
    if path_a and path_b:
        return Path(path_a).resolve() == Path(path_b).resolve()
    return url_a.strip() == url_b.strip()


def build_engine(url: str, echo: bool = False, settings: Any = None):
    """Create an engine for ``url`` with backend-appropriate pooling.

    SQLite gets ``check_same_thread=False`` (FastAPI worker threads) and a
    pre-created parent directory. PostgreSQL (Neon, Supabase, RDS) gets
    ``pool_pre_ping`` plus a short recycle window, which is what serverless
    Postgres needs because it closes idle connections behind your back.
    """
    from config.settings import get_settings

    config = settings or get_settings()
    if url.startswith("postgres"):
        url = normalise_postgres_url(url, config)
    path = sqlite_path(url)
    if path:
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    if url.startswith("sqlite"):
        engine_kwargs: Dict[str, Any] = {"connect_args": {"check_same_thread": False}}
    elif url.startswith("postgres"):
        engine_kwargs = {
            "pool_pre_ping": True,
            "pool_recycle": int(getattr(config, "DATABASE_POOL_RECYCLE", 300)),
            "pool_size": int(getattr(config, "DATABASE_POOL_SIZE", 5)),
            "max_overflow": int(getattr(config, "DATABASE_MAX_OVERFLOW", 5)),
        }
    else:
        engine_kwargs = {}
    LOGGER.debug("Creating database engine for %s", url.split("@")[-1] if "@" in url else url)
    return create_engine(url, echo=echo, **engine_kwargs)


def normalise_postgres_url(url: str, config: Any = None) -> str:
    """Return a SQLAlchemy 2 driver URL for a Postgres DSN.

    Neon (like Heroku) hands out ``postgres://`` URLs; SQLAlchemy needs
    ``postgresql://`` with an explicit driver, and Neon requires TLS unless the
    URL already says otherwise.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    ssl_mode = getattr(config, "DATABASE_SSL_MODE", "require") if config else "require"
    if ssl_mode and "sslmode=" not in url:
        url = f"{url}{'&' if '?' in url else '?'}sslmode={ssl_mode}"
    return url


def get_engine(database_url: Optional[str] = None, echo: bool = False):
    """Return a process-wide SQLAlchemy engine, creating it on first use.

    Args:
        database_url: Explicit database URL; defaults to ``DATABASE_URL``.
        echo: Log all SQL statements.

    Returns:
        A SQLModel/SQLAlchemy engine bound to a SQLite file (or any URL).
    """
    global _engine
    if _engine is None:
        from config.settings import get_settings

        config = get_settings()
        _engine = build_engine(database_url or config.database_url, echo=echo, settings=config)
    return _engine


def current_engine():
    """Return the already-installed engine, or ``None`` (never creates one)."""
    return _engine


def set_engine(engine: Any) -> None:
    """Override the global engine (tests, embedders, custom settings)."""
    global _engine
    _engine = engine


def bind_engine(settings: Any = None):
    """Return an engine matching ``settings``, rebinding the global when needed.

    A process may be handed more than one configuration (tests, embedders, the
    CLI honouring ``DATABASE_URL`` from ``.env``). When the installed engine
    addresses a different database it is replaced; a matching engine is kept so
    pre-seeded in-memory databases keep working.

    Args:
        settings: Settings override; defaults to the ambient settings.

    Returns:
        The engine to use.
    """
    from config.settings import get_settings

    config = settings or get_settings()
    target = config.database_url
    existing = current_engine()
    if existing is not None and same_database(str(existing.url), target):
        return existing
    engine = build_engine(target, settings=config)
    set_engine(engine)
    LOGGER.debug("Bound the database engine to %s", target)
    return engine


def init_db(engine: Any = None) -> None:
    """Create all tables if they do not exist yet.

    Args:
        engine: Optional engine override; defaults to :func:`get_engine`.
    """
    target = engine or get_engine()
    SQLModel.metadata.create_all(target)
    LOGGER.debug("Database schema ensured")


@contextmanager
def session_scope(engine: Any = None) -> Iterator[Session]:
    """Context manager yielding a transactional session.

    Commits on success, rolls back on failure, and always closes.
    ``expire_on_commit=False`` keeps loaded attributes readable after the
    commit, so callers can return ORM objects from inside the block without
    triggering a detached-instance error.
    """
    target = engine or get_engine()
    session = Session(target, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def json_column(value: Any) -> str:
    """Helper returning a JSON string for Text columns."""
    return _json_dumps(value)


def query_all(session: Session, model: Any, **filters: Any) -> List[Any]:
    """Return every row of ``model`` matching the simple equality ``filters``."""
    statement = select(model)
    for field, value in filters.items():
        statement = statement.where(getattr(model, field) == value)
    return list(session.exec(statement).all())


__all__ = [
    "utcnow",
    "new_id",
    "TokenRecord",
    "Project",
    "Task",
    "PlanRecord",
    "ProjectFile",
    "EventLog",
    "AgentRecord",
    "ProjectStateRecord",
    "BudgetRecord",
    "GatewayClientRecord",
    "GatewayAuditRecord",
    "get_engine",
    "set_engine",
    "init_db",
    "session_scope",
    "query_all",
    "json_column",
]
