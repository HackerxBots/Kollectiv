"""Count sponsor views and clicks on this server, and total what each sponsor owes.

Only the sponsor id, the kind of event, the price at that moment and the time are
stored. Nothing identifies the viewer. The totals are for the operator to invoice
from; nothing is sent to a payment provider or an ad network from here.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from sqlalchemy import func
from sqlmodel import select

from src.db.models import SponsorEventRecord, session_scope
from src.sponsors.catalog import SponsorLine

VIEW = "view"
CLICK = "click"


def record(sponsor_id: str, kind: str, cpc_cents: int = 0) -> None:
    """Store one event. ``kind`` is ``view`` or ``click``."""
    if kind not in (VIEW, CLICK):
        raise ValueError(f"unknown sponsor event kind: {kind!r}")
    with session_scope() as session:
        session.add(SponsorEventRecord(sponsor_id=sponsor_id, kind=kind, cpc_cents=cpc_cents))


def click_count(sponsor_id: str) -> int:
    """Return how many clicks a sponsor has had, for cap checks."""
    with session_scope() as session:
        statement = select(func.count()).where(
            SponsorEventRecord.sponsor_id == sponsor_id,
            SponsorEventRecord.kind == CLICK,
        )
        return int(session.exec(statement).one())


def summary(lines: Iterable[SponsorLine]) -> List[Dict[str, Any]]:
    """Per-sponsor views, clicks and amount owed, for every campaign in the catalogue.

    ``owed_cents`` sums the price each click was recorded at, so a later price change
    does not rewrite what was already earned.
    """
    rows: List[Dict[str, Any]] = []
    with session_scope() as session:
        for line in lines:
            views = session.exec(
                select(func.count()).where(
                    SponsorEventRecord.sponsor_id == line.id,
                    SponsorEventRecord.kind == VIEW,
                )
            ).one()
            clicks = session.exec(
                select(func.count()).where(
                    SponsorEventRecord.sponsor_id == line.id,
                    SponsorEventRecord.kind == CLICK,
                )
            ).one()
            owed = session.exec(
                select(func.coalesce(func.sum(SponsorEventRecord.cpc_cents), 0)).where(
                    SponsorEventRecord.sponsor_id == line.id,
                    SponsorEventRecord.kind == CLICK,
                )
            ).one()
            rows.append(
                {
                    "id": line.id,
                    "sponsor": line.sponsor,
                    "views": int(views),
                    "clicks": int(clicks),
                    "cap_clicks": line.cap_clicks,
                    "capped": bool(line.cap_clicks and int(clicks) >= line.cap_clicks),
                    "owed_cents": int(owed),
                }
            )
    return rows
