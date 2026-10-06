"""Pooled object storage on Cloudflare R2 — a 9Drive-style single drive.

Several buckets (each with its own free tier) are presented as one drive:

* writes are routed to the bucket with the most free space,
* the pool remembers which bucket holds which path, so reads, deletes and
  presigned URLs go straight to the owner,
* quotas are summed into a single ``used_gb``/``free_gb``/``total_gb`` view.

The class is a drop-in replacement for
:class:`src.storage.pool_manager.TeraBoxPoolManager`: the orchestrator, the
state manager and the sync engine only rely on the shared method surface
(``upload_file``, ``download_file``, ``read_text``, ``write_text``,
``list_project_files``, ``get_total_quota``, ...), so switching backends is a
configuration change and nothing else.

Usage::

    pool = R2Storage(settings)
    await pool.initialize()
    await pool.upload_file("./out.md", "/prj_1/out.md")
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config.settings import Settings, get_settings
from src.storage.r2_client import R2Client, format_bytes, r2_accounts_from_settings
from src.utils.errors import ConfigurationError, NotFoundError, R2Error
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)


@dataclass
class R2BucketState:
    """Runtime state of one pooled bucket."""

    account_id: str
    label: str
    client: R2Client
    used_bytes: int = 0
    free_bytes: int = 10 * 1024**3
    healthy: bool = False
    error: str = ""
    files: Dict[str, str] = field(default_factory=dict)

    @property
    def free_gb(self) -> float:
        """Free space in gigabytes."""
        return round(self.free_bytes / 1024**3, 3)

    @property
    def used_gb(self) -> float:
        """Used space in gigabytes."""
        return round(self.used_bytes / 1024**3, 3)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for status endpoints."""
        return {
            "account_id": self.account_id,
            "label": self.label,
            "bucket": self.client.bucket,
            "endpoint": self.client.endpoint,
            "healthy": self.healthy,
            "used_gb": self.used_gb,
            "free_gb": self.free_gb,
            "used_human": format_bytes(self.used_bytes),
            "free_human": format_bytes(self.free_bytes),
            "files": len(self.files),
            "error": self.error,
        }


