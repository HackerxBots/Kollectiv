"""Pick the sponsor line to show, from the clock alone.

The line rotates every ``SPONSOR_ROTATE_SECONDS``. Every viewer in the same window
sees the same line, so nothing has to be remembered per viewer.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from src.sponsors.catalog import SponsorLine, load_catalog

MIN_ROTATE_SECONDS = 30


def current_line(
    settings: Any,
    now: Optional[float] = None,
    catalog: Optional[List[SponsorLine]] = None,
) -> Optional[Dict[str, str]]:
    """Return the sponsor line for this moment, or ``None``.

    ``None`` when sponsors are disabled (the default), when the catalogue is empty,
    or when it cannot be read. No network call is made.
    """
    if not getattr(settings, "SPONSORS_ENABLED", False):
        return None
    lines = catalog if catalog is not None else load_catalog(getattr(settings, "SPONSORS_FILE", ""))
    if not lines:
        return None
    period = max(MIN_ROTATE_SECONDS, int(getattr(settings, "SPONSOR_ROTATE_SECONDS", 300)))
    moment = int(time.time() if now is None else now)
    return lines[(moment // period) % len(lines)].as_dict()
