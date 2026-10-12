"""Sponsor catalogue: a local file of short, text-only sponsor lines.

Read from ``SPONSORS_FILE`` (a JSON list, or ``{"sponsors": [...]}``). Nothing is
fetched from the network. A bad entry is skipped and the reason is logged; it is
never shown. Copy that could pass for the tool, the model or an error is refused.

Each entry is a campaign: ``cpc_cents`` is what the sponsor pays per click, and
``cap_clicks`` ends the campaign after that many clicks (0 means no cap).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from src.utils.logger import get_logger

LOGGER = get_logger(__name__)

#: The fixed label every sponsor line carries. It cannot be changed by a catalogue.
LABEL = "Sponsored"
MAX_ENTRIES = 50
MAX_TEXT = 140
MAX_NAME = 60
MAX_URL = 300
MAX_CPC_CENTS = 100_000  # 1,000 USD per click is far beyond any real campaign
MAX_CAP_CLICKS = 10_000_000

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
#: Copy that could pass for an error, a warning, a usage message or the tool itself.
_IMPERSONATION = re.compile(
    r"^\s*(error|warning|rate[\s-]*limit|usage|retry|quota|kollektiv|system|assistant|"
    r"claude|gpt|groq|deepseek|arena)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SponsorLine:
    """One validated sponsor campaign."""

    id: str
    sponsor: str
    text: str
    url: str
    cpc_cents: int = 0
    cap_clicks: int = 0

    def as_public(self) -> Dict[str, str]:
        """Return what the dashboard receives.

        The destination is deliberately not included: the dashboard links to
        ``go``, which counts the click and then redirects, so a click is always
        counted. The label is always ``Sponsored``.
        """
        return {
            "label": LABEL,
            "id": self.id,
            "sponsor": self.sponsor,
            "text": self.text,
            "go": f"/sponsors/go/{self.id}",
        }


def _int_field(raw: Dict[str, Any], key: str, limit: int) -> Tuple[Optional[int], str]:
    value = raw.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > limit:
        return None, f"{key} must be a whole number from 0 to {limit}"
    return value, ""


def validate_entry(raw: Any) -> Tuple[Optional[SponsorLine], str]:
    """Validate one catalogue entry.

    Returns:
        ``(line, "")`` when valid, or ``(None, reason)``. An entry with
        ``"active": false`` returns ``(None, "inactive")`` and is not an error.
    """
    if not isinstance(raw, dict):
        return None, "entry is not an object"
    if raw.get("active", True) is False:
        return None, "inactive"

    ident = str(raw.get("id", "")).strip()
    if not _ID.match(ident):
        return None, "id must be lowercase letters, digits and dashes"

    sponsor = str(raw.get("sponsor", "")).strip()
    if not sponsor or len(sponsor) > MAX_NAME or _CONTROL.search(sponsor):
        return None, "sponsor name is missing, too long or has control characters"

    text = str(raw.get("text", "")).strip()
    if not text or len(text) > MAX_TEXT or _CONTROL.search(text):
        return None, f"text must be 1-{MAX_TEXT} characters on one line"
    if _IMPERSONATION.search(text):
        return None, "text reads like the tool, the model or an error; refused"

    url = str(raw.get("url", "")).strip()
    if not url or len(url) > MAX_URL or _CONTROL.search(url):
        return None, "url is missing or too long"
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or "@" in parts.netloc:
        return None, "url must be https with no user name or password"

    cpc, reason = _int_field(raw, "cpc_cents", MAX_CPC_CENTS)
    if cpc is None:
        return None, reason
    cap, reason = _int_field(raw, "cap_clicks", MAX_CAP_CLICKS)
    if cap is None:
        return None, reason

    return (
        SponsorLine(id=ident, sponsor=sponsor, text=text, url=url, cpc_cents=cpc, cap_clicks=cap),
        "",
    )


_cache: Dict[str, Tuple[float, List[SponsorLine]]] = {}


def load_catalog(path: str) -> List[SponsorLine]:
    """Load and validate the catalogue at ``path``. Re-read when the file changes.

    Returns an empty list when no path is set, the file is missing or it is not
    valid JSON. Every problem is logged once per change of the file.
    """
    if not path:
        return []
    file = Path(path)
    try:
        mtime = file.stat().st_mtime
    except OSError:
        LOGGER.warning("Sponsor catalogue not found at %s; no sponsor line will show", path)
        return []
    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOGGER.warning("Sponsor catalogue %s is not readable JSON: %s", path, exc)
        _cache[path] = (mtime, [])
        return []

    entries = data.get("sponsors", []) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        LOGGER.warning("Sponsor catalogue %s must be a list or {\"sponsors\": [...]}", path)
        _cache[path] = (mtime, [])
        return []

    lines: List[SponsorLine] = []
    seen: set = set()
    for index, raw in enumerate(entries[:MAX_ENTRIES]):
        line, reason = validate_entry(raw)
        if line is None:
            if reason != "inactive":
                LOGGER.warning("Sponsor catalogue entry %d skipped: %s", index, reason)
            continue
        if line.id in seen:
            LOGGER.warning("Sponsor catalogue entry %d skipped: duplicate id %s", index, line.id)
            continue
        seen.add(line.id)
        lines.append(line)
    _cache[path] = (mtime, lines)
    return lines
