"""Async TeraBox Open Platform client.

Implements the OAuth2 refresh-token flow, file listing, sharded uploads and
streaming downloads against ``https://openapi.terabox.com``.

Endpoints used (all under ``TERABOX_BASE_URL``)::

    GET  /oauth/2.0/token            OAuth2 (refresh_token / client_credentials)
    GET  /rest/2.0/xpan/file?method=list     directory listing
    POST /rest/2.0/xpan/file?method=create   create a folder
    POST /rest/2.0/xpan/file?method=precreate  plan a large upload (sharding)
    POST /rest/2.0/xpan/file?method=upload     (or the superfile upload host)
    POST /rest/2.0/xpan/file?method=merge      merge uploaded shards
    GET  /rest/2.0/xpan/multimedia?method=filemetas  direct download links
    POST /rest/2.0/xpan/file?method=delete   delete files
    GET  /rest/2.0/xpan/nas?method=uinfo     quota information

TeraBox has shipped several incompatible revisions of the Open Platform
under the same base host. The exact request encoding for the *upload* step
differs between them (``file`` field vs. ``local_path`` + ``block_list``
multipart vs. an UPPER_API host). Those spots are marked with ``TODO`` and
isolated in the ``_upload_*`` helpers so adapting to a specific revision is a
one-method change.

Access tokens are valid for ~2 days; the client proactively refreshes when
fewer than 5 minutes remain.

Usage::

    client = TeraBoxClient({"email": "a@b.c", "refresh_token": "..."})
    await client.authenticate()
    await client.upload_file("./out.zip", "/Kollektiv/out.zip")
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from config.settings import Settings, get_settings
from src.utils.crypto import mask_email
from src.utils.errors import (
    AuthenticationError,
    ConfigurationError,
    RateLimitError,
    TeraBoxError,
    TeraBoxTransientError,
)
from src.utils.logger import get_logger
from src.utils.retry import async_retry
from src.utils.token_store import SERVICE_TERABOX, TokenStore

LOGGER = get_logger(__name__)

#: Refresh an access token when it has less than this many seconds left.
TOKEN_REFRESH_SKEW_SECONDS = 300

#: TeraBox error codes that indicate an invalid/expired token.
AUTH_ERROR_CODES = frozenset({-6, -7, 110, 111, 31023, 31024, 31034, 400002})

#: Default shard size (must be a power of two multiple of 4 MiB).
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024


@dataclass
class TeraBoxFile:
    """A single entry returned by ``list_files``.

    Attributes:
        name: Base file name.
        path: Absolute remote path.
        size: Size in bytes.
        type: ``"file"`` or ``"folder"``.
        isdir: Convenience flag derived from ``type``.
        fs_id: TeraBox internal file id when present.
        md5: Content hash when reported by the API.
        server_mtime: Server modification time (epoch seconds) when present.
    """

    name: str
    path: str
    size: int = 0
    type: str = "file"
    isdir: bool = False
    fs_id: Optional[int] = None
    md5: str = ""
    server_mtime: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return the dict shape documented by the public API."""
        return {
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "type": self.type,
            "isdir": self.isdir,
            "fs_id": self.fs_id,
            "md5": self.md5,
        }


