"""The sponsor catalogue: what may appear on a Kollektiv sponsor line.

A sponsor line is the *only* advertising surface Kollektiv has. It is drawn on
dead time -- while a worker is thinking, or while the pool waits out a rate
limit -- and nowhere else: never inside generated code, never in a file, never
as an agent answer. This module owns what a line may say and where the list of
lines comes from:

* ``SPONSOR_CATALOG_PATH`` -- a JSON file you control (a team running its own
  sponsors, a maintainer listing their own projects, an ad-free catalogue).
* ``SPONSOR_CATALOG_URL`` -- an HTTPS endpoint returning the same JSON, for a
  catalogue you do not want to keep in sync by hand. It is fetched lazily, only
  when a line is actually requested, and it is verified against
  ``SPONSOR_CATALOG_PUBLIC_KEY`` when that key is set.

Nothing here is required for Kollektiv to work. With no catalogue configured,
:func:`load_catalog` returns an empty catalogue and the line is simply absent.

Catalogue JSON::

    {
      "version": 1,
      "updated_at": "2026-10-07T00:00:00Z",
      "entries": [
        {
          "id": "gridline",
          "advertiser": "Gridline",
          "text": "Postgres branching for preview environments",
          "url": "https://example.com/gridline",
          "categories": ["databases"],
          "cpm_cents": 120,
          "weight": 1
        }
      ],
      "signature": "base64-ed25519-over-canonical-payload"
    }

Selection is deterministic and local: a weighted pick keyed by an HMAC of the
time bucket and the caller's self-declared category. No request leaves the
machine to choose a line, and no state about the user is involved -- two people
with the same clock and the same category see the same sequence.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from config.settings import Settings, get_settings
from src.utils.logger import get_logger
from src.utils.net import async_client_kwargs
from src.utils.retry import async_retry

LOGGER = get_logger(__name__)

#: Longest a single line may be. Long enough for a useful sentence, short enough
#: that it cannot bury an agent answer or pretend to be a paragraph of output.
MAX_TEXT_LENGTH = 140

#: Shortest a line may be; "buy" is not an advertisement, it is noise.
MIN_TEXT_LENGTH = 8

#: Phrases that would let an advertiser impersonate the tool or the model. Kept
#: in the catalogue layer on purpose: this is the rule an advertiser agrees to,
#: enforced by code rather than by an honour system.
FORBIDDEN_MARKERS: Tuple[str, ...] = (
    "kollektiv",
    "system",
    "assistant",
    "error",
    "warning",
    "traceback",
    "stack trace",
    "ignore previous",
    "you must",
    "click here",
)

#: Default rate used when a catalogue entry omits one, in cents per 1000 lines.
DEFAULT_CPM_CENTS = 100


class SponsorCatalogError(ValueError):
    """Raised when a catalogue exists but cannot be trusted or parsed."""


@dataclass(frozen=True)
class SponsorEntry:
    """One admissible sponsor line.

    Attributes:
        sponsor_id: Stable identifier used by the ledger.
        advertiser: Who is paying; shown next to the line so it cannot pass as
            editorial content.
        text: The one-line message itself.
        url: Destination, printed in full so it can be read without clicking.
        categories: Self-declared interests this entry may be shown against.
        cpm_cents: Rate per 1000 impressions.
        weight: Relative frequency inside the rotation.
    """

    sponsor_id: str
    advertiser: str
    text: str
    url: str
    categories: Tuple[str, ...] = ()
    cpm_cents: int = DEFAULT_CPM_CENTS
    weight: int = 1

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly view of the entry."""
        return {
            "id": self.sponsor_id,
            "advertiser": self.advertiser,
            "text": self.text,
            "url": self.url,
            "categories": list(self.categories),
            "cpm_cents": self.cpm_cents,
            "weight": self.weight,
        }


def normalise_text(value: Any) -> str:
    """Validate and collapse a line of sponsor copy.

    Args:
        value: The raw ``text`` field from a catalogue.

    Returns:
        The normalised single line.

    Raises:
        SponsorCatalogError: When the text is missing, too short or long, spans
            several lines, carries control characters, or contains a marker
            that would let it impersonate the tool or the model.
    """
    text = " ".join(str(value or "").split())
    if len(text) < MIN_TEXT_LENGTH:
        raise SponsorCatalogError(f"sponsor text is too short: {text!r}")
    if len(text) > MAX_TEXT_LENGTH:
        raise SponsorCatalogError(f"sponsor text is longer than {MAX_TEXT_LENGTH} characters: {text!r}")
    lowered = text.lower()
    for marker in FORBIDDEN_MARKERS:
        if marker in lowered:
            raise SponsorCatalogError(f"sponsor text may not contain {marker!r}: {text!r}")
    return text


