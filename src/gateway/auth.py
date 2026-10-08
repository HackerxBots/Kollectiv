"""Gateway authentication: per-client tokens, stored encrypted, verifiable once.

Every client (Claude Code, Codex, Cursor, the dashboard, a teammate's script)
gets its own token. Tokens are generated with :func:`secrets.token_urlsafe`,
shown **exactly once** at creation, and stored through the project's
:class:`~src.utils.token_store.TokenStore` — encrypted with Fernet, like every
other credential in Kollektiv (rules are easier to keep when there are no
exceptions). The client row carries the policy, not the secret.

Verification decrypts stored tokens and compares in constant time. That is
deliberately slower than hashing and deliberately simpler to audit: there is one
place a secret lives, it is encrypted at rest, and ``kollektiv gateway token
revoke`` deletes it.

Tokens are prefixed ``kgw_`` so a leaked string is recognisable in a log or a
screenshot, and are never written to the audit log.
"""

from __future__ import annotations

import hmac
import secrets
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

from sqlmodel import select

from config.settings import Settings, get_settings
from src.db.models import GatewayClientRecord, session_scope
from src.gateway.policy import POLICY_PRESETS, Policy, load_policy_file, resolve_policy
from src.utils.errors import ConfigurationError
from src.utils.logger import get_logger
from src.utils.token_store import TokenStore

LOGGER = get_logger(__name__)

#: Token prefix, so a stray token is identifiable at a glance.
TOKEN_PREFIX = "kgw_"
#: Service name used in the encrypted token store.
TOKEN_SERVICE = "gateway"
#: Roles a client can have. Each maps to the policy preset of the same name;
#: ``client`` is the friendly alias for the dashboard preset. Preset names are
#: valid roles on purpose: ``kollektiv gateway init --role read-only`` should
#: work without the operator learning a second vocabulary.
ROLES = ("admin", "dashboard", "worker", "messenger", "read-only", "client")


