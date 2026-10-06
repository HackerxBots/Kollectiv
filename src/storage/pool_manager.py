"""Pooled TeraBox storage across many free accounts.

A single free TeraBox account is small and rate limited; Kollektiv therefore
treats a list of accounts as one logical drive:

* uploads go to the account with the most free space (with a per-file
  directory hint so a project stays readable);
* downloads search every account until the file is found;
* listings are aggregated (duplicates reported once, with all locations);
* the pool keeps an in-memory index so ``download_file``/``get_file_url``
  are typically a single API call.

Usage::

    pool = TeraBoxPoolManager(settings.terabox_account_list())
    await pool.initialize()
    await pool.upload_file("./artifact.zip", "/artifact.zip")
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config.settings import Settings, get_settings
from src.storage.terabox_client import TeraBoxClient, TeraBoxQuota
from src.utils.errors import ConfigurationError, TeraBoxError
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)


@dataclass
class PoolAccountState:
    """Runtime state tracked for one pooled account."""

    client: TeraBoxClient
    quota: TeraBoxQuota = field(default_factory=TeraBoxQuota)
    healthy: bool = False
    error: str = ""
    uploads: int = 0
    downloads: int = 0

    @property
    def account_id(self) -> str:
        """Stable id of the underlying account."""
        return self.client.account_id

    @property
    def free_gb(self) -> float:
        """Free space in gigabytes (0 when unknown)."""
        return round(self.quota.free / (1024**3), 3)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable status snapshot."""
        return {
            "account_id": self.account_id,
            "email": self.client.label,
            "healthy": self.healthy,
            "free_gb": self.free_gb,
            "used_gb": round(self.quota.used / (1024**3), 3),
            "total_gb": round(self.quota.total / (1024**3), 3),
            "uploads": self.uploads,
            "downloads": self.downloads,
            "error": self.error,
        }


