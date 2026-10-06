"""HTTP client helpers shared by every Kollektiv API client.

The only non-obvious part is TLS trust. Kollektiv talks to three third-party
services from potentially three different environments (a laptop, a container,
a corporate network with a TLS-inspecting proxy). Rather than hard-coding a CA
bundle, :func:`resolve_verify` follows the conventions every Python tool
already uses:

1. ``KOLLEKTIV_CA_BUNDLE`` -- Kollektiv specific override.
2. ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` / ``CURL_CA_BUNDLE`` -- the
   standard environment variables.
3. ``SSL_CA_BUNDLE`` from ``.env``.
4. ``certifi``'s bundle (httpx default).

Set ``HTTP_SSL_VERIFY=false`` only for local debugging against a self-signed
endpoint; never in production.

Usage::

    from src.utils.net import async_client_kwargs

    client = httpx.AsyncClient(**async_client_kwargs(settings, base_url="https://api.github.com"))
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Union

from config.settings import Settings, get_settings
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)

#: Environment variables consulted, in order, when no explicit bundle is set.
CA_BUNDLE_ENV_VARS = ("KOLLEKTIV_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")

#: Default User-Agent sent by every Kollektiv HTTP client.
USER_AGENT = "Kollektiv/0.1 (+https://github.com/HackerxBots/Kollektiv)"


def resolve_ca_bundle(explicit: str = "") -> str:
    """Return the path of a CA bundle to trust.

    Args:
        explicit: Value from ``SSL_CA_BUNDLE`` in the settings/.env file.

    Returns:
        A filesystem path, or ``""`` to keep the library default (certifi).
    """
    candidates = [os.getenv(name) for name in CA_BUNDLE_ENV_VARS]
    candidates.append(explicit)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            LOGGER.debug("Using CA bundle %s", candidate)
            return candidate
    return ""


def resolve_verify(settings: Optional[Settings] = None) -> Union[bool, str]:
    """Return the ``verify`` argument for httpx.

    Args:
        settings: Optional settings override.

    Returns:
        ``False`` when verification is disabled by configuration, the path of a
        CA bundle when one is configured/discoverable, otherwise ``True``
        (httpx then uses certifi).
    """
    resolved = settings or get_settings()
    if not getattr(resolved, "HTTP_SSL_VERIFY", True):
        LOGGER.warning("TLS certificate verification is disabled (HTTP_SSL_VERIFY=false)")
        return False
    return resolve_ca_bundle(getattr(resolved, "SSL_CA_BUNDLE", "") or "") or True


def default_headers(**extra: str) -> Dict[str, str]:
    """Build the standard header set, merged with ``extra``."""
    headers = {"User-Agent": USER_AGENT}
    headers.update({key: value for key, value in extra.items() if value})
    return headers


def async_client_kwargs(settings: Optional[Settings] = None, **extra: Any) -> Dict[str, Any]:
    """Build keyword arguments for :class:`httpx.AsyncClient`.

    Args:
        settings: Optional settings override.
        **extra: Extra keyword arguments merged into the result (they win).

    Returns:
        A dict ready to splat into ``httpx.AsyncClient(**kwargs)``.
    """
    resolved = settings or get_settings()
    kwargs: Dict[str, Any] = {
        "verify": resolve_verify(resolved),
        "headers": default_headers(),
        "follow_redirects": True,
    }
    kwargs.update(extra)
    if "headers" in extra and extra["headers"]:
        merged = default_headers()
        merged.update(extra["headers"])
        kwargs["headers"] = merged
    return kwargs


__all__ = [
    "resolve_ca_bundle",
    "resolve_verify",
    "default_headers",
    "async_client_kwargs",
    "CA_BUNDLE_ENV_VARS",
    "USER_AGENT",
]