def normalise_url(value: Any) -> str:
    """Validate a sponsor destination.

    Args:
        value: The raw ``url`` field.

    Returns:
        The URL, unchanged, when it is a plain ``http(s)`` address.

    Raises:
        SponsorCatalogError: When the URL is missing or not http(s).
    """
    url = str(value or "").strip()
    if not url.startswith(("http://", "https://")):
        raise SponsorCatalogError(f"sponsor url must be http(s): {url!r}")
    return url


def _entry_from_payload(payload: Dict[str, Any], default_cpm: int) -> SponsorEntry:
    """Build a :class:`SponsorEntry` from one catalogue object.

    Args:
        payload: One element of the catalogue's ``entries`` list.
        default_cpm: Rate to fall back on when the entry omits ``cpm_cents``.

    Returns:
        The validated entry.

    Raises:
        SponsorCatalogError: When required fields are missing or invalid.
    """
    sponsor_id = str(payload.get("id") or "").strip()
    if not sponsor_id:
        raise SponsorCatalogError("sponsor entry is missing an 'id'")
    advertiser = " ".join(str(payload.get("advertiser") or sponsor_id).split())
    categories = tuple(
        str(item).strip().lower() for item in (payload.get("categories") or []) if str(item).strip()
    )
    try:
        cpm_cents = int(payload.get("cpm_cents", default_cpm))
    except (TypeError, ValueError) as exc:
        raise SponsorCatalogError(f"sponsor {sponsor_id}: cpm_cents is not a number") from exc
    try:
        weight = int(payload.get("weight", 1))
    except (TypeError, ValueError) as exc:
        raise SponsorCatalogError(f"sponsor {sponsor_id}: weight is not a number") from exc
    return SponsorEntry(
        sponsor_id=sponsor_id,
        advertiser=advertiser,
        text=normalise_text(payload.get("text")),
        url=normalise_url(payload.get("url")),
        categories=categories,
        cpm_cents=max(0, cpm_cents),
        weight=max(1, weight),
    )


def canonical_payload(payload: Dict[str, Any]) -> bytes:
    """Return the exact bytes an Ed25519 catalogue signature covers.

    Args:
        payload: The decoded catalogue, ``signature`` removed.

    Returns:
        ``json.dumps(..., sort_keys=True, separators=(",", ":"))`` encoded as
        UTF-8, so signers and verifiers cannot disagree about whitespace.
    """
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def verify_signature(payload: Dict[str, Any], public_key_b64: str) -> bool:
    """Verify a catalogue's Ed25519 signature.

    Args:
        payload: The decoded catalogue including its ``signature`` field.
        public_key_b64: Base64 of the 32 raw public-key bytes (or a PEM block).

    Returns:
        ``True`` when the signature matches, ``False`` for every other outcome
        (missing signature, bad key, backend unavailable) -- callers log and
        refuse the catalogue rather than crash.
    """
    signature_b64 = str(payload.get("signature") or "")
    if not signature_b64:
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        raw_key = public_key_b64.strip()
        key: Ed25519PublicKey
        if "BEGIN PUBLIC KEY" in raw_key:
            from cryptography.hazmat.primitives import serialization

            loaded = serialization.load_pem_public_key(raw_key.encode("utf-8"))
            if not isinstance(loaded, Ed25519PublicKey):
                LOGGER.warning("SPONSOR_CATALOG_PUBLIC_KEY is not an Ed25519 key")
                return False
            key = loaded
        else:
            key = Ed25519PublicKey.from_public_bytes(base64.b64decode(raw_key))
        key.verify(base64.b64decode(signature_b64), canonical_payload(payload))
        return True
    except InvalidSignature:
        LOGGER.warning("Sponsor catalogue signature does not match the configured public key")
        return False
    except Exception as exc:  # noqa: BLE001 - a bad key must not break the run
        LOGGER.warning("Could not verify the sponsor catalogue signature: %s", exc)
        return False