class TeraBoxPoolManager:
    """Route storage operations across a pool of TeraBox accounts.

    Args:
        accounts: List of account dicts (or settings account models).
        settings: Optional settings override.
        min_free_bytes: An account must have at least this much free space to
            receive a new upload (defaults to 64 MiB).
        client_factory: Injection point used by tests to supply fake clients.
    """

    def __init__(
        self,
        accounts: Optional[List[Any]] = None,
        settings: Optional[Settings] = None,
        min_free_bytes: int = 64 * 1024 * 1024,
        client_factory: Optional[Any] = None,
    ) -> None:
        self.settings = settings or get_settings()
        raw_accounts = accounts if accounts is not None else self.settings.terabox_account_list()
        self._client_factory = client_factory
        self.min_free_bytes = min_free_bytes
        self._states: Dict[str, PoolAccountState] = {}
        self._index: Dict[str, str] = {}  # remote path -> account_id
        self._initialized = False
        self._lock = asyncio.Lock()

        for account in raw_accounts:
            client = self._make_client(account)
            self._states[client.account_id] = PoolAccountState(client=client)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _make_client(self, account: Any) -> TeraBoxClient:
        """Instantiate a client for ``account`` (honouring the factory hook)."""
        if self._client_factory is not None:
            return self._client_factory(account, self.settings)
        return TeraBoxClient.from_config(account, settings=self.settings)

    @property
    def accounts(self) -> List[PoolAccountState]:
        """All pooled account states."""
        return list(self._states.values())

    @property
    def account_count(self) -> int:
        """Number of accounts in the pool."""
        return len(self._states)

    def is_configured(self) -> bool:
        """Return ``True`` when at least one account is present."""
        return bool(self._states)

    def get_state(self, account_id: str) -> Optional[PoolAccountState]:
        """Return the pooled state for ``account_id`` when present."""
        return self._states.get(account_id)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def initialize(self) -> Dict[str, Any]:
        """Authenticate every account and refresh its quota.

        Failures are recorded per account instead of raising, so a partially
        working pool still serves requests.

        Returns:
            ``{initialized, accounts, healthy, failed}`` summary.
        """
        if not self._states:
            LOGGER.warning("TeraBox pool is empty: no storage accounts configured")
            self._initialized = True
            return {"initialized": True, "accounts": 0, "healthy": 0, "failed": 0}

        async def prepare(state: PoolAccountState) -> bool:
            """Authenticate one account and load its quota."""
            try:
                await state.client.authenticate()
                state.quota = await state.client.get_quota(refresh=True)
                state.healthy = True
                state.error = ""
                LOGGER.info(
                    "TeraBox account %s ready (free %.2f GB of %.2f GB)",
                    state.client.label,
                    state.free_gb,
                    round(state.quota.total / (1024**3), 3),
                )
                return True
            except Exception as exc:  # noqa: BLE001 - reported in the summary
                state.healthy = False
                state.error = str(exc)
                LOGGER.error("TeraBox account %s failed to initialise: %s", state.client.label, exc)
                return False

        results = await asyncio.gather(*(prepare(state) for state in self._states.values()))
        self._initialized = True
        healthy = sum(1 for ok in results if ok)
        return {
            "initialized": True,
            "accounts": len(results),
            "healthy": healthy,
            "failed": len(results) - healthy,
        }

    async def close(self) -> None:
        """Close every pooled HTTP client."""
        await asyncio.gather(*(state.client.close() for state in self._states.values()), return_exceptions=True)

    async def ensure_initialized(self) -> None:
        """Initialise the pool on first use."""
        if self._initialized:
            return
        async with self._lock:
            if not self._initialized:
                await self.initialize()

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    async def get_best_account(self, refresh: bool = False) -> TeraBoxClient:
        """Return the healthy account with the most free space.

        Args:
            refresh: Re-query quotas before ranking.

        Returns:
            A ready-to-use :class:`TeraBoxClient`.

        Raises:
            ConfigurationError: When the pool is empty.
            TeraBoxError: When no account is currently healthy.
        """
        await self.ensure_initialized()
        if not self._states:
            raise ConfigurationError(
                "No TeraBox accounts configured. Set TERABOX_ACCOUNTS in .env to a JSON "
                "list of accounts with access_token/refresh_token values."
            )
        if refresh:
            await self._refresh_quotas()
        candidates = [state for state in self._states.values() if state.healthy]
        if not candidates:
            raise TeraBoxError("No healthy TeraBox account available", accounts=len(self._states))
        best = max(candidates, key=lambda state: (state.quota.free if state.quota.total else 0, -state.uploads))
        LOGGER.debug("Routing storage operation to %s (%.2f GB free)", best.client.label, best.free_gb)
        return best.client

    async def _refresh_quotas(self) -> None:
        """Refresh quota information for every healthy account."""

        async def refresh(state: PoolAccountState) -> None:
            try:
                state.quota = await state.client.get_quota(refresh=True)
                state.healthy = True
            except Exception as exc:  # noqa: BLE001 - keep the pool usable
                state.error = str(exc)
                LOGGER.warning("Quota refresh failed for %s: %s", state.client.label, exc)

        await asyncio.gather(*(refresh(state) for state in self._states.values()))

    def _record_path(self, remote_path: str, account_id: str) -> None:
        """Remember which account holds ``remote_path``."""
        self._index[remote_path] = account_id

    async def locate(self, remote_path: str) -> Optional[PoolAccountState]:
        """Find the account holding ``remote_path``.

        The in-memory index answers instantly; on a miss every healthy
        account is probed in parallel.

        Args:
            remote_path: Remote path to locate.

        Returns:
            The owning account state, or ``None`` when the file is absent.
        """
        await self.ensure_initialized()
        cached = self._index.get(remote_path)
        if cached and cached in self._states:
            return self._states[cached]

        async def probe(state: PoolAccountState) -> Optional[PoolAccountState]:
            if not state.healthy:
                return None
            try:
                if await state.client.file_exists(remote_path):
                    return state
            except Exception as exc:  # noqa: BLE001 - probing must not raise
                LOGGER.debug("Probe of %s on %s failed: %s", remote_path, state.client.label, exc)
            return None

        results = await asyncio.gather(*(probe(state) for state in self._states.values()))
        found = next((state for state in results if state is not None), None)
        if found is not None:
            self._record_path(remote_path, found.account_id)
        return found

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------
    async def upload_file(
        self,
        local_path: str,
        remote_path: str,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload a file to the account with the most free space.

        Args:
            local_path: Local source path.
            remote_path: Destination path.
            account_id: Force a specific account instead of auto-routing.

        Returns:
            ``{account_id, email, path, url, size, md5}``.

        Raises:
            FileNotFoundError: When ``local_path`` is missing.
            TeraBoxError: When the upload fails everywhere.
        """
        if not os.path.isfile(local_path):
            raise FileNotFoundError(f"Local file not found: {local_path}")
        await self.ensure_initialized()
        size = os.path.getsize(local_path)

        if account_id:
            state = self._states.get(account_id)
            if state is None:
                raise ConfigurationError(f"Unknown TeraBox account id: {account_id}")
            order = [state]
        else:
            order = await self._ordered_candidates(size)

        errors: List[str] = []
        for state in order:
            try:
                result = await state.client.upload_file(local_path, remote_path)
                state.uploads += 1
                self._record_path(remote_path, state.account_id)
                # Keep quota fresh enough for subsequent routing decisions.
                if state.quota.free >= size:
                    state.quota.free -= size
                    state.quota.used += size
                payload = dict(result)
                payload.update({"account_id": state.account_id, "email": state.client.label})
                LOGGER.info(
                    "Pooled upload of %s (%s bytes) to account %s", remote_path, size, state.client.label
                )
                return payload
            except Exception as exc:  # noqa: BLE001 - try the next account
                state.error = str(exc)
                errors.append(f"{state.client.label}: {exc}")
                LOGGER.warning("Upload of %s failed on %s: %s", remote_path, state.client.label, exc)

        if not self._states:
            raise ConfigurationError(
                "No TeraBox accounts are configured; set TERABOX_ACCOUNTS in .env "
                "to enable shared storage (the local workspace is used meanwhile)."
            )
        raise TeraBoxError(f"Upload of {remote_path} failed on every account", errors=errors)

    async def _ordered_candidates(self, size: int) -> List[PoolAccountState]:
        """Return healthy accounts ordered by most free space.

        Accounts whose remaining quota is unknown sort last but are still
        tried (TeraBox sometimes omits quota from ``uinfo``).
        """
        await self.ensure_initialized()
        healthy = []
        for state in self._states.values():
            if not state.healthy:
                continue
            has_room = state.quota.free <= 0 or state.quota.free >= max(size, self.min_free_bytes)
            if not has_room:
                LOGGER.warning(
                    "Skipping %s for a %s byte upload: only %s bytes free",
                    state.client.label,
                    size,
                    state.quota.free,
                )
                continue
            healthy.append(state)
        if not healthy:
            # Fall back to every account so a stale quota reading cannot
            # permanently block uploads.
            healthy = [state for state in self._states.values() if state.healthy]
        return sorted(healthy, key=lambda state: (state.quota.free, -state.uploads), reverse=True)

    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download ``remote_path`` from whichever account holds it.

        Args:
            remote_path: Source path (as stored in the pool index).
            local_path: Destination path on the local filesystem.

        Returns:
            ``True`` on success, ``False`` when the file is nowhere to be found.
        """
        state = await self.locate(remote_path)
        if state is None:
            LOGGER.error("File %s was not found on any pooled TeraBox account", remote_path)
            return False
        success = await state.client.download_file(remote_path, local_path)
        if success:
            state.downloads += 1
        return success

    async def download_latest(self, path_prefix: str, local_path: str) -> bool:
        """Download the most recently modified file under ``path_prefix``.

        Useful for locating a document whose exact name is unknown (for
        example ``PROJECT_STATE.md`` when TeraBox appends a suffix).

        Args:
            path_prefix: Remote file name or prefix.
            local_path: Destination path.

        Returns:
            ``True`` when a file was downloaded.
        """
        candidates = await self.list_all_files(os.path.dirname(path_prefix) or "/")
        base = os.path.basename(path_prefix)
        matches = [entry for entry in candidates if entry["name"] == base or entry["name"].startswith(base)]
        if not matches:
            return False
        matches.sort(key=lambda entry: (entry.get("server_mtime") or 0, entry.get("size") or 0), reverse=True)
        return await self.download_file(matches[0]["path"], local_path)

    async def delete_file(self, remote_path: str, account_id: Optional[str] = None) -> bool:
        """Delete a file from its owning account (or from every account)."""
        if account_id:
            state = self._states.get(account_id)
            if state is None:
                return False
            deleted = await state.client.delete_file(remote_path)
        else:
            state = await self.locate(remote_path)
            if state is None:
                return False
            deleted = await state.client.delete_file(remote_path)
        if deleted:
            self._index.pop(remote_path, None)
        return deleted

    async def read_text(self, remote_path: str) -> Optional[str]:
        """Read a small text file from the pool."""
        state = await self.locate(remote_path)
        if state is None:
            return None
        return await state.client.read_text(remote_path)

    async def write_text(self, remote_path: str, content: str) -> Dict[str, Any]:
        """Write a string to the pool as a file, returning the upload result."""
        workspace = self.settings.workspace_path / "tmp"
        workspace.mkdir(parents=True, exist_ok=True)
        temp_path = workspace / f"pool-{abs(hash(remote_path)):x}.tmp"
        try:
            temp_path.write_text(content, encoding="utf-8")
            return await self.upload_file(str(temp_path), remote_path)
        finally:
            if temp_path.exists():
                with contextlib.suppress(OSError):  # pragma: no cover - best effort cleanup
                    temp_path.unlink()

    async def list_all_files(self, path: str = "/") -> List[Dict[str, Any]]:
        """Aggregate a directory listing across every pooled account.

        Args:
            path: Remote directory path.

        Returns:
            A de-duplicated list of file dicts. Each entry gains
            ``account_id``, ``email`` and ``locations`` keys.
        """
        await self.ensure_initialized()

        async def list_one(state: PoolAccountState) -> List[Dict[str, Any]]:
            if not state.healthy:
                return []
            try:
                entries = await state.client.list_files(path)
            except Exception as exc:  # noqa: BLE001 - partial results are fine
                LOGGER.warning("Listing %s on %s failed: %s", path, state.client.label, exc)
                return []
            for entry in entries:
                entry["account_id"] = state.account_id
                entry["email"] = state.client.label
            return entries

        batches = await asyncio.gather(*(list_one(state) for state in self._states.values()))
        merged: Dict[str, Dict[str, Any]] = {}
        for entries in batches:
            for entry in entries:
                key = entry["path"]
                if key in merged:
                    merged[key]["locations"].append(entry["account_id"])
                    continue
                entry.setdefault("locations", [entry["account_id"]])
                merged[key] = entry
                if entry["type"] == "file":
                    self._record_path(key, entry["account_id"])
        result = list(merged.values())
        LOGGER.debug("Aggregated %s entries under %s across %s accounts", len(result), path, len(self._states))
        return result

    async def list_project_files(self, project_id: str) -> List[Dict[str, Any]]:
        """List every file stored for a project.

        Args:
            project_id: The project identifier.

        Returns:
            File dicts found under the project's remote folder.
        """
        root = f"{self.settings.TERABOX_REMOTE_ROOT.rstrip('/')}/{project_id}"
        files: List[Dict[str, Any]] = []
        for account in self.accounts:
            if not account.healthy:
                continue
            try:
                files.extend(await account.client.list_recursive(root, max_depth=4))
            except Exception as exc:  # noqa: BLE001 - partial results are fine
                LOGGER.warning("Project listing failed on %s: %s", account.client.label, exc)
        return files

    async def get_file_url(self, remote_path: str) -> str:
        """Return a direct download URL for ``remote_path`` (``""`` if absent)."""
        state = await self.locate(remote_path)
        if state is None:
            return ""
        try:
            return await state.client.get_file_url(remote_path)
        except TeraBoxError as exc:
            LOGGER.error("Could not build a URL for %s: %s", remote_path, exc)
            return ""

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    async def get_total_quota(self) -> Dict[str, Any]:
        """Return aggregate quota across the pool.

        Returns:
            ``{used_gb, free_gb, total_gb, accounts, healthy, per_account}``.
        """
        await self.ensure_initialized()
        await self._refresh_quotas()
        used = sum(state.quota.used for state in self._states.values())
        free = sum(state.quota.free for state in self._states.values())
        total = sum(state.quota.total for state in self._states.values())
        gb = 1024**3
        return {
            "used_gb": round(used / gb, 3),
            "free_gb": round(free / gb, 3),
            "total_gb": round(total / gb, 3),
            "accounts": len(self._states),
            "healthy": sum(1 for state in self._states.values() if state.healthy),
            "per_account": [state.to_dict() for state in self._states.values()],
        }

    def get_pool_status(self) -> List[Dict[str, Any]]:
        """Return a synchronous snapshot of every account in the pool."""
        return [state.to_dict() for state in self._states.values()]

    def has_capacity(self, size_bytes: int) -> bool:
        """Return ``True`` when some healthy account can hold ``size_bytes``."""
        for state in self._states.values():
            if not state.healthy:
                continue
            if state.quota.total == 0 or state.quota.free >= size_bytes:
                return True
        return False


__all__ = ["TeraBoxPoolManager", "PoolAccountState"]
