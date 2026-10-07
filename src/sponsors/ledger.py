"""The sponsor ledger: what the opt-in sponsor line has earned, on your machine.

Kollektiv does not phone home. The ledger is a local tally -- impressions per
sponsor, the gross value at the catalogue's published rate, and the share that
belongs to the developer (``SPONSOR_SHARE_BP``, 75% by default). A payout uses a
*signed claim*: :meth:`SponsorLedger.claim` produces a two-part token that
states exactly one thing -- "this deployment saw N lines from sponsor X, worth M
cents" -- and nothing else. Nobody sees that number until the operator decides,
deliberately, to send it to someone, and the only identity involved is whatever
``payout_to`` string they choose to put in it.

Integer arithmetic is deliberate. Money is stored in *millicents* (1/1000 of a
cent): a rate of 120 cents per 1000 impressions and a 7500 basis-point share
therefore round exactly once, at the edge, and never drift while they accrue.

Two honest caveats, written here rather than buried in the docs:

* A local tally is self-reported. It is fine for the developer-first model --
  the sponsor pays the person who actually looked at the line -- and useless as
  an anti-fraud mechanism. The hosted mode described in
  ``docs/monetization.md`` counts impressions server-side instead.
* The ledger stores per-sponsor totals, never a trail. There is no timestamped
  log of what you were doing, because there does not need to be one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import select

from config.settings import Settings, get_settings
from src.db.models import SponsorLedgerRecord, session_scope
from src.sponsors.catalog import SponsorEntry
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)

#: Domain separator for the HMAC key derived from ``SECRET_KEY``.
CLAIM_KEY_SALT = b"kollektiv.sponsor.claim.v1"

#: Claim format version.
CLAIM_VERSION = 1


def _b64url_encode(raw: bytes) -> str:
    """Return unpadded URL-safe base64 for ``raw``."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    """Decode unpadded URL-safe base64, tolerating missing padding."""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def claim_key(secret: str) -> bytes:
    """Derive the HMAC key used to sign claims.

    Args:
        secret: The deployment's ``SECRET_KEY``.

    Returns:
        A 32-byte key. An empty secret still produces a key (the API and CLI
        both warn about an empty secret elsewhere); it just is not a secret
        worth trusting.
    """
    return hashlib.sha256(CLAIM_KEY_SALT + (secret or "").encode("utf-8")).digest()


def sign_claim(payload: Dict[str, Any], secret: str) -> str:
    """Sign a claim payload, producing ``payload.signature``.

    Args:
        payload: The claim statement (see :meth:`SponsorLedger.claim`).
        secret: The deployment's ``SECRET_KEY``.

    Returns:
        An unpadded base64url two-part token.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    signature = hmac.new(claim_key(secret), body, hashlib.sha256).digest()
    return f"{_b64url_encode(body)}.{_b64url_encode(signature)}"


def verify_claim(token: str, secret: str) -> Dict[str, Any]:
    """Verify and decode a claim token.

    Args:
        token: The token produced by :func:`sign_claim`.
        secret: The deployment's ``SECRET_KEY``.

    Returns:
        The decoded claim payload.

    Raises:
        ValueError: When the token is malformed or the signature does not match.
    """
    parts = str(token or "").split(".")
    if len(parts) != 2:
        raise ValueError("claim token must have two dot-separated parts")
    body = _b64url_decode(parts[0])
    expected = hmac.new(claim_key(secret), body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _b64url_decode(parts[1])):
        raise ValueError("claim token signature does not match this SECRET_KEY")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("claim token does not carry a JSON object")
    return payload


