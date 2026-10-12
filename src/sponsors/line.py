"""Pick the sponsor line to show, and send clicks on to the sponsor.

The line rotates every ``SPONSOR_ROTATE_SECONDS`` across the campaigns that have not
reached their cap. Every viewer in the same window sees the same line, so nothing is
remembered per viewer. Views and clicks are counted on this server (see ``ledger``).
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from src.sponsors import ledger
from src.sponsors.catalog import SponsorLine, load_catalog

MIN_ROTATE_SECONDS = 30


def _enabled(settings: Any) -> bool:
    return bool(getattr(settings, "SPONSORS_ENABLED", False))


def _catalog(settings: Any, catalog: Optional[List[SponsorLine]]) -> List[SponsorLine]:
    if catalog is not None:
        return catalog
    return load_catalog(getattr(settings, "SPONSORS_FILE", ""))


def _capped(line: SponsorLine) -> bool:
    return bool(line.cap_clicks) and ledger.click_count(line.id) >= line.cap_clicks


def current_line(
    settings: Any,
    now: Optional[float] = None,
    catalog: Optional[List[SponsorLine]] = None,
    record_view: bool = False,
) -> Optional[Dict[str, str]]:
    """Return the sponsor line for this moment, or ``None``.

    ``None`` when sponsors are switched off, the catalogue is empty or unreadable,
    or every campaign has reached its cap. No network call is made.
    """
    if not _enabled(settings):
        return None
    eligible = [line for line in _catalog(settings, catalog) if not _capped(line)]
    if not eligible:
        return None
    period = max(MIN_ROTATE_SECONDS, int(getattr(settings, "SPONSOR_ROTATE_SECONDS", 300)))
    moment = int(time.time() if now is None else now)
    chosen = eligible[(moment // period) % len(eligible)]
    if record_view:
        ledger.record(chosen.id, ledger.VIEW, chosen.cpc_cents)
    return chosen.as_public()


def go_target(
    settings: Any,
    sponsor_id: str,
    catalog: Optional[List[SponsorLine]] = None,
) -> Optional[str]:
    """Count a click and return the sponsor's destination, or ``None`` if unknown.

    A capped campaign still forwards the reader, but its click is not counted.
    """
    if not _enabled(settings):
        return None
    match = next((line for line in _catalog(settings, catalog) if line.id == sponsor_id), None)
    if match is None:
        return None
    if not _capped(match):
        ledger.record(match.id, ledger.CLICK, match.cpc_cents)
    return match.url