class R2Storage:
    """A pool of R2 buckets exposed as one S3-compatible drive.

    Args:
        settings: Settings override (tests / embedders).
        accounts: Explicit bucket definitions; defaults to ``R2_ACCOUNTS`` (or
            the single-bucket ``R2_*`` variables).
        client_factory: Injectable ``R2Client`` factory (tests).
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        accounts: Optional[List[Dict[str, str]]] = None,
        client_factory: Optional[Any] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._accounts = accounts if accounts is not None else r2_accounts_from_settings(self.settings)
        self._factory = client_factory or self._default_factory
        self._states: Dict[str, R2BucketState] = {}
        self._initialized = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------
    def _default_factory(self, account: Dict[str, str]) -> R2Client:
        """Build an :class:`R2Client` from an account definition."""
        return R2Client(
            bucket=account["bucket"],
            access_key_id=account["access_key_id"],
            secret_access_key=account["secret_access_key"],
            endpoint=account["endpoint"],
            region=account.get("region") or self.settings.R2_REGION or "auto",
            prefix=account.get("prefix") or self.settings.R2_PREFIX or "",
            label=account.get("name") or account["bucket"],
            settings=self.settings,
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def accounts(self) -> List[R2BucketState]:
        """The pooled bucket states."""
        return list(self._states.values())

    @property
    def account_count(self) -> int:
        """Number of pooled buckets."""
        return len(self._states)

    @property
    def remote_root(self) -> str:
        """Prefix for remote paths (R2 keys are namespaced by ``R2_PREFIX``)."""
        return ""

    def is_configured(self) -> bool:
        """Return ``True`` when at least one bucket is configured."""
        return bool(self._accounts)

    def get_state(self, account_id: str) -> Optional[R2BucketState]:
        """Return the state for ``account_id`` if it is pooled."""
        return self._states.get(account_id)

    def get_pool_status(self) -> List[Dict[str, Any]]:
        """Return per-bucket status dicts (no I/O)."""
        return [state.to_dict() for state in self._states.values()]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def initialize(self, probe: bool = True) -> Dict[str, Any]:
        """Create the clients, verify the buckets and read the used space."""
        async with self._lock:
            if self._initialized:
                return self._summary()
            for account in self._accounts:
                try:
                    client = self._factory(account)
                except Exception as exc:  # noqa: BLE001 - one bad entry must not stop the pool
                    LOGGER.error("Could not build an R2 client for %s: %s", account.get("name"), exc)
                    continue
                self._states[client.account_id] = R2BucketState(
                    account_id=client.account_id,
                    label=client.label,
                    client=client,
                    free_bytes=int(self.settings.R2_FREE_TIER_GB * 1024**3),
                )
            if not self._states:
                LOGGER.warning("R2 pool is empty: no usable buckets configured")
                self._initialized = True
                return self._summary()
            await self._refresh(probe=probe)
            self._initialized = True
        LOGGER.info(
            "R2 pool ready: %s bucket(s), %s free",
            len(self._states),
            format_bytes(sum(state.free_bytes for state in self._states.values())),
        )
        return self._summary()

    async def ensure_initialized(self) -> None:
        """Initialise the pool on first use."""
        if not self._initialized:
            await self.initialize()

    async def close(self) -> None:
        """Close every bucket client."""
        for state in self._states.values():
            with contextlib.suppress(Exception):
                await state.client.close()
        self._states.clear()
        self._initialized = False

    async def _refresh(self, probe: bool = True) -> None:
        """Verify every bucket and recompute its used space."""

        async def refresh_one(state: R2BucketState) -> None:
            try:
                if probe:
                    await state.client.head_bucket()
                state.healthy = True
                state.error = ""
                listing = await state.client.list_objects(prefix="")
                state.used_bytes = sum(int(item.get("size") or 0) for item in listing)
                for item in listing:
                    if item.get("type") == "file" and item.get("key"):
                        state.files[state.client.path(str(item["key"]))] = str(item["key"])
                state.free_bytes = max(
                    0, int(self.settings.R2_FREE_TIER_GB * 1024**3) - state.used_bytes
                )
            except Exception as exc:  # noqa: BLE001 - a broken bucket is reported, not fatal
                state.healthy = False
                state.error = str(exc)
                LOGGER.warning("R2 bucket %s is unhealthy: %s", state.label, exc)

        await asyncio.gather(*(refresh_one(state) for state in self._states.values()))

    def _summary(self) -> Dict[str, Any]:
        """Return the initialisation summary."""
        return {
            "backend": "r2",
            "configured": self.is_configured(),
            "accounts": len(self._states),
            "healthy": len([state for state in self._states.values() if state.healthy]),
            **{key: value for key, value in self.get_total_quota().items() if key != "per_account"},
        }

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    async def get_best_account(self, refresh: bool = False) -> R2Client:
        """Return the client of the bucket with the most free space."""
        await self.ensure_initialized()
        if not self._states:
            raise ConfigurationError(
                "No R2 buckets are configured; set R2_BUCKET/R2_ACCESS_KEY_ID/"
                "R2_SECRET_ACCESS_KEY/R2_ENDPOINT (or R2_ACCOUNTS) to enable shared storage."
            )
        if refresh:
            await self._refresh(probe=False)
        healthy = [state for state in self._states.values() if state.healthy] or list(self._states.values())
        best = max(healthy, key=lambda state: state.free_bytes)
        return best.client

    def _ordered_candidates(self, size: int = 0) -> List[R2BucketState]:
        """Return buckets ordered by free space (healthiest first)."""
        states = list(self._states.values())
        if not states:
            return []
        healthy = [state for state in states if state.healthy]
        pool = healthy or states
        ordered = sorted(pool, key=lambda state: state.free_bytes, reverse=True)
        if size:
            fits = [state for state in ordered if state.free_bytes >= size]
            if fits:
                return fits + [state for state in ordered if state not in fits]
        return ordered

    def _record_path(self, remote_path: str, state: R2BucketState) -> None:
        """Remember which bucket owns ``remote_path``."""
        state.files[remote_path] = state.client.key(remote_path)

    async def locate(self, remote_path: str) -> Optional[R2BucketState]:
        """Return the bucket holding ``remote_path`` (probes when unknown)."""
        await self.ensure_initialized()
        for state in self._states.values():
            if remote_path in state.files:
                return state
        for state in self._states.values():
            try:
                await state.client.head(state.client.key(remote_path))
                self._record_path(remote_path, state)
                return state
            except NotFoundError:
                continue
            except Exception as exc:  # noqa: BLE001 - keep probing other buckets
                LOGGER.debug("Probe of %s in %s failed: %s", remote_path, state.label, exc)
        return None

    # ------------------------------------------------------------------
    # Object operations (pool surface)
    # ------------------------------------------------------------------
    async def upload_file(
        self, local_path: str, remote_path: str, account_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Upload a local file to the bucket with the most free space.

        Returns:
            ``{account_id, email, path, url, size, md5, key}``.

        Raises:
            FileNotFoundError: When ``local_path`` does not exist.
            R2Error: When the upload fails on every bucket.
        """
        if not os.path.isfile(local_path):
            raise FileNotFoundError(f"Local file not found: {local_path}")
        await self.ensure_initialized()
        if not self._states:
            raise ConfigurationError(
                "No R2 buckets are configured; set R2_ACCOUNTS (or the R2_* variables) "
                "to enable shared storage."
            )
        size = os.path.getsize(local_path)
        if account_id:
            forced = self._states.get(account_id)
            if forced is None:
                raise ConfigurationError(f"Unknown R2 bucket id: {account_id}")
            candidates = [forced]
        else:
            candidates = self._ordered_candidates(size)

        errors: List[str] = []
        for state in candidates:
            try:
                result = await state.client.put_file(local_path, state.client.key(remote_path))
                state.used_bytes += size
                state.free_bytes = max(0, state.free_bytes - size)
                self._record_path(remote_path, state)
                LOGGER.info(
                    "Uploaded %s (%s) to R2 bucket %s", remote_path, format_bytes(size), state.label
                )
                return {
                    "account_id": state.account_id,
                    "email": state.label,
                    "path": remote_path,
                    "key": result["key"],
                    "url": result["url"],
                    "size": size,
                    "md5": result["md5"],
                    "etag": result.get("etag", ""),
                }
            except Exception as exc:  # noqa: BLE001 - try the next bucket
                state.error = str(exc)
                errors.append(f"{state.label}: {exc}")
                LOGGER.warning("Upload of %s failed on %s: %s", remote_path, state.label, exc)
        raise R2Error(f"Upload of {remote_path} failed on every bucket", errors=errors)

    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download an object to disk, routing to the bucket that owns it."""
        state = await self.locate(remote_path)
        if state is None:
            LOGGER.warning("Cannot download %s: not present in any bucket", remote_path)
            return False
        return await state.client.get_file(state.client.key(remote_path), local_path)

    async def read_text(self, remote_path: str) -> Optional[str]:
        """Read a text object, or ``None`` when it does not exist."""
        try:
            state = await self.locate(remote_path)
            if state is None:
                return None
            return await state.client.get_text(state.client.key(remote_path))
        except NotFoundError:
            return None
        except (R2Error, ConfigurationError) as exc:
            LOGGER.warning("Could not read %s: %s", remote_path, exc)
            return None

    async def write_text(self, remote_path: str, content: str) -> Dict[str, Any]:
        """Write a text object to the healthiest bucket."""
        await self.ensure_initialized()
        client = await self.get_best_account()
        state = next(
            (item for item in self._states.values() if item.client is client), None
        )
        result = await client.put_text(client.key(remote_path), content)
        if state is not None:
            state.used_bytes += result["size"]
            state.free_bytes = max(0, state.free_bytes - result["size"])
            self._record_path(remote_path, state)
        return result

    async def delete_file(self, remote_path: str, account_id: Optional[str] = None) -> bool:
        """Delete an object from the bucket that owns it."""
        if account_id:
            state = self._states.get(account_id)
        else:
            state = await self.locate(remote_path)
        if state is None:
            return False
        deleted = await state.client.delete(state.client.key(remote_path))
        if deleted:
            size = 0
            state.files.pop(remote_path, None)
            state.used_bytes = max(0, state.used_bytes - size)
        return deleted

    async def get_file_url(self, remote_path: str, expires: Optional[int] = None) -> str:
        """Return a (presigned) URL for an object."""
        state = await self.locate(remote_path)
        if state is None:
            raise NotFoundError(f"Object not found in any R2 bucket: {remote_path}")
        key = state.client.key(remote_path)
        public = state.client.public_url(key)
        if public:
            return public
        return state.client.presigned_url(key, expires=expires)

    async def list_all_files(self, path: str = "") -> List[Dict[str, Any]]:
        """List every object under ``path`` across all buckets."""
        await self.ensure_initialized()
        files: List[Dict[str, Any]] = []
        for state in self._states.values():
            try:
                listing = await state.client.list_objects(prefix=path)
            except Exception as exc:  # noqa: BLE001 - a broken bucket must not hide the rest
                LOGGER.warning("Listing %s in %s failed: %s", path, state.label, exc)
                continue
            for item in listing:
                if item.get("type") != "file":
                    continue
                remote = state.client.path(str(item["key"]))
                self._record_path(remote, state)
                files.append(
                    {
                        "name": item.get("name", ""),
                        "path": remote,
                        "size": int(item.get("size") or 0),
                        "type": "file",
                        "account_id": state.account_id,
                        "modified": item.get("last_modified", ""),
                        "url": state.client.public_url(state.client.key(remote)),
                    }
                )
        files.sort(key=lambda item: item["path"])
        return files

    async def list_project_files(self, project_id: str) -> List[Dict[str, Any]]:
        """List the files stored for one project."""
        root = f"{self.remote_root.rstrip('/')}/{project_id}"
        return await self.list_all_files(root)

    async def download_latest(self, path_prefix: str, local_path: str) -> bool:
        """Download the most recently modified object under ``path_prefix``."""
        files = await self.list_all_files(path_prefix)
        if not files:
            return False
        newest = max(files, key=lambda item: item.get("modified") or "")
        return await self.download_file(str(newest["path"]), local_path)

    # ------------------------------------------------------------------
    # Quota
    # ------------------------------------------------------------------
    def get_total_quota(self) -> Dict[str, Any]:
        """Return the pooled quota: ``{used_gb, free_gb, total_gb, ...}``."""
        per_account = self.get_pool_status()
        total_free_tier = int(self.settings.R2_FREE_TIER_GB * 1024**3)
        used = sum(state.used_bytes for state in self._states.values())
        capacity = total_free_tier * max(1, len(self._states)) if self._states else 0
        free = max(0, capacity - used)
        return {
            "used_gb": round(used / 1024**3, 3),
            "free_gb": round(free / 1024**3, 3),
            "total_gb": round(capacity / 1024**3, 3),
            "accounts": len(self._states),
            "healthy": len([state for state in self._states.values() if state.healthy]),
            "backend": "r2",
            "per_account": per_account,
        }

    def has_capacity(self, size_bytes: int) -> bool:
        """Return ``True`` when the pool can still take ``size_bytes``."""
        if not self._states:
            return False
        free = int(self.settings.R2_FREE_TIER_GB * 1024**3) * len(self._states) - sum(
            state.used_bytes for state in self._states.values()
        )
        return free >= size_bytes
