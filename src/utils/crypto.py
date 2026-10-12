"""Symmetric encryption helpers built on :mod:`cryptography` Fernet.

Fernet keys are derived deterministically from ``SECRET_KEY`` so that the
same secret always decrypts previously stored tokens (rotating the secret
therefore invalidates stored tokens -- re-run the login flow afterwards).

Usage::

    cipher = TokenCipher.from_secret(settings.fernet_secret)
    blob = cipher.encrypt_json({"access_token": "..."})
    data = cipher.decrypt_json(blob)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

from src.utils.errors import ConfigurationError
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)


def derive_fernet_key(secret: str, salt: bytes = b"kollektiv.fernet.v1") -> bytes:
    """Derive a 32 byte urlsafe base64 Fernet key from an arbitrary secret.

    Args:
        secret: The application secret (``SECRET_KEY``).
        salt: Fixed domain separation salt.

    Returns:
        A Fernet compatible key.

    Raises:
        ConfigurationError: If ``secret`` is empty.
    """
    if not secret:
        raise ConfigurationError("Cannot derive an encryption key from an empty secret")
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 100_000, dklen=32)
    return base64.urlsafe_b64encode(digest)


def generate_secret_key(nbytes: int = 32) -> str:
    """Return a fresh random secret suitable for ``SECRET_KEY``."""
    return base64.urlsafe_b64encode(os.urandom(nbytes)).decode("ascii")


class TokenCipher:
    """Thin wrapper around :class:`cryptography.fernet.Fernet`.

    Args:
        key: A Fernet key (already derived) -- use :meth:`from_secret` to
            derive one from a human supplied password.
    """

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def from_secret(cls, secret: str) -> "TokenCipher":
        """Build a cipher from an arbitrary secret string."""
        return cls(derive_fernet_key(secret))

    @property
    def fernet(self) -> Fernet:
        """The underlying Fernet instance."""
        return self._fernet

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string, returning an ASCII token.

        Args:
            plaintext: Value to encrypt.

        Returns:
            The encrypted token as a ``str`` (utf-8 decode of the ciphertext).
        """
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> str:
        """Decrypt a token produced by :meth:`encrypt`.

        Args:
            token: The encrypted token.

        Returns:
            The original plaintext.

        Raises:
            InvalidToken: If the token is corrupt or was encrypted with a
                different secret.
        """
        return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")

    def encrypt_json(self, payload: Dict[str, Any]) -> str:
        """Serialise ``payload`` to JSON and encrypt it."""
        return self.encrypt(json.dumps(payload, default=str, sort_keys=True))

    def decrypt_json(self, token: str) -> Dict[str, Any]:
        """Decrypt ``token`` and parse it as a JSON object.

        Returns an empty dict when the token cannot be decrypted, logging the
        failure instead of raising -- callers usually treat a missing token
        the same way as an expired one.
        """
        try:
            raw = self.decrypt(token)
        except (InvalidToken, ValueError) as exc:
            LOGGER.error("Stored token could not be decrypted: %s", exc)
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            LOGGER.error("Decrypted token is not valid JSON: %s", exc)
            return {}
        return data if isinstance(data, dict) else {}

    def try_decrypt_json(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        """Like :meth:`decrypt_json` but returns ``None`` for empty input."""
        if not token:
            return None
        return self.decrypt_json(token)


def mask_secret(value: Optional[str], keep: int = 4) -> str:
    """Return a redacted representation of ``value`` for logs.

    Args:
        value: The secret to mask.
        keep: Number of leading characters to keep visible.

    Returns:
        ``"***"`` for empty input, otherwise ``"abcd*** (len=42)"``.
    """
    if not value:
        return "***"
    head = value[:keep] if len(value) > keep else ""
    return f"{head}*** (len={len(value)})"


def mask_email(value: Optional[str]) -> str:
    """Partially redact an email address for logs.

    Args:
        value: Email address.

    Returns:
        ``"a***@example.com"`` style string, or ``"***"`` when empty.
    """
    if not value or "@" not in value:
        return "***"
    local, _, domain = value.partition("@")
    return f"{local[:1]}***@{domain}"


__all__ = [
    "TokenCipher",
    "derive_fernet_key",
    "generate_secret_key",
    "mask_secret",
    "mask_email",
]
