"""Parse the configured R2 buckets (``R2_ACCOUNTS`` or the single-bucket vars).

Lives in ``config`` rather than ``src.storage`` so that :mod:`config.settings`
can use it without importing the storage package, which imports settings back.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

_LOGGER = logging.getLogger("src.storage.r2_client")


def r2_accounts_from_settings(settings: Any) -> List[Dict[str, str]]:
    """Return the configured R2 buckets, falling back to the single-bucket vars.

    ``R2_ACCOUNTS`` accepts a JSON list so several buckets can be pooled
    (9Drive-style), each contributing its own free tier:

    .. code-block:: json

        [{"name": "primary", "bucket": "kollektiv",
          "access_key_id": "…", "secret_access_key": "…",
          "endpoint": "https://<account>.r2.cloudflarestorage.com"}]
    """
    raw = (settings.R2_ACCOUNTS or "").strip()
    accounts: List[Dict[str, str]] = []
    if raw and raw not in ("[]", "{}"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            _LOGGER.error("R2_ACCOUNTS is not valid JSON: %s", exc)
            parsed = []
        if isinstance(parsed, dict):
            parsed = [parsed]
        if isinstance(parsed, list):
            for index, item in enumerate(parsed, start=1):
                if not isinstance(item, dict):
                    continue
                bucket = str(item.get("bucket") or settings.R2_BUCKET)
                endpoint = str(item.get("endpoint") or settings.R2_ENDPOINT)
                access = str(item.get("access_key_id") or item.get("access_key") or "")
                secret = str(item.get("secret_access_key") or item.get("secret_key") or "")
                if not (bucket and endpoint and access and secret):
                    _LOGGER.warning("Skipping incomplete R2_ACCOUNTS entry #%s", index)
                    continue
                accounts.append(
                    {
                        "name": str(item.get("name") or f"r2-{index}"),
                        "bucket": bucket,
                        "endpoint": endpoint,
                        "access_key_id": access,
                        "secret_access_key": secret,
                        "prefix": str(item.get("prefix") or settings.R2_PREFIX or ""),
                        "region": str(item.get("region") or settings.R2_REGION or "auto"),
                    }
                )
        elif parsed is not None:
            _LOGGER.error("R2_ACCOUNTS must be a JSON list or object, got %s", type(parsed).__name__)
    if not accounts and settings.is_r2_configured:
        accounts.append(
            {
                "name": "r2",
                "bucket": settings.R2_BUCKET,
                "endpoint": settings.R2_ENDPOINT,
                "access_key_id": settings.R2_ACCESS_KEY_ID,
                "secret_access_key": settings.R2_SECRET_ACCESS_KEY,
                "prefix": settings.R2_PREFIX or "",
                "region": settings.R2_REGION or "auto",
            }
        )
    return accounts