def millicents_to_cents(value: int) -> int:
    """Round millicents to whole cents (banker-free: half up, far from zero).

    Args:
        value: An amount in 1/1000 cents.

    Returns:
        The amount in cents, rounded to the nearest cent.
    """
    if value >= 0:
        return (value + 500) // 1000
    return -((-value + 500) // 1000)


class SponsorLedger:
    """Async facade over the ``sponsor_ledger`` table.

    Attributes:
        settings: The settings used for rates and thresholds.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        """Store the settings; no I/O happens here.

        Args:
            settings: Optional settings override.
        """
        self.settings = settings or get_settings()

    # -- writes --------------------------------------------------------
    async def record(
        self,
        entry: SponsorEntry,
        *,
        impressions: int = 1,
        share_bp: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Accrue ``impressions`` of ``entry`` into the local tally.

        Args:
            entry: The sponsor whose line was shown.
            impressions: How many lines were shown (positive integer).
            share_bp: Developer share in basis points; defaults to
                ``SPONSOR_SHARE_BP``.
            now: Timestamp override, for tests.

        Returns:
            The updated row as a dict, with whole-cent amounts included.

        Raises:
            ValueError: When ``impressions`` is not positive.
        """
        if impressions <= 0:
            raise ValueError("impressions must be positive")
        share = self.settings.SPONSOR_SHARE_BP if share_bp is None else share_bp
        share = max(0, min(10000, int(share)))
        rate = int(entry.cpm_cents or self.settings.SPONSOR_CPM_CENTS)
        gross = impressions * max(0, rate)
        net = gross * share // 10000
        stamp = now or datetime.now(UTC)

        with session_scope() as session:
            row = session.get(SponsorLedgerRecord, entry.sponsor_id)
            if row is None:
                row = SponsorLedgerRecord(
                    sponsor_id=entry.sponsor_id,
                    advertiser=entry.advertiser,
                    impressions=impressions,
                    gross_millicents=gross,
                    net_millicents=net,
                    share_bp=share,
                    first_seen=stamp,
                    updated_at=stamp,
                )
                session.add(row)
            else:
                row.advertiser = entry.advertiser or row.advertiser
                row.impressions += impressions
                row.gross_millicents += gross
                row.net_millicents += net
                row.share_bp = share
                row.updated_at = stamp
            session.commit()
            session.refresh(row)
            result = self._row_to_dict(row)
        LOGGER.debug(
            "Sponsor %s accrued %s impression(s): %s net millicents",
            entry.sponsor_id,
            impressions,
            net,
        )
        return result

    async def forget(self) -> int:
        """Delete every ledger row.

        Returns:
            The number of sponsors forgotten. Nothing is archived and nothing
            is sent anywhere: the tally stops existing.
        """
        with session_scope() as session:
            records = list(session.exec(select(SponsorLedgerRecord)).all())
            for record in records:
                session.delete(record)
            session.commit()
        LOGGER.info("Deleted %s sponsor ledger row(s)", len(records))
        return len(records)

    # -- reads ---------------------------------------------------------
    async def rows(self) -> List[Dict[str, Any]]:
        """Return every ledger row as a dict, newest activity first."""
        with session_scope() as session:
            records = list(session.exec(select(SponsorLedgerRecord)).all())
        records.sort(key=lambda row: row.updated_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return [self._row_to_dict(record) for record in records]

    async def summary(self) -> Dict[str, Any]:
        """Return totals, the payout threshold and whether a claim is possible.

        A deployment that has never initialised its database answers with an
        empty, zeroed summary and an ``error`` string instead of raising: the
        status command is exactly what you run when something is wrong.
        """
        try:
            rows = await self.rows()
        except SQLAlchemyError as exc:
            LOGGER.warning("Could not read the sponsor ledger: %s", exc)
            rows = []
            rows_error = str(exc).splitlines()[0]
        else:
            rows_error = ""
        gross = sum(row["gross_millicents"] for row in rows)
        net = sum(row["net_millicents"] for row in rows)
        threshold = int(self.settings.SPONSOR_MIN_PAYOUT_CENTS)
        net_cents = millicents_to_cents(net)
        return {
            "enabled": bool(self.settings.SPONSORS_ENABLED),
            "share_bp": int(self.settings.SPONSOR_SHARE_BP),
            "currency": "USD",
            "sponsors": len(rows),
            "impressions": sum(row["impressions"] for row in rows),
            "gross_millicents": gross,
            "net_millicents": net,
            "gross_cents": millicents_to_cents(gross),
            "net_cents": net_cents,
            "min_payout_cents": threshold,
            "claimable": net_cents >= threshold,
            "error": rows_error,
            "rows": rows,
        }

    async def claim(self, *, payout_to: str = "", note: str = "") -> Dict[str, Any]:
        """Build a signed statement of what this deployment is owed.

        Nothing is transmitted: the caller decides who (if anyone) receives the
        token. ``payout_to`` is the only identity in the payload and it is
        empty unless the operator types one in.

        Args:
            payout_to: Optional payout handle (email, lightning address, ...).
            note: Optional free-text note for the sponsor.

        Returns:
            ``{claim, payload, redeem}`` where ``redeem`` lists the plain steps
            a person can follow.

        Raises:
            ValueError: When the accrued share is below ``SPONSOR_MIN_PAYOUT_CENTS``.
        """
        summary = await self.summary()
        threshold = int(summary["min_payout_cents"])
        if not summary["claimable"]:
            shortfall = threshold - int(summary["net_cents"])
            raise ValueError(
                f"nothing to claim yet: {summary['net_cents']} of {threshold} cents "
                f"({shortfall} to go). Keep the line on, or lower SPONSOR_MIN_PAYOUT_CENTS."
            )
        payload: Dict[str, Any] = {
            "v": CLAIM_VERSION,
            "kind": "kollektiv.sponsor.claim",
            "issued_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "currency": "USD",
            "share_bp": summary["share_bp"],
            "total_impressions": summary["impressions"],
            "gross_cents": summary["gross_cents"],
            "net_cents": summary["net_cents"],
            "sponsors": [
                {
                    "id": row["sponsor_id"],
                    "advertiser": row.get("advertiser", ""),
                    "impressions": row["impressions"],
                    "net_cents": row["net_cents"],
                }
                for row in summary["rows"]
            ],
            "payout_to": payout_to,
            "note": note,
        }
        token = sign_claim(payload, self.settings.SECRET_KEY)
        LOGGER.info("Built a sponsor claim for %s cents", payload["net_cents"])
        return {
            "claim": token,
            "payload": payload,
            "redeem": [
                "Send the claim token (or its payload) to whoever runs your catalogue of sponsors.",
                "They verify it with the same SECRET_KEY: python -c "
                "\"from src.sponsors import verify_claim; print(verify_claim(open('claim.txt').read().strip(), 'SECRET_KEY'))\"",
                "They pay the amount in net_cents to the payout_to you supplied.",
                "Run 'kollektiv sponsors forget' afterwards to wipe the local tally, if you want to.",
            ],
        }

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _row_to_dict(row: SponsorLedgerRecord) -> Dict[str, Any]:
        """Render a ledger row as a dict with whole-cent amounts."""
        return {
            "sponsor_id": row.sponsor_id,
            "advertiser": row.advertiser,
            "impressions": row.impressions,
            "gross_millicents": row.gross_millicents,
            "net_millicents": row.net_millicents,
            "gross_cents": millicents_to_cents(row.gross_millicents),
            "net_cents": millicents_to_cents(row.net_millicents),
            "share_bp": row.share_bp,
            "first_seen": row.first_seen.isoformat() if row.first_seen else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }


__all__ = [
    "CLAIM_KEY_SALT",
    "CLAIM_VERSION",
    "SponsorLedger",
    "claim_key",
    "millicents_to_cents",
    "sign_claim",
    "verify_claim",
]
