"""The sponsor line itself: when it may be drawn, what it looks like, and how it
becomes a ledger entry.

Three rules shape this module, and they are the whole difference between this
and the ad-supported agents it is modelled on:

1. **Dead time only.** A line is drawn while a worker is thinking or while the
   pool is waiting out a rate limit -- time the developer is already idle. It is
   never drawn inside generated code, inside a file, inside an agent answer, or
   as part of a system message.
2. **Opt-in, and off by default.** With ``SPONSORS_ENABLED=false`` (the default)
   :func:`current_line` returns ``None`` and nothing is ever fetched, shown or
   recorded. There is no variant of Kollektiv where this is on without the
   operator asking.
3. **Labelled and complete.** Every line is prefixed (``sponsored:``) and prints
   the advertiser and the full URL, so it can never be mistaken for model output
   or for editorial advice.

:class:`SponsorLineMux` adds the attention budget: at most one line every
``SPONSOR_MIN_INTERVAL_SECONDS`` (90 s by default) per process, which is also
what keeps a long run from turning into a wall of advertising.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from config.settings import Settings, get_settings
from src.sponsors.catalog import (
    SponsorCatalog,
    SponsorEntry,
    choose,
    eligible_entries,
    load_catalog,
    split_categories,
)
from src.sponsors.ledger import SponsorLedger
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)

#: Prefix every line carries. The advertiser terms in ``docs/monetization.md``
#: forbid anything that would disguise a line as system output, and this is the
#: belt to that pair of braces.
LINE_PREFIX = "sponsored"

#: Length of the rotation bucket. A line changes at most once per minute even if
#: something asks for it more often than the mux allows.
BUCKET_SECONDS = 60

#: Contexts where a line is allowed to appear. Anything not in this tuple gets
#: ``None``, which is the point: the surface is deliberately tiny.
ALLOWED_CONTEXTS = ("waiting", "between-tasks", "rate-limit")


def sponsor_line_enabled(settings: Optional[Settings] = None) -> bool:
    """Return whether the operator turned the sponsor line on.

    Args:
        settings: Optional settings override.

    Returns:
        ``True`` only when ``SPONSORS_ENABLED`` is set.
    """
    resolved = settings or get_settings()
    return bool(resolved.SPONSORS_ENABLED)


def render_line(entry: SponsorEntry, *, prefix: str = LINE_PREFIX) -> str:
    """Render one sponsor line as a single string.

    Args:
        entry: The entry to render.
        prefix: Label placed in front of the advertiser name.

    Returns:
        A single line of text, e.g.
        ``sponsored: Gridline -- Postgres branching for previews -- https://…``
    """
    return f"{prefix}: {entry.advertiser} -- {entry.text} -- {entry.url}"


def context_allowed(context: str) -> bool:
    """Return whether a line may be drawn in ``context``.

    Args:
        context: One of :data:`ALLOWED_CONTEXTS`, or anything else (agent
            output, file writes, ...).

    Returns:
        ``True`` for dead-time contexts only.
    """
    return str(context or "").strip().lower() in ALLOWED_CONTEXTS


@dataclass
class SponsorLineMux:
    """Watches the clock so the line cannot crowd out the work.

    Attributes:
        settings: Settings the mux reads thresholds from.
        last_shown_at: Monotonic timestamp of the last line; ``None`` before
            the first one (a fresh mux is always due).
        shown: How many lines this process has drawn.
        accrued: Sponsor ids drawn since construction, for tests and telemetry
            that stays local.
    """

    settings: Settings = field(default_factory=get_settings)
    last_shown_at: Optional[float] = None
    shown: int = 0
    accrued: List[str] = field(default_factory=list)

    def due(self, now: Optional[float] = None) -> bool:
        """Return whether the attention budget allows another line right now.

        Args:
            now: Monotonic clock override, for tests.

        Returns:
            ``True`` when at least ``SPONSOR_MIN_INTERVAL_SECONDS`` have passed
            since the previous line (or when nothing has been drawn yet).
        """
        moment = time.monotonic() if now is None else now
        if self.last_shown_at is None:
            return True
        return (moment - self.last_shown_at) >= max(0, int(self.settings.SPONSOR_MIN_INTERVAL_SECONDS))

    async def next_line(
        self,
        *,
        context: str = "waiting",
        catalog: Optional[SponsorCatalog] = None,
        categories: Optional[Sequence[str]] = None,
        record: bool = True,
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Draw the next line, if the operator opted in and the budget allows.

        Args:
            context: Where the line is wanted; see :func:`context_allowed`.
            catalog: Pre-loaded catalogue (avoids re-reading the file).
            categories: Self-declared interests; defaults to
                ``SPONSOR_CATEGORIES``.
            record: When ``True`` the line is accrued in the local ledger.
            now: Monotonic clock override, for tests.

        Returns:
            ``{sponsor_id, advertiser, text, url, rendered, context}``, or
            ``None`` when the line is disabled, not due, disallowed in this
            context, or the catalogue is empty. Every failure inside is logged
            and turns into ``None``: advertising must never break a run.
        """
        try:
            if not sponsor_line_enabled(self.settings):
                return None
            if not context_allowed(context):
                LOGGER.debug("Sponsor line suppressed in context %r", context)
                return None
            moment = time.monotonic() if now is None else now
            if not self.due(moment):
                return None
            resolved_catalog = catalog if catalog is not None else await load_catalog(self.settings)
            if resolved_catalog.is_empty:
                LOGGER.debug("Sponsor line requested but the catalogue is empty (%s)", resolved_catalog.source)
                return None
            interests = list(categories) if categories is not None else split_categories(self.settings.SPONSOR_CATEGORIES)
            bucket = int(time.time() // BUCKET_SECONDS)
            entry = choose(eligible_entries(resolved_catalog, interests), bucket=bucket, categories=interests)
            if entry is None:
                return None
            self.last_shown_at = moment
            self.shown += 1
            self.accrued.append(entry.sponsor_id)
            if record:
                await SponsorLedger(self.settings).record(entry)
            line = {
                "sponsor_id": entry.sponsor_id,
                "advertiser": entry.advertiser,
                "text": entry.text,
                "url": entry.url,
                "rendered": render_line(entry),
                "context": context,
            }
            LOGGER.info("Sponsor line for %s (%s)", entry.sponsor_id, context)
            return line
        except Exception as exc:  # noqa: BLE001 - a broken sponsor must not break work
            LOGGER.error("Could not draw a sponsor line: %s", exc, exc_info=True)
            return None

    def reset(self) -> None:
        """Forget the attention budget (used between tests and projects)."""
        self.last_shown_at = None
        self.shown = 0
        self.accrued.clear()


async def current_line(
    settings: Optional[Settings] = None,
    *,
    context: str = "waiting",
    categories: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Convenience wrapper: one line, one call, one fresh mux.

    Args:
        settings: Optional settings override.
        context: Where the line is wanted.
        categories: Self-declared interests.

    Returns:
        The line as :meth:`SponsorLineMux.next_line` returns it, or ``None``.
    """
    mux = SponsorLineMux(settings=settings or get_settings())
    return await mux.next_line(context=context, categories=categories)


__all__ = [
    "ALLOWED_CONTEXTS",
    "BUCKET_SECONDS",
    "LINE_PREFIX",
    "SponsorLineMux",
    "context_allowed",
    "current_line",
    "render_line",
    "sponsor_line_enabled",
]
