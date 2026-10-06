"""Encrypted token storage backed by SQLite.

Every credential Kollektiv touches -- TeraBox OAuth tokens, worker session
tokens -- is encrypted with Fernet before it reaches the database. Plaintext
credentials never touch disk.

The store is deliberately synchronous (SQLite is fast and the calls are
tiny) but exposes async wrappers (:meth:`TokenStore.asave_token` etc.) so
async callers do not have to think about it.

Usage::

    store = TokenStore()
    store.save_token("terabox", "acct-1", {"access_token": "..."})
    tokens = store.get_token("terabox", "acct-1")
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlmodel import select

from src.db.models import TokenRecord, get_engine, init_db, session_scope, utcnow
from src.utils.crypto import TokenCipher
from src.utils.errors import ConfigurationError
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)

#: Services this store knows about (free-form, but these are the built-ins).
SERVICE_TERABOX = "terabox"
SERVICE_ARENA = "arena"


class TokenStore:
    """Encrypted, per-account token persistence.

    Args:
        secret: Secret used to derive the Fernet key. Defaults to
            ``settings.fernet_secret``.
        engine: Optional SQLModel engine override (tests use an in-memory one).
        cache: Keep decrypted tokens in memory for the process lifetime.
            This *only* affects performance, not durability.
    """

    def __init__(
        self,
        secret: Optional[str] = None,
        engine: Any = None,
        cache: bool = True,
    ) -> None:
        from config.settings import get_settings

        settings = get_settings()
        self._engine = engine or get_engine()
        try:
            self._cipher = TokenCipher.from_secret(secret or settings.fernet_secret)
        except ConfigurationError as exc:  # pragma: no cover - defensive
            LOGGER.error("Token encryption unavailable: %s", exc)
            raise
        self._cache: Dict[tuple[str, str], Dict[str, Any]] = {}
        self._cache_enabled = cache
        init_db(self._engine)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _cache_key(self, service: str, account_id: str) -> tuple[str, str]:
        return (service.strip().lower(), account_id.strip())

    def _invalidate(self, service: str, account_id: str) -> None:
        self._cache.pop(self._cache_key(service, account_id), None)

    # ------------------------------------------------------------------
    # Sync API
    # ------------------------------------------------------------------
    def save_token(self, service: str, account_id: str, token_data: Dict[str, Any]) -> None:
        """Insert or replace the encrypted token for ``(service, account_id)``.

        Args:
            service: Logical service name, e.g. ``"terabox"``.
            account_id: Stable account identifier.
            token_data: Arbitrary JSON-serialisable credentials. The optional
                ``expires_in`` key (seconds) is also stored as ``expires_at``.
        """
        payload = self._cipher.encrypt_json(token_data)
        expires_at = self._expiry_from(token_data)
        with session_scope(self._engine) as session:
            existing = self._fetch(session, service, account_id)
            if existing is None:
                session.add(
                    TokenRecord(
                        service=service.strip().lower(),
                        account_id=account_id.strip(),
                        payload=payload,
                        expires_at=expires_at,
                    )
                )
            else:
                existing.payload = payload
                existing.expires_at = expires_at
                existing.updated_at = utcnow()
                session.add(existing)
        if self._cache_enabled:
            self._cache[self._cache_key(service, account_id)] = dict(token_data)
        LOGGER.info("Stored encrypted token for %s/%s", service, account_id)

    def get_token(self, service: str, account_id: str) -> Dict[str, Any]:
        """Return the decrypted token dict, or ``{}`` when absent.

        Args:
            service: Logical service name.
            account_id: Stable account identifier.

        Returns:
            The stored credentials, or an empty dict.
        """
        key = self._cache_key(service, account_id)
        if self._cache_enabled and key in self._cache:
            return dict(self._cache[key])
        with session_scope(self._engine) as session:
            record = self._fetch(session, service, account_id)
        if record is None:
            return {}
        data = self._cipher.decrypt_json(record.payload)
        if self._cache_enabled and data:
            self._cache[key] = dict(data)
        return data

    def update_token(self, service: str, account_id: str, token_data: Dict[str, Any]) -> None:
        """Merge ``token_data`` into the stored token (creating it if needed)."""
        merged = self.get_token(service, account_id)
        merged.update(token_data)
        self.save_token(service, account_id, merged)

    def delete_token(self, service: str, account_id: str) -> bool:
        """Delete the stored token.

        Returns:
            ``True`` when a row was removed, ``False`` when nothing matched.
        """
        removed = False
        with session_scope(self._engine) as session:
            record = self._fetch(session, service, account_id)
            if record is not None:
                session.delete(record)
                removed = True
        self._invalidate(service, account_id)
        if removed:
            LOGGER.info("Deleted token for %s/%s", service, account_id)
        return removed

    def list_tokens(self, service: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return metadata (never secrets) about stored tokens.

        Args:
            service: Optional service filter.

        Returns:
            List of ``{service, account_id, updated_at, expires_at, expired}``.
        """
        statement = select(TokenRecord)
        if service:
            statement = statement.where(TokenRecord.service == service.strip().lower())
        with session_scope(self._engine) as session:
            records = list(session.exec(statement).all())
        return [
            {
                "service": record.service,
                "account_id": record.account_id,
                "updated_at": record.updated_at.isoformat() if record.updated_at else None,
                "expires_at": record.expires_at.isoformat() if record.expires_at else None,
                "expired": self.is_expired(record.expires_at),
            }
            for record in records
        ]

    def has_token(self, service: str, account_id: str) -> bool:
        """Return ``True`` when a token exists for the pair (does not decrypt)."""
        with session_scope(self._engine) as session:
            return self._fetch(session, service, account_id) is not None

    def clear_cache(self) -> None:
        """Drop the in-memory decrypted token cache."""
        self._cache.clear()

    # ------------------------------------------------------------------
    # Async wrappers
    # ------------------------------------------------------------------
    async def asave_token(self, service: str, account_id: str, token_data: Dict[str, Any]) -> None:
        """Async wrapper around :meth:`save_token`."""
        await asyncio.to_thread(self.save_token, service, account_id, token_data)

    async def aget_token(self, service: str, account_id: str) -> Dict[str, Any]:
        """Async wrapper around :meth:`get_token`."""
        return await asyncio.to_thread(self.get_token, service, account_id)

    async def aupdate_token(self, service: str, account_id: str, token_data: Dict[str, Any]) -> None:
        """Async wrapper around :meth:`update_token`."""
        await asyncio.to_thread(self.update_token, service, account_id, token_data)

    async def adelete_token(self, service: str, account_id: str) -> bool:
        """Async wrapper around :meth:`delete_token`."""
        return await asyncio.to_thread(self.delete_token, service, account_id)

    async def alist_tokens(self, service: Optional[str] = None) -> List[Dict[str, Any]]:
        """Async wrapper around :meth:`list_tokens`."""
        return await asyncio.to_thread(self.list_tokens, service)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _fetch(session: Any, service: str, account_id: str) -> Optional[TokenRecord]:
        """Fetch a single token row inside the given session."""
        statement = select(TokenRecord).where(
            TokenRecord.service == service.strip().lower(),
            TokenRecord.account_id == account_id.strip(),
        )
        return session.exec(statement).first()

    @staticmethod
    def _expiry_from(token_data: Dict[str, Any]) -> Optional[datetime]:
        """Derive an absolute expiry timestamp from ``expires_in`` seconds."""
        seconds = token_data.get("expires_in")
        if seconds is None:
            raw_expiry = token_data.get("expires_at")
            if isinstance(raw_expiry, str):
                try:
                    return datetime.fromisoformat(raw_expiry)
                except ValueError:
                    return None
            return None
        try:
            return utcnow() + timedelta(seconds=float(seconds))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def is_expired(expires_at: Optional[datetime], skew_seconds: int = 60) -> bool:
        """Return ``True`` when ``expires_at`` is in the past (with safety skew)."""
        if expires_at is None:
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= utcnow() + timedelta(seconds=skew_seconds)


__all__ = ["TokenStore", "SERVICE_TERABOX", "SERVICE_ARENA"]
