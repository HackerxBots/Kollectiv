"""The gateway audit log: local, aggregate, and deliberately content-free.

Every call through the gateway leaves one row: who called, which tool, how long
it took, whether it worked, and — when it failed — a short reason. It stores the
*argument names* a caller used, never their values, because the values are the
project's data: a brief, an email body, a WhatsApp message. An audit log that
can leak the data it audits is not worth having, and this one has no reason to
keep it.

The log exists for exactly one audience: the operator, on their own machine,
debugging their own deployment (``kollektiv gateway audit``, ``GET /audit``).
It is never uploaded, never aggregated, and ``kollektiv gateway audit --clear``
deletes it.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

from sqlmodel import select

from config.settings import Settings, get_settings
from src.db.models import GatewayAuditRecord, session_scope
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)

#: Longest argument-name list stored on one row.
MAX_ARG_NAMES = 280


class GatewayAudit:
    """Async facade over the ``gateway_audit`` table."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        """Store the settings; no I/O happens here.

        Args:
            settings: Optional settings override.
        """
        self.settings = settings or get_settings()

    async def record(
        self,
        *,
        client: str,
        tool: str,
        ok: bool,
        milliseconds: int = 0,
        denied: bool = False,
        arg_names: Optional[List[str]] = None,
        detail: str = "",
    ) -> Dict[str, Any]:
        """Append one row to the local audit log.

        Args:
            client: Client name (never the token).
            tool: Namespaced tool name.
            ok: Whether the call succeeded (a denied call is not ``ok``).
            milliseconds: Duration in milliseconds.
            denied: True when policy refused the call.
            arg_names: The names of the arguments supplied, not their values.
            detail: Short failure reason, truncated.

        Returns:
            The row as a dict. Failures to write are logged and swallowed: an
            audit log must never break a tool call.
        """
        names = sorted(str(name) for name in (arg_names or []))
        record = GatewayAuditRecord(
            created_at=datetime.now(UTC),
            client=client[:64],
            tool=tool[:200],
            namespace=(tool.split(".", 1)[0] if tool else "")[:64],
            ok=bool(ok),
            denied=bool(denied),
            milliseconds=max(0, int(milliseconds)),
            arg_names=(", ".join(names))[:MAX_ARG_NAMES],
            detail=str(detail)[:500],
        )
        try:
            with session_scope() as session:
                session.add(record)
                session.commit()
                session.refresh(record)
            return self._row_to_dict(record)
        except Exception as exc:  # noqa: BLE001 - auditing is best effort
            LOGGER.warning("Could not write the gateway audit row: %s", exc)
            return self._row_to_dict(record)

    async def recent(self, limit: int = 50, client: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return the most recent rows, newest first.

        Args:
            limit: How many rows (capped at 1000).
            client: Optional client filter.

        Returns:
            The rows as dicts.
        """
        capped = max(1, min(int(limit or 50), 1000))
        with session_scope() as session:
            statement = select(GatewayAuditRecord)
            if client:
                statement = statement.where(GatewayAuditRecord.client == client)
            rows = list(session.exec(statement).all())
        rows.sort(key=lambda row: row.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return [self._row_to_dict(row) for row in rows[:capped]]

    async def stats(self, client: Optional[str] = None) -> Dict[str, Any]:
        """Return totals per client and per tool.

        Args:
            client: Optional client filter.

        Returns:
            ``{calls, failures, denied, per_tool: {...}, per_client: {...}}``.
        """
        rows = await self.recent(limit=1000, client=client)
        per_tool: Dict[str, int] = {}
        per_client: Dict[str, int] = {}
        for row in rows:
            per_tool[row["tool"]] = per_tool.get(row["tool"], 0) + 1
            per_client[row["client"]] = per_client.get(row["client"], 0) + 1
        return {
            "calls": len(rows),
            "failures": len([row for row in rows if not row["ok"] and not row["denied"]]),
            "denied": len([row for row in rows if row["denied"]]),
            "per_tool": dict(sorted(per_tool.items(), key=lambda item: -item[1])),
            "per_client": dict(sorted(per_client.items(), key=lambda item: -item[1])),
        }

    async def clear(self) -> int:
        """Delete every audit row.

        Returns:
            How many rows were removed.
        """
        with session_scope() as session:
            rows = list(session.exec(select(GatewayAuditRecord)).all())
            for row in rows:
                session.delete(row)
            session.commit()
        LOGGER.info("Cleared %s gateway audit row(s)", len(rows))
        return len(rows)

    @staticmethod
    def _row_to_dict(row: GatewayAuditRecord) -> Dict[str, Any]:
        """Render an audit row as JSON."""
        return {
            "at": row.created_at.isoformat() if row.created_at else None,
            "client": row.client,
            "tool": row.tool,
            "namespace": row.namespace,
            "ok": row.ok,
            "denied": row.denied,
            "ms": row.milliseconds,
            "args": [name for name in (row.arg_names or "").split(", ") if name],
            "detail": row.detail,
        }


class timed:  # noqa: N801 - used as a context manager, reads better lowercase
    """Tiny stopwatch: ``with timed() as t: ...; t.ms``.

    Kept here rather than in ``src/utils`` because the gateway is the only
    caller and a one-purpose class does not belong in shared code.
    """

    def __init__(self) -> None:
        """Start the clock."""
        self.started = time.perf_counter()
        self.ms = 0

    def __enter__(self) -> "timed":
        """Restart the clock on entry."""
        self.started = time.perf_counter()
        return self

    def __exit__(self, *_: Any) -> None:
        """Record the elapsed milliseconds on exit."""
        self.ms = int((time.perf_counter() - self.started) * 1000)


__all__ = ["GatewayAudit", "MAX_ARG_NAMES", "timed"]