@dataclass
class TeraBoxQuota:
    """Quota information for one account."""

    total: int = 0
    used: int = 0
    free: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Return the quota in the public shape (bytes)."""
        return {"total": self.total, "used": self.used, "free": self.free}


class TeraBoxClient:
    """An async client bound to a single TeraBox account.

    Args:
        account: Account dict with ``email``, ``password``, ``access_token``,
            ``refresh_token`` (and optionally ``app_id``/``app_key``).
        settings: Settings override (handy in tests).
        token_store: Encrypted token store override.
        client: Pre-built :class:`httpx.AsyncClient` (used in tests).
    """

    def __init__(
        self,
        account: Dict[str, Any],
        settings: Optional[Settings] = None,
        token_store: Optional[TokenStore] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.account: Dict[str, Any] = dict(account or {})
        self.email: str = str(self.account.get("email") or self.account.get("name") or "")
        self.account_id: str = str(self.account.get("account_id") or self.account.get("id") or self._derive_id())

        self.app_id: str = str(self.account.get("app_id") or self.settings.TERABOX_APP_ID or "")
        self.app_key: str = str(self.account.get("app_key") or self.settings.TERABOX_APP_KEY or "")

        self.access_token: str = str(self.account.get("access_token") or "")
        self.refresh_token_value: str = str(self.account.get("refresh_token") or "")
        self.token_expires_at: float = 0.0
        self._quota_cache: Optional[TeraBoxQuota] = None

        self._store = token_store
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.settings.TERABOX_BASE_URL.rstrip("/"),
            timeout=httpx.Timeout(self.settings.TERABOX_REQUEST_TIMEOUT, read=self.settings.TERABOX_UPLOAD_TIMEOUT),
            headers={"User-Agent": "Kollektiv/0.1 (+https://github.com/HackerxBots/Kollectiv)"},
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------
    def _derive_id(self) -> str:
        """Derive a stable account id when the config omits one."""
        seed = (self.email or "terabox-account").strip().lower()
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_config(cls, account: Any, **kwargs: Any) -> "TeraBoxClient":
        """Build a client from a settings account model or a plain dict."""
        if hasattr(account, "model_dump"):
            data = account.model_dump()
        elif isinstance(account, dict):
            data = dict(account)
        else:  # pragma: no cover - defensive
            raise ConfigurationError(f"Unsupported TeraBox account type: {type(account)!r}")
        data.setdefault("account_id", getattr(account, "account_id", None) or data.get("email", ""))
        return cls(data, **kwargs)

    @property
    def label(self) -> str:
        """Safe label for logs (never the password)."""
        return mask_email(self.email) if self.email else self.account_id

    @property
    def client(self) -> httpx.AsyncClient:
        """The underlying HTTP client."""
        return self._client

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def __aenter__(self) -> "TeraBoxClient":
        """Authenticate and return the client for ``async with`` usage."""
        await self.authenticate()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        """Close the HTTP client."""
        await self.close()

    async def close(self) -> None:
        """Close the HTTP client (only when this instance created it)."""
        if self._owns_client:
            await self._client.aclose()

    def _token_store(self) -> TokenStore:
        """Lazily create the encrypted token store."""
        if self._store is None:
            self._store = TokenStore(self.settings.fernet_secret)
        return self._store

    def _token_headers(self) -> Dict[str, str]:
        """Return the ``Authorization`` header for API calls."""
        if not self.access_token:
            raise AuthenticationError("TeraBox client has no access token; call authenticate() first")
        return {"Authorization": f"Bearer {self.access_token}"}

    def _load_stored_tokens(self) -> None:
        """Load cached tokens from the encrypted store, if any."""
        try:
            stored = self._token_store().get_token(SERVICE_TERABOX, self.account_id)
        except Exception as exc:  # pragma: no cover - storage failures are not fatal
            LOGGER.warning("Could not read stored TeraBox tokens for %s: %s", self.label, exc)
            return
        if not stored:
            return
        self.access_token = stored.get("access_token") or self.access_token
        self.refresh_token_value = stored.get("refresh_token") or self.refresh_token_value
        expires_at = stored.get("expires_at")
        if isinstance(expires_at, (int, float)):
            self.token_expires_at = float(expires_at)

    def _persist_tokens(self, payload: Dict[str, Any]) -> None:
        """Encrypt and store the refreshed token payload."""
        try:
            self._token_store().save_token(SERVICE_TERABOX, self.account_id, payload)
        except Exception as exc:  # pragma: no cover - never crash on persistence
            LOGGER.error("Failed to persist TeraBox tokens for %s: %s", self.label, exc)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    async def authenticate(self) -> str:
        """Obtain a usable access token.

        Strategy:

        1. Reuse the in-memory / stored access token when it is still valid.
        2. Otherwise perform the OAuth2 refresh-token grant.
        3. Otherwise (no refresh token) try the client-credentials grant.

        Returns:
            The access token.

        Raises:
            AuthenticationError: When no grant can produce a token.
        """
        if not self.access_token:
            self._load_stored_tokens()

        if self.access_token and self._token_is_valid():
            LOGGER.debug("Reusing cached TeraBox token for %s", self.label)
            return self.access_token

        if self.refresh_token_value:
            try:
                return await self.refresh_token()
            except Exception as exc:
                LOGGER.warning("Refresh grant failed for %s: %s", self.label, exc)

        if self.app_id and self.app_key:
            try:
                return await self._client_credentials_grant()
            except Exception as exc:
                LOGGER.warning("Client-credentials grant failed for %s: %s", self.label, exc)

        if self.access_token:
            # Trust a pre-supplied token even without expiry metadata.
            LOGGER.info("Using pre-supplied TeraBox access token for %s", self.label)
            return self.access_token

        raise AuthenticationError(
            "No usable TeraBox credentials. Provide access_token/refresh_token, or set "
            "TERABOX_APP_ID and TERABOX_APP_KEY so the client-credentials grant can run.",
            account=self.label,
        )

    def _token_is_valid(self) -> bool:
        """Return ``True`` when the cached access token is not about to expire."""
        if not self.access_token:
            return False
        if self.token_expires_at <= 0:
            # No expiry information: assume valid, the API will tell us otherwise.
            return True
        return time.time() + TOKEN_REFRESH_SKEW_SECONDS < self.token_expires_at

    @async_retry(max_retries=3, base_delay=1.0, max_delay=15.0)
    async def refresh_token(self) -> str:
        """Exchange the refresh token for a new access token.

        Returns:
            The new access token.

        Raises:
            AuthenticationError: When no refresh token is available or the
                server rejects the grant.
        """
        if not self.refresh_token_value:
            raise AuthenticationError("No refresh_token configured", account=self.label)

        params: Dict[str, Any] = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token_value,
            "client_id": self.app_id,
            "client_secret": self.app_key,
        }
        response = await self._client.get(self.settings.TERABOX_OAUTH_PATH, params=params)
        data = self._parse_json(response, context="refresh_token")

        token = data.get("access_token")
        if not token:
            raise AuthenticationError(
                f"Refresh grant returned no access_token: {self._error_hint(data)}",
                account=self.label,
            )

        self.access_token = token
        self.refresh_token_value = data.get("refresh_token", self.refresh_token_value)
        expires_in = int(data.get("expires_in") or 2 * 24 * 3600)
        self.token_expires_at = time.time() + expires_in

        self._persist_tokens(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token_value,
                "expires_in": expires_in,
                "expires_at": self.token_expires_at,
                "scope": data.get("scope", ""),
            }
        )
        LOGGER.info("Refreshed TeraBox token for %s (expires in %ss)", self.label, expires_in)
        return self.access_token

    async def _client_credentials_grant(self) -> str:
        """Fetch an app-level token (used when no user refresh token exists)."""
        params = {
            "grant_type": "client_credentials",
            "client_id": self.app_id,
            "client_secret": self.app_key,
        }
        response = await self._client.get(self.settings.TERABOX_OAUTH_PATH, params=params)
        data = self._parse_json(response, context="client_credentials")
        token = data.get("access_token")
        if not token:
            raise AuthenticationError(
                f"client_credentials grant returned no token: {self._error_hint(data)}",
                account=self.label,
            )
        self.access_token = token
        expires_in = int(data.get("expires_in") or 2 * 24 * 3600)
        self.token_expires_at = time.time() + expires_in
        self._persist_tokens(
            {
                "access_token": token,
                "expires_in": expires_in,
                "expires_at": self.token_expires_at,
                "grant": "client_credentials",
            }
        )
        LOGGER.info("Obtained app token for %s (expires in %ss)", self.label, expires_in)
        return token

    async def ensure_token(self) -> str:
        """Return a valid token, refreshing transparently when needed."""
        if self.access_token and self._token_is_valid():
            return self.access_token
        return await self.authenticate()

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _parse_json(self, response: httpx.Response, context: str = "request") -> Dict[str, Any]:
        """Validate an HTTP response and decode its JSON body.

        Args:
            response: The raw response.
            context: Description used in error messages.

        Returns:
            The decoded JSON object.

        Raises:
            RateLimitError: On HTTP 429.
            TeraBoxError: On any other non-2xx status or malformed body.
        """
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise RateLimitError(
                f"TeraBox rate limited during {context}",
                retry_after=float(retry_after) if retry_after else None,
            )
        if response.status_code >= 500:
            raise TeraBoxTransientError(
                f"TeraBox HTTP {response.status_code} during {context}: {response.text[:300]}",
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise TeraBoxError(
                f"TeraBox HTTP {response.status_code} during {context}: {response.text[:300]}",
                status=response.status_code,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise TeraBoxTransientError(
                f"TeraBox returned non-JSON during {context}: {response.text[:200]}"
            ) from exc
        if isinstance(data, dict) and data.get("errno") not in (None, 0):
            errno = data.get("errno")
            message = self._error_hint(data)
            if errno in AUTH_ERROR_CODES:
                self.access_token = ""
                self.token_expires_at = 0.0
                raise AuthenticationError(f"TeraBox auth error during {context}: {message}", errno=errno)
            raise TeraBoxError(f"TeraBox error during {context}: {message}", errno=errno)
        return data if isinstance(data, dict) else {"data": data}

    @staticmethod
    def _error_hint(data: Dict[str, Any]) -> str:
        """Extract the most descriptive error message from a TeraBox payload."""
        for key in ("error_description", "errmsg", "error", "message", "msg"):
            value = data.get(key)
            if value:
                return str(value)
        return json.dumps({k: v for k, v in data.items() if k != "list"})[:200]

    @async_retry(max_retries=3, base_delay=1.0, max_delay=20.0, exclude=(RateLimitError,))
    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Any] = None,
        context: str = "request",
        retry_on_auth: bool = True,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Perform an authenticated API request with retry and token refresh.

        Args:
            method: HTTP verb.
            url: Path relative to the base URL.
            params: Query parameters.
            data: Form body.
            files: Multipart files.
            context: Description for logs and errors.
            retry_on_auth: Retry once after refreshing an expired token.
            **kwargs: Extra arguments forwarded to httpx.

        Returns:
            The decoded JSON response.

        Raises:
            AuthenticationError: When the token cannot be refreshed.
            TeraBoxError: For any other API failure.
        """
        await self.ensure_token()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update(self._token_headers())
        try:
            response = await self._client.request(
                method, url, params=params, data=data, files=files, headers=headers, **kwargs
            )
            return self._parse_json(response, context=context)
        except AuthenticationError:
            if not retry_on_auth:
                raise
            LOGGER.info("TeraBox token rejected during %s; refreshing once", context)
            self.access_token = ""
            self.token_expires_at = 0.0
            await self.authenticate()
            headers.update(self._token_headers())
            response = await self._client.request(
                method, url, params=params, data=data, files=files, headers=headers, **kwargs
            )
            return self._parse_json(response, context=context)

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------
    @async_retry(max_retries=3, base_delay=1.0, max_delay=20.0)
    async def list_files(self, path: str = "/") -> List[Dict[str, Any]]:
        """List the contents of a remote directory.

        Args:
            path: Remote directory path (``/`` for the account root).

        Returns:
            List of ``{name, path, size, type, fs_id, md5}`` dicts. A missing
            directory yields an empty list rather than an exception.

        Raises:
            TeraBoxError: On API failures other than "not found".
        """
        params = {
            "method": "list",
            "dir": path or "/",
            "order": "time",
            "desc": 1,
            "limit": 1000,
        }
        try:
            payload = await self._request(
                "GET", "/rest/2.0/xpan/file", params=params, context=f"list {path}"
            )
        except TeraBoxError as exc:
            if exc.details.get("errno") in {-9, 2, 31066, 31064}:  # path does not exist
                LOGGER.info("TeraBox directory %s does not exist yet on %s", path, self.label)
                return []
            raise

        entries: List[Dict[str, Any]] = []
        for item in payload.get("list") or []:
            isdir = int(item.get("isdir") or 0) == 1
            name = item.get("server_filename") or item.get("filename") or ""
            entries.append(
                TeraBoxFile(
                    name=name,
                    path=item.get("path") or os.path.join(path, name),
                    size=int(item.get("size") or 0),
                    type="folder" if isdir else "file",
                    isdir=isdir,
                    fs_id=item.get("fs_id"),
                    md5=item.get("md5", ""),
                    server_mtime=item.get("server_mtime"),
                ).to_dict()
            )
        LOGGER.debug("Listed %s entries under %s on %s", len(entries), path, self.label)
        return entries

    async def list_recursive(self, path: str = "/", max_depth: int = 3) -> List[Dict[str, Any]]:
        """Recursively list ``path`` up to ``max_depth`` levels deep.

        Args:
            path: Root path to walk.
            max_depth: Maximum recursion depth (``1`` = only the root level).

        Returns:
            A flat list of file dicts (folders are traversed, not returned).
        """
        results: List[Dict[str, Any]] = []
        if max_depth <= 0:
            return results
        for entry in await self.list_files(path):
            if entry["type"] == "folder":
                results.extend(await self.list_recursive(entry["path"], max_depth - 1))
            else:
                results.append(entry)
        return results

    @async_retry(max_retries=3, base_delay=1.0, max_delay=20.0)
    async def create_folder(self, path: str) -> bool:
        """Create a remote folder (creating parent directories as needed).

        Args:
            path: Absolute remote folder path.

        Returns:
            ``True`` when the folder exists afterwards, ``False`` on failure.
        """
        normalized = "/" + path.strip("/")
        if normalized in {"", "/"}:
            return True  # the account root always exists
        parent = os.path.dirname(normalized) or "/"
        if parent not in {"", "/"} and parent != normalized:
            await self.create_folder(parent)

        payload = await self._request(
            "POST",
            "/rest/2.0/xpan/file",
            params={"method": "create"},
            data={"path": normalized, "isdir": 1, "rtype": 1},
            context=f"create folder {normalized}",
        )
        created = payload.get("errno") in (0, None)
        LOGGER.debug("Created folder %s on %s (ok=%s)", normalized, self.label, created)
        return bool(created)

    async def _remote_size(self, remote_path: str) -> Optional[int]:
        """Return the size of a remote file, or ``None`` when it is absent."""
        parent = os.path.dirname(remote_path) or "/"
        name = os.path.basename(remote_path)
        try:
            for entry in await self.list_files(parent):
                if entry["name"] == name and entry["type"] == "file":
                    return int(entry["size"])
        except TeraBoxError as exc:
            LOGGER.debug("Could not stat %s on %s: %s", remote_path, self.label, exc)
        return None

    async def upload_file(
        self,
        local_path: str,
        remote_path: str,
        chunk_size: Optional[int] = None,
        verify: bool = True,
    ) -> Dict[str, Any]:
        """Upload a local file, using precreate/shard/merge for large files.

        Flow (following the Open Platform 2.0 upload protocol):

        1. Ensure the destination folder exists (``create_folder``).
        2. Compute the MD5 of each 4 MiB block.
        3. ``precreate`` -- register the upload, receiving ``uploadid`` and
           the list of shards the server still wants.
        4. Upload each required shard (superfile endpoint when available, the
           plain upload endpoint otherwise).
        5. ``merge`` -- finalise and create the file entry.

        Args:
            local_path: Path of the local file.
            remote_path: Destination path on TeraBox.
            chunk_size: Shard size in bytes (default ``TERABOX_CHUNK_SIZE``).
            verify: Re-list the destination afterwards to confirm the size.

        Returns:
            ``{path, size, url, md5, account_id, uploadid}``.

        Raises:
            FileNotFoundError: When ``local_path`` does not exist.
            TeraBoxError: When the upload cannot be completed.
        """
        source = Path(local_path)
        if not source.is_file():
            raise FileNotFoundError(f"Local file not found: {local_path}")

        file_size = source.stat().st_size
        block_size = int(chunk_size or self.settings.TERABOX_CHUNK_SIZE or DEFAULT_CHUNK_SIZE)
        block_size = max(block_size, DEFAULT_CHUNK_SIZE)

        remote_dir = os.path.dirname(remote_path) or "/"
        await self.create_folder(remote_dir)

        if file_size == 0:
            # Zero byte files still need a precreate/merge round trip; the
            # protocol expects a single empty block list entry.
            block_list: List[str] = []
            rtype = 3
        else:
            block_list = self._block_md5s(source, block_size)
            rtype = 3 if file_size > block_size else 3

        payload = {
            "path": remote_path,
            "size": file_size,
            "isdir": 0,
            "block_list": json.dumps(block_list),
            "rtype": rtype,
            "autoinit": 1,
        }
        precreate = await self._request(
            "POST",
            "/rest/2.0/xpan/file",
            params={"method": "precreate"},
            data=payload,
            context=f"precreate {remote_path}",
        )
        uploadid = str(precreate.get("uploadid") or "")
        if not uploadid and precreate.get("errno") not in (0, None):
            raise TeraBoxError(f"precreate failed for {remote_path}: {self._error_hint(precreate)}")

        wanted = self._wanted_shards(precreate, block_list)
        if wanted:
            await self._upload_shards(source, remote_path, uploadid, block_list, wanted, block_size)

        merge = await self._request(
            "POST",
            "/rest/2.0/xpan/file",
            params={"method": "merge"},
            data={
                "path": remote_path,
                "size": file_size,
                "isdir": 0,
                "block_list": json.dumps(block_list),
                "uploadid": uploadid,
                "rtype": 3,
            },
            context=f"merge {remote_path}",
        )
        if merge.get("errno") not in (0, None):
            raise TeraBoxError(f"merge failed for {remote_path}: {self._error_hint(merge)}")

        if verify:
            remote_size = await self._remote_size(remote_path)
            if remote_size is not None and remote_size != file_size:
                raise TeraBoxError(
                    f"Upload size mismatch for {remote_path}: local={file_size} remote={remote_size}"
                )

        url = await self.get_file_url(remote_path)
        result = {
            "path": remote_path,
            "size": file_size,
            "url": url,
            "md5": block_list[0] if len(block_list) == 1 else "",
            "account_id": self.account_id,
            "email": self.email,
            "uploadid": uploadid,
        }
        LOGGER.info("Uploaded %s (%s bytes) to TeraBox %s", remote_path, file_size, self.label)
        return result

    @staticmethod
    def _block_md5s(path: Path, block_size: int) -> List[str]:
        """Return the MD5 hash of every block of ``path``."""
        digests: List[str] = []
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(block_size)
                if not chunk:
                    break
                digests.append(hashlib.md5(chunk).hexdigest())
        return digests

    @staticmethod
    def _wanted_shards(precreate: Dict[str, Any], block_list: List[str]) -> List[int]:
        """Work out which shard indexes the server still needs.

        TeraBox answers ``precreate`` with ``block_list`` counting the blocks
        it already has (rapid-upload). When the response carries no block
        list, every shard must be uploaded.
        """
        already_have = precreate.get("block_list") or []
        if not isinstance(already_have, list):
            return list(range(len(block_list)))
        if len(already_have) >= len(block_list):
            return []
        return list(range(len(already_have), len(block_list)))

    async def _upload_shards(
        self,
        source: Path,
        remote_path: str,
        uploadid: str,
        block_list: List[str],
        wanted: List[int],
        block_size: int,
    ) -> None:
        """Upload the shards the server asked for.

        TODO: TeraBox has two upload revisions. The modern one is a multipart
        POST to ``https://{server}.pcs.baidu.com/rest/2.0/pcs/superfile2``
        with fields ``method=upload``, ``type=tmpfile``, ``path``,
        ``uploadid``, ``partseq``. Older revisions accept
        ``method=upload`` on ``/rest/2.0/xpan/file`` with the file part named
        ``file``. Set ``TERABOX_UPLOAD_HOST`` (or patch this method) when your
        account is provisioned against the older revision.
        """
        upload_host = os.getenv("TERABOX_UPLOAD_HOST", "").rstrip("/")
        with source.open("rb") as handle:
            for index in wanted:
                handle.seek(index * block_size)
                blob = handle.read(block_size)
                params = {
                    "method": "upload",
                    "type": "tmpfile",
                    "path": remote_path,
                    "uploadid": uploadid,
                    "partseq": index,
                }
                files = {"file": (os.path.basename(remote_path), blob, "application/octet-stream")}
                if upload_host:
                    url = f"{upload_host}/rest/2.0/pcs/superfile2"
                else:
                    url = "/rest/2.0/xpan/file"
                payload = await self._request(
                    "POST",
                    url,
                    params=params,
                    files=files,
                    context=f"upload shard {index} of {remote_path}",
                    retry_on_auth=False,
                    timeout=self.settings.TERABOX_UPLOAD_TIMEOUT,
                )
                if payload.get("errno") not in (0, None):
                    raise TeraBoxError(
                        f"Shard {index} upload failed for {remote_path}: {self._error_hint(payload)}"
                    )
        LOGGER.debug("Uploaded %s shard(s) for %s", len(wanted), remote_path)

    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Stream a remote file to a local path.

        Args:
            remote_path: Source path on TeraBox.
            local_path: Destination path on the local filesystem.

        Returns:
            ``True`` on success, ``False`` when the download failed (the
            failure is logged; partial files are removed).
        """
        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination.with_suffix(destination.suffix + ".part")
        try:
            url = await self.get_file_url(remote_path)
            if not url:
                LOGGER.error("No download URL for %s on %s", remote_path, self.label)
                return False
            timeout = httpx.Timeout(
                self.settings.TERABOX_REQUEST_TIMEOUT, read=self.settings.TERABOX_UPLOAD_TIMEOUT
            )
            async with self._client.stream("GET", url, timeout=timeout) as response:
                if response.status_code >= 400:
                    LOGGER.error(
                        "Download of %s failed with HTTP %s", remote_path, response.status_code
                    )
                    return False
                with temp_path.open("wb") as handle:
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 256):
                        handle.write(chunk)
            temp_path.replace(destination)
            LOGGER.info("Downloaded %s -> %s (%s bytes)", remote_path, destination, destination.stat().st_size)
            return True
        except (httpx.HTTPError, OSError, TeraBoxError) as exc:
            LOGGER.error("Download of %s failed: %s", remote_path, exc)
            if temp_path.exists():
                with contextlib.suppress(OSError):  # pragma: no cover - best effort cleanup
                    temp_path.unlink()
            return False

    @async_retry(max_retries=3, base_delay=1.0, max_delay=15.0)
    async def get_file_url(self, remote_path: str) -> str:
        """Return a temporary direct download URL for ``remote_path``.

        Args:
            remote_path: Remote file path.

        Returns:
            The signed download URL, or ``""`` when the file is missing.
        """
        params = {
            "method": "filemetas",
            "targets": json.dumps([remote_path]),
            "dlink": 1,
        }
        try:
            payload = await self._request(
                "GET",
                "/rest/2.0/xpan/multimedia",
                params=params,
                context=f"filemetas {remote_path}",
            )
        except TeraBoxError as exc:
            if exc.details.get("errno") in {-9, 2, 31066, 31064}:
                return ""
            raise

        info = payload.get("info") or []
        dlink = ""
        if isinstance(info, list) and info:
            dlink = info[0].get("dlink", "")
        dlink = dlink or payload.get("dlink", "")
        if not dlink:
            return ""
        # TeraBox hands back a plain dlink; the download host expects the
        # access token appended.
        separator = "&" if "?" in dlink else "?"
        return f"{dlink}{separator}access_token={self.access_token}"

    @async_retry(max_retries=3, base_delay=1.0, max_delay=15.0)
    async def file_exists(self, remote_path: str) -> bool:
        """Return ``True`` when ``remote_path`` exists as a file."""
        return await self._remote_size(remote_path) is not None

    @async_retry(max_retries=3, base_delay=1.0, max_delay=15.0)
    async def delete_file(self, remote_path: str) -> bool:
        """Delete a remote file.

        Args:
            remote_path: Path to delete.

        Returns:
            ``True`` when the API accepted the deletion.
        """
        payload = await self._request(
            "POST",
            "/rest/2.0/xpan/file",
            params={"method": "delete"},
            data={"filelist": json.dumps([remote_path]), "async": 0},
            context=f"delete {remote_path}",
        )
        deleted = payload.get("errno") in (0, None)
        if deleted:
            LOGGER.info("Deleted %s from TeraBox %s", remote_path, self.label)
        return bool(deleted)

    # ------------------------------------------------------------------
    # Quota
    # ------------------------------------------------------------------
    async def get_quota(self, refresh: bool = False) -> TeraBoxQuota:
        """Return the account quota in bytes.

        Args:
            refresh: Bypass the in-memory cache.

        Returns:
            A :class:`TeraBoxQuota`; zeroed when the API cannot be reached.
        """
        if self._quota_cache is not None and not refresh:
            return self._quota_cache
        try:
            payload = await self._request(
                "GET", "/rest/2.0/xpan/nas", params={"method": "uinfo"}, context="quota"
            )
        except (TeraBoxError, AuthenticationError) as exc:
            LOGGER.warning("Quota lookup failed for %s: %s", self.label, exc)
            configured = (self.account.get("quota") or {}) if isinstance(self.account, dict) else {}
            quota = TeraBoxQuota(
                total=int(configured.get("total", 0)),
                used=int(configured.get("used", 0)),
                free=int(configured.get("free", 0)),
                raw={"error": str(exc)},
            )
            self._quota_cache = quota
            return quota

        total = int(payload.get("total") or payload.get("quota") or 0)
        used = int(payload.get("used") or payload.get("used_size") or 0)
        free = max(total - used, 0) if total else 0
        quota = TeraBoxQuota(total=total, used=used, free=free, raw=payload)
        self._quota_cache = quota
        return quota

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------
    async def read_text(self, remote_path: str, max_bytes: int = 5 * 1024 * 1024) -> Optional[str]:
        """Download a small remote text file into memory.

        Args:
            remote_path: Remote path of the text file.
            max_bytes: Safety cap; larger files are rejected.

        Returns:
            The decoded text, or ``None`` when the file is missing or too big.
        """
        url = await self.get_file_url(remote_path)
        if not url:
            return None
        try:
            response = await self._client.get(url, follow_redirects=True)
            if response.status_code >= 400:
                LOGGER.error("Text download of %s failed: HTTP %s", remote_path, response.status_code)
                return None
            if len(response.content) > max_bytes:
                LOGGER.warning("%s exceeds the %s byte text limit", remote_path, max_bytes)
                return None
            return response.content.decode("utf-8", errors="replace")
        except httpx.HTTPError as exc:
            LOGGER.error("Text download of %s failed: %s", remote_path, exc)
            return None

    async def write_text(self, remote_path: str, content: str, encoding: str = "utf-8") -> Dict[str, Any]:
        """Write an in-memory string to a remote file.

        A temporary local file is used so the regular (sharded) upload path
        applies to arbitrarily large documents.

        Args:
            remote_path: Destination remote path.
            content: Text to write.
            encoding: Text encoding.

        Returns:
            The upload result dict.
        """
        workspace = self.settings.workspace_path / "tmp"
        workspace.mkdir(parents=True, exist_ok=True)
        temp_path = workspace / f"upload-{uuid.uuid4().hex}.tmp"
        try:
            temp_path.write_text(content, encoding=encoding)
            return await self.upload_file(str(temp_path), remote_path)
        finally:
            if temp_path.exists():
                with contextlib.suppress(OSError):  # pragma: no cover - best effort cleanup
                    temp_path.unlink()

    async def get_account_info(self) -> Dict[str, Any]:
        """Return basic account information (quota + label)."""
        quota = await self.get_quota()
        return {
            "account_id": self.account_id,
            "email": mask_email(self.email),
            "quota": quota.to_dict(),
            "token_valid": self._token_is_valid(),
        }


__all__ = ["TeraBoxClient", "TeraBoxFile", "TeraBoxQuota", "AUTH_ERROR_CODES"]