class GatewayAuth:
    """Async facade over the client table and the encrypted token store.

    Attributes:
        settings: Settings used for defaults and the token store.
    """

    def __init__(self, settings: Optional[Settings] = None, token_store: Any = None) -> None:
        """Store the settings and prepare the token store.

        Args:
            settings: Optional settings override.
            token_store: Optional TokenStore (tests inject a fake one). The real
                store takes the Fernet secret, not the settings object.
        """
        self.settings = settings or get_settings()
        self.tokens: Any = token_store or TokenStore(self.settings.fernet_secret)

    # ------------------------------------------------------------------
    # Issuing and revoking
    # ------------------------------------------------------------------
    async def issue(
        self,
        name: str,
        *,
        label: str = "",
        role: str = "client",
        policy: Optional[Policy] = None,
        rotate: bool = False,
    ) -> Dict[str, Any]:
        """Create (or re-key) a client and return its token, once.

        Args:
            name: Client name, unique and used in the audit log.
            label: Human description (``"Ada's laptop"``).
            role: One of :data:`ROLES`; decides the default policy.
            policy: Explicit policy; overrides the role's preset.
            rotate: Replace the token of an existing client instead of failing.

        Returns:
            ``{name, role, token, policy, created}`` — ``token`` is the only
            time the secret is visible.

        Raises:
            ConfigurationError: When the client exists and ``rotate`` is false.
        """
        clean = (name or "").strip()
        if not clean:
            raise ConfigurationError("a gateway client needs a name")
        if role not in ROLES:
            raise ConfigurationError(f"unknown gateway role {role!r}; choose one of {', '.join(ROLES)}")
        token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        chosen = policy or Policy.preset(role if role in POLICY_PRESETS else "dashboard")

        with session_scope() as session:
            row = session.get(GatewayClientRecord, clean)
            created = row is None
            if row is None:
                row = GatewayClientRecord(name=clean, label=label or clean, role=role, policy=chosen.to_json())
                session.add(row)
            elif not rotate:
                raise ConfigurationError(
                    f"gateway client {clean!r} already exists; use rotate=True (CLI: --rotate) to re-key it"
                )
            else:
                row.role = role
                row.policy = chosen.to_json()
                row.active = True
                if label:
                    row.label = label
            session.commit()

        await self.tokens.asave_token(TOKEN_SERVICE, clean, {"access_token": token, "role": role})
        self._forget_cache()
        LOGGER.info("Issued a gateway token for %s (role %s)", clean, role)
        return {
            "name": clean,
            "role": role,
            "token": token,
            "policy": chosen.to_dict(),
            "created": created,
        }

    async def revoke(self, name: str) -> bool:
        """Delete a client and its token.

        Args:
            name: The client to remove.

        Returns:
            ``True`` when something was removed.
        """
        removed = False
        with session_scope() as session:
            row = session.get(GatewayClientRecord, name)
            if row is not None:
                session.delete(row)
                session.commit()
                removed = True
        try:
            if await self.tokens.adelete_token(TOKEN_SERVICE, name):
                removed = True
        except Exception as exc:  # noqa: BLE001 - a missing token is not a failure
            LOGGER.debug("No stored gateway token to delete for %s: %s", name, exc)
        self._forget_cache()
        if removed:
            LOGGER.info("Revoked the gateway token for %s", name)
        return removed

    def _forget_cache(self) -> None:
        """Drop the token store's in-process cache after a write.

        Rotation and revocation must take effect in this process too, but a
        third-party store is not required to implement the cache at all — so the
        call is optional, and its absence is only worth a debug line.
        """
        forget = getattr(self.tokens, "clear_cache", None)
        if callable(forget):
            forget()
        else:  # pragma: no cover - only fakes and exotic stores
            LOGGER.debug("Token store %s has no clear_cache(); relying on re-reads", type(self.tokens).__name__)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    async def clients(self) -> List[Dict[str, Any]]:
        """Return every client row (never the secret), newest first."""
        with session_scope() as session:
            rows = list(session.exec(select(GatewayClientRecord)).all())
        rows.sort(key=lambda row: row.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        file_policies = load_policy_file(self.settings.GATEWAY_POLICY_PATH) if self.settings.GATEWAY_POLICY_PATH else {}
        result: List[Dict[str, Any]] = []
        for row in rows:
            policy = resolve_policy(row.name, row.policy, file_policies=file_policies, role=row.role)
            result.append(
                {
                    "name": row.name,
                    "label": row.label,
                    "role": row.role,
                    "active": row.active,
                    "calls": row.calls,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "last_seen": row.last_seen.isoformat() if row.last_seen else None,
                    "policy": policy.to_dict(),
                }
            )
        return result

    async def get(self, name: str) -> Optional[GatewayClientRecord]:
        """Return one client row, or ``None``.

        Args:
            name: Client name.

        Returns:
            The row when it exists and is active.
        """
        with session_scope() as session:
            row = session.get(GatewayClientRecord, name)
        return row if row is not None and row.active else None

    def policy_for(self, row: GatewayClientRecord) -> Policy:
        """Resolve the effective policy for a client row.

        Args:
            row: The client row.

        Returns:
            The policy to enforce (file overrides stored overrides role preset).
        """
        file_policies = load_policy_file(self.settings.GATEWAY_POLICY_PATH) if self.settings.GATEWAY_POLICY_PATH else {}
        return resolve_policy(row.name, row.policy, file_policies=file_policies, role=row.role)

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------
    async def authenticate(self, token: str) -> Optional[Dict[str, Any]]:
        """Resolve a bearer token to a client and its policy.

        Args:
            token: The raw token from the ``Authorization: Bearer`` header.

        Returns:
            ``{client, role, policy}``, or ``None`` when the token is unknown,
            malformed or revoked. Comparison is constant-time per candidate.
        """
        presented = (token or "").strip()
        if not presented.startswith(TOKEN_PREFIX):
            return None
        with session_scope() as session:
            rows = [row for row in session.exec(select(GatewayClientRecord)).all() if row.active]
        for row in rows:
            try:
                stored = await self.tokens.aget_token(TOKEN_SERVICE, row.name)
            except Exception as exc:  # noqa: BLE001 - a missing token is normal
                LOGGER.debug("No stored gateway token for %s: %s", row.name, exc)
                continue
            candidate = str((stored or {}).get("access_token") or "")
            if candidate and hmac.compare_digest(candidate, presented):
                return {"client": row.name, "role": row.role, "policy": self.policy_for(row)}
        LOGGER.warning("Rejected an unknown gateway token (prefix %s…)", presented[:8])
        return None

    async def touch(self, name: str, *, milliseconds: int = 0) -> None:
        """Record that a client just called something.

        Args:
            name: Client name.
            milliseconds: Duration of the call, added to the client's total.
        """
        try:
            with session_scope() as session:
                row = session.get(GatewayClientRecord, name)
                if row is None:
                    return
                row.last_seen = datetime.now(UTC)
                row.calls += 1
                session.add(row)
                session.commit()
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never fail a call
            LOGGER.debug("Could not record gateway activity for %s: %s", name, exc)


__all__ = ["ROLES", "TOKEN_PREFIX", "TOKEN_SERVICE", "GatewayAuth"]
