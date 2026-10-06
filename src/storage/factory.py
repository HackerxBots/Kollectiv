"""Storage backend selection.

Kollektiv can keep its shared drive in three places, and the rest of the
orchestrator never needs to know which one is in use:

============================ ==========================================
Backend                       When it is picked
============================ ==========================================
``r2``      (Cloudflare R2)   ``STORAGE_BACKEND=r2``, or ``auto`` with
                              ``R2_ACCESS_KEY_ID``/``R2_ENDPOINT`` set.
``terabox`` (pooled accounts) ``STORAGE_BACKEND=terabox``, or ``auto``
                              with usable ``TERABOX_ACCOUNTS``.
``none``                      nothing configured: state and artifacts stay
                              in the local workspace (degraded mode).
============================ ==========================================

Usage::

    pool = build_storage(settings)
    await pool.initialize()
"""

from __future__ import annotations

from typing import Any, Optional

from config.settings import Settings, get_settings
from src.storage.pool_manager import TeraBoxPoolManager
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)


def build_storage(settings: Optional[Settings] = None, **overrides: Any) -> Any:
    """Return the storage backend selected by ``STORAGE_BACKEND``.

    Args:
        settings: Settings override (tests / embedders).
        **overrides: Extra keyword arguments forwarded to the backend
            constructor (``accounts``, ``client_factory``, ...).

    Returns:
        An object exposing the shared storage surface: ``initialize``,
        ``close``, ``is_configured``, ``upload_file``, ``download_file``,
        ``read_text``, ``write_text``, ``list_project_files``,
        ``get_total_quota``, ``get_file_url``, ``delete_file``, ...
    """
    resolved = settings or get_settings()
    backend = resolved.storage_backend

    if backend == "r2":
        from src.storage.r2_pool import R2Storage

        LOGGER.info("Shared storage: Cloudflare R2 (pooled buckets)")
        return R2Storage(resolved, **overrides)

    if backend == "terabox":
        LOGGER.info("Shared storage: pooled TeraBox accounts")
        return TeraBoxPoolManager(settings=resolved, **overrides)

    LOGGER.warning(
        "Shared storage: none configured; PROJECT_STATE.md and artifacts stay in %s",
        resolved.workspace_path,
    )
    # An empty TeraBox pool is the canonical "degraded" backend: it reports
    # is_configured() == False and raises a clear ConfigurationError on writes,
    # which lets the state manager fall back to the local workspace.
    overrides.setdefault("accounts", [])
    return TeraBoxPoolManager(settings=resolved, **overrides)
