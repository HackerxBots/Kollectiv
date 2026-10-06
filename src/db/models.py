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

from sqlalchemy import JSON as SAJSON
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
    """A single subtask assigned to one worker agent."""

    __tablename__ = "tasks"

    id: str = Field(default_factory=lambda: new_id("tsk_"), primary_key=True, max_length=64)
    project_id: str = Field(index=True, max_length=64)
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


#: Extra SQLAlchemy column type alias kept for readability in annotations.
JSONColumn = SAJSON


# ----------------------------------------------------------------------
# Engine / session management
# ----------------------------------------------------------------------
_engine = None


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

        settings = get_settings()
        url = database_url or settings.DATABASE_URL
        if url.startswith("sqlite:///"):
            path = settings.sqlite_path or url.replace("sqlite:///", "")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, echo=echo, connect_args=connect_args)
        LOGGER.debug("Created database engine for %s", url)
    return _engine


def set_engine(engine: Any) -> None:
    """Override the global engine (used by tests with an in-memory database)."""
    global _engine
    _engine = engine


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
    "get_engine",
    "set_engine",
    "init_db",
    "session_scope",
    "query_all",
    "json_column",
]