@dataclass
class SponsorCatalog:
    """A validated set of sponsor lines plus where it came from.

    Attributes:
        entries: Admissible entries, in catalogue order.
        version: Catalogue format version.
        updated_at: Free-form timestamp from the catalogue.
        source: ``"path:<file>"``, ``"url:<endpoint>"`` or ``"none"``.
        error: Why a configured catalogue was refused, when that happened. The
            run continues with an empty catalogue; ``kollektiv sponsors status``
            surfaces this string so the operator can fix it.
    """

    entries: List[SponsorEntry] = field(default_factory=list)
    version: int = 1
    updated_at: str = ""
    source: str = "none"
    error: str = ""

    @property
    def is_empty(self) -> bool:
        """Return ``True`` when there is nothing to show."""
        return not self.entries

    def categories(self) -> List[str]:
        """Return every category mentioned by the catalogue, sorted."""
        seen = {category for entry in self.entries for category in entry.categories}
        return sorted(seen)

    def by_id(self, sponsor_id: str) -> Optional[SponsorEntry]:
        """Return the entry with ``sponsor_id``, if the catalogue has it."""
        return next((entry for entry in self.entries if entry.sponsor_id == sponsor_id), None)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly view of the catalogue (entries included)."""
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "source": self.source,
            "error": self.error,
            "count": len(self.entries),
            "categories": self.categories(),
            "entries": [entry.to_dict() for entry in self.entries],
        }


def parse_catalog(payload: Any, *, source: str = "memory", public_key: str = "") -> SponsorCatalog:
    """Validate a decoded catalogue.

    One malformed entry never poisons the rest: bad entries are logged and
    skipped, so a catalogue keeps working while a sponsor fixes their copy.

    Args:
        payload: The decoded JSON.
        source: Where the payload came from, recorded on the catalogue.
        public_key: When set, the catalogue must carry a valid Ed25519 signature.

    Returns:
        The validated catalogue. A refused catalogue comes back empty with
        :attr:`SponsorCatalog.error` explaining why.
    """
    if not isinstance(payload, dict):
        return SponsorCatalog(source=source, error="catalogue is not a JSON object")
    if public_key and not verify_signature(payload, public_key):
        return SponsorCatalog(source=source, error="catalogue signature is missing or invalid")
    entries: List[SponsorEntry] = []
    for raw in payload.get("entries") or []:
        if not isinstance(raw, dict):
            LOGGER.warning("Skipping a sponsor entry that is not an object (source %s)", source)
            continue
        try:
            entries.append(_entry_from_payload(raw, int(payload.get("cpm_cents", DEFAULT_CPM_CENTS))))
        except SponsorCatalogError as exc:
            LOGGER.warning("Refused a sponsor entry from %s: %s", source, exc)
    return SponsorCatalog(
        entries=entries,
        version=int(payload.get("version", 1)),
        updated_at=str(payload.get("updated_at") or ""),
        source=source,
        error="",
    )


def load_catalog_file(path: str, *, public_key: str = "") -> SponsorCatalog:
    """Load a catalogue from disk.

    Args:
        path: Filesystem path to the JSON catalogue.
        public_key: Optional Ed25519 public key that must have signed it.

    Returns:
        The catalogue, or an empty one carrying the error when the file is
        missing or unreadable. Never raises for I/O: a broken catalogue must not
        stop an agent run.
    """
    target = Path(path).expanduser()
    source = f"path:{target}"
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        LOGGER.warning("Sponsor catalogue %s does not exist", target)
        return SponsorCatalog(source=source, error=f"catalogue not found: {target}")
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.error("Could not read sponsor catalogue %s: %s", target, exc)
        return SponsorCatalog(source=source, error=f"catalogue unreadable: {exc}")
    return parse_catalog(payload, source=source, public_key=public_key)


@async_retry(
    max_retries=3,
    retry_on=(httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError),
)
async def fetch_catalog(
    url: str,
    *,
    settings: Optional[Settings] = None,
    public_key: str = "",
) -> SponsorCatalog:
    """Fetch and validate a catalogue over HTTPS.

    Args:
        url: Catalogue endpoint returning the JSON described in this module.
        settings: Optional settings override (TLS trust, timeouts).
        public_key: Optional Ed25519 public key that must have signed it.

    Returns:
        The catalogue, or an empty one carrying the error. Network failures are
        retried three times with exponential backoff, then reported.
    """
    resolved = settings or get_settings()
    try:
        async with httpx.AsyncClient(
            **async_client_kwargs(resolved, timeout=resolved.SPONSOR_REQUEST_TIMEOUT)
        ) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001 - a catalogue is never worth a crash
        LOGGER.error("Could not fetch the sponsor catalogue from %s: %s", url, exc)
        return SponsorCatalog(source=f"url:{url}", error=f"catalogue fetch failed: {exc}")
    return parse_catalog(payload, source=f"url:{url}", public_key=public_key)


async def load_catalog(settings: Optional[Settings] = None) -> SponsorCatalog:
    """Load the configured catalogue, from disk first and then from the network.

    Args:
        settings: Optional settings override.

    Returns:
        The catalogue. With nothing configured this is an empty catalogue whose
        ``source`` is ``"none"`` -- the honest default: Kollektiv ships with no
        ads of any kind.
    """
    resolved = settings or get_settings()
    if resolved.SPONSOR_CATALOG_PATH:
        return load_catalog_file(resolved.SPONSOR_CATALOG_PATH, public_key=resolved.SPONSOR_CATALOG_PUBLIC_KEY)
    if resolved.SPONSOR_CATALOG_URL:
        return await fetch_catalog(
            resolved.SPONSOR_CATALOG_URL,
            settings=resolved,
            public_key=resolved.SPONSOR_CATALOG_PUBLIC_KEY,
        )
    return SponsorCatalog(source="none")


def split_categories(value: str) -> List[str]:
    """Turn ``"databases, ai"`` into ``["databases", "ai"]``.

    Args:
        value: Comma separated categories from the settings or a query string.

    Returns:
        Normalised, de-duplicated categories in the order given.
    """
    seen: List[str] = []
    for item in str(value or "").split(","):
        category = item.strip().lower()
        if category and category not in seen:
            seen.append(category)
    return seen


def eligible_entries(catalog: SponsorCatalog, categories: Sequence[str]) -> List[SponsorEntry]:
    """Return the entries that may be shown to someone with these interests.

    Args:
        catalog: The loaded catalogue.
        categories: The caller's self-declared interests (possibly empty).

    Returns:
        Entries whose ``categories`` is empty (untargeted) or intersects the
        caller's list. An empty category list therefore still yields the
        untargeted half of the catalogue rather than nothing.
    """
    wanted = {category.lower() for category in categories}
    if not wanted:
        return [entry for entry in catalog.entries if not entry.categories]
    return [entry for entry in catalog.entries if not entry.categories or wanted & set(entry.categories)]


def choose(
    entries: Sequence[SponsorEntry],
    *,
    bucket: int,
    categories: Sequence[str] = (),
    salt: str = "kollektiv.sponsor.v1",
) -> Optional[SponsorEntry]:
    """Pick a line deterministically, weighted by ``weight``.

    The pick is an HMAC of the time bucket and the caller's categories, so it is
    reproducible (a bug report can say "at bucket X you get Y") and involves no
    randomness source, no device fingerprint and no server round trip.

    Args:
        entries: Candidate entries, usually from :func:`eligible_entries`.
        bucket: Time bucket, typically ``int(time.time() // interval)``.
        categories: The caller's self-declared interests, part of the key so
            two interests can rotate independently.
        salt: Domain separation for the HMAC key.

    Returns:
        The chosen entry, or ``None`` when there is nothing to show.
    """
    if not entries:
        return None
    total = sum(max(1, entry.weight) for entry in entries)
    key = salt.encode("utf-8")
    message = f"{bucket}:{','.join(sorted(category.lower() for category in categories))}".encode()
    digest = hmac.new(key, message, hashlib.sha256).digest()
    target = int.from_bytes(digest[:8], "big") % total
    running = 0
    for entry in entries:
        running += max(1, entry.weight)
        if target < running:
            return entry
    return entries[-1]  # pragma: no cover - arithmetic guard


__all__ = [
    "DEFAULT_CPM_CENTS",
    "FORBIDDEN_MARKERS",
    "MAX_TEXT_LENGTH",
    "MIN_TEXT_LENGTH",
    "SponsorCatalog",
    "SponsorCatalogError",
    "SponsorEntry",
    "canonical_payload",
    "choose",
    "eligible_entries",
    "fetch_catalog",
    "load_catalog",
    "load_catalog_file",
    "normalise_text",
    "normalise_url",
    "parse_catalog",
    "split_categories",
    "verify_signature",
]
