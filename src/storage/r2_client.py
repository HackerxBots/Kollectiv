"""Cloudflare R2 (S3-compatible) object storage client.

R2 gives Kollektiv a genuinely free, verifiable shared drive: the free tier
includes 10 GB of storage with no egress fees, and the API is the S3 REST
dialect — so this module signs requests with :mod:`src.utils.sigv4` and talks
plain ``httpx``, exactly like every other client in the project.

The same client works against AWS S3, Backblaze B2, MinIO or any other
S3-compatible endpoint by changing ``R2_ENDPOINT``.

Usage::

    client = R2Client(bucket="kollektiv", access_key_id="…", secret_access_key="…",
                      endpoint="https://<account>.r2.cloudflarestorage.com")
    await client.put_file("state.md", "kollektiv/prj_1/PROJECT_STATE.md")
    text = await client.get_text("kollektiv/prj_1/PROJECT_STATE.md")
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from config.settings import Settings, get_settings
from src.utils.errors import (
    AuthenticationError,
    ConfigurationError,
    NotFoundError,
    R2Error,
    R2TransientError,
)
from src.utils.logger import get_logger
from src.utils.sigv4 import UNSIGNED_PAYLOAD, presign_url, sha256_hex, sign_request

LOGGER = get_logger(__name__)

#: Files at or below this size are hashed and uploaded in one request; larger
#: ones stream with ``UNSIGNED-PAYLOAD`` to avoid buffering them in memory.
MAX_BUFFERED_UPLOAD = 64 * 1024 * 1024

_LIST_PAGE_SIZE = 1000


def content_type_for(path: str) -> str:
    """Guess a content type from a filename, defaulting to octet-stream."""
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


class R2Client:
    """One S3-compatible bucket.

    Args:
        bucket: Bucket name.
        access_key_id / secret_access_key: R2 API token credentials.
        endpoint: ``https://<account_id>.r2.cloudflarestorage.com``.
        region: Signing region (``auto`` for R2).
        prefix: Key prefix for everything Kollektiv writes.
        label: Human readable name (used in logs and the account id).
        account_id: Override the derived account id.
        session_token: Optional STS session token.
        settings: Settings override (tests / embedders).
        client: Pre-built ``httpx.AsyncClient`` (tests).
    """

    def __init__(
        self,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        endpoint: str,
        *,
        region: str = "auto",
        prefix: str = "",
        label: str = "",
        account_id: Optional[str] = None,
        session_token: str = "",
        settings: Optional[Settings] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        if not (bucket and access_key_id and secret_access_key and endpoint):
            raise ConfigurationError(
                "R2 needs R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_ENDPOINT."
            )
        self.settings = settings or get_settings()
        self.bucket = bucket
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.endpoint = endpoint.rstrip("/")
        self.region = region or "auto"
        self.prefix = (prefix or "").strip("/")
        self.session_token = session_token
        self.label = label or bucket
        self.account_id = account_id or hashlib.sha256(self.label.encode("utf-8")).hexdigest()[:16]

        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.R2_REQUEST_TIMEOUT, connect=10.0),
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------
    @property
    def client(self) -> httpx.AsyncClient:
        """The underlying HTTP client."""
        return self._client

    def key(self, remote_path: str) -> str:
        """Map a Kollektiv remote path (``/Kollektiv/prj_x/a.py``) to an R2 key."""
        cleaned = (remote_path or "").lstrip("/")
        if self.prefix:
            if cleaned == self.prefix or cleaned.startswith(self.prefix + "/"):
                return cleaned
            return f"{self.prefix}/{cleaned}" if cleaned else self.prefix
        return cleaned

    def path(self, key: str) -> str:
        """Inverse of :meth:`key`: an R2 key back to a remote-style path."""
        stripped = key
        if self.prefix and stripped.startswith(self.prefix + "/"):
            stripped = stripped[len(self.prefix) + 1 :]
        return "/" + stripped.lstrip("/")

    def _object_url(self, key: str) -> str:
        """Return the URL for an object, percent-encoding the key once."""
        return f"{self.endpoint}/{self.bucket}/{quote(key, safe='/-_.~')}"

    def _bucket_url(self) -> str:
        """Return the bucket URL (used for listing)."""
        return f"{self.endpoint}/{self.bucket}"

    def _signed_headers(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[bytes] = None,
        payload_hash: Optional[str] = None,
    ) -> Dict[str, str]:
        """Sign a request with SigV4."""
        return sign_request(
            method,
            url,
            access_key=self.access_key_id,
            secret_key=self.secret_access_key,
            region=self.region,
            service="s3",
            headers=headers,
            params=params,
            body=body,
            payload_hash=payload_hash,
            session_token=self.session_token,
        )

    def _raise_for_status(self, response: httpx.Response, context: str) -> None:
        """Translate an S3 error response into a typed Kollektiv error."""
        if response.is_success:
            return
        detail = self._error_detail(response)
        status = response.status_code
        if status in (401, 403):
            raise AuthenticationError(f"R2 {context} was denied ({status}): {detail}")
        if status == 404:
            raise NotFoundError(f"R2 {context}: {detail or 'not found'}")
        if status == 429 or status >= 500:
            raise R2TransientError(f"R2 {context} failed ({status}): {detail}")
        raise R2Error(f"R2 {context} failed ({status}): {detail}")

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        """Extract the S3 ``<Code>/<Message>`` pair from an error body."""
        try:
            text = response.text
        except Exception:  # noqa: BLE001 - body may be a stream  # pragma: no cover
            return ""
        if not text:
            return ""
        if text.lstrip().startswith("<"):
            code = _between(text, "<Code>", "</Code>")
            message = _between(text, "<Message>", "</Message>")
            return " ".join(part for part in (code, message) if part)
        return text[:200]

    # ------------------------------------------------------------------
    # Object operations
    # ------------------------------------------------------------------
    async def put_bytes(
        self, key: str, data: bytes, content_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """Upload ``data`` under ``key``.

        Returns:
            ``{key, path, size, md5, etag, url}``.
        """
        url = self._object_url(key)
        headers = {
            "content-type": content_type or content_type_for(key),
            "content-length": str(len(data)),
            "x-amz-content-sha256": sha256_hex(data),
        }
        signed = self._signed_headers("PUT", url, headers=headers, body=data)
        signed.pop("x-amz-content-sha256", None)  # identical value; httpx sets the rest
        async with self._client.stream("PUT", url, headers=signed, content=data) as response:
            body = await response.aread()
        response = httpx.Response(response.status_code, headers=response.headers, content=body)
        self._raise_for_status(response, f"upload of {key}")
        LOGGER.debug("Uploaded %s (%s bytes) to R2 bucket %s", key, len(data), self.bucket)
        return {
            "key": key,
            "path": self.path(key),
            "size": len(data),
            "md5": hashlib.md5(data).hexdigest(),  # noqa: S324 - S3 ETag compatibility
            "etag": response.headers.get("etag", "").strip('"'),
            "url": self.public_url(key) or self._object_url(key),
        }

    async def put_file(
        self,
        local_path: str,
        key: str,
        content_type: Optional[str] = None,
        progress: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Upload a file from disk (streamed when it is large)."""
        if not os.path.isfile(local_path):
            raise FileNotFoundError(f"Local file not found: {local_path}")
        size = os.path.getsize(local_path)
        if size <= MAX_BUFFERED_UPLOAD:
            with open(local_path, "rb") as handle:
                data = handle.read()
            return await self.put_bytes(key, data, content_type=content_type)

        url = self._object_url(key)
        headers = {
            "content-type": content_type or content_type_for(local_path),
            "content-length": str(size),
        }
        signed = self._signed_headers("PUT", url, headers=headers, payload_hash=UNSIGNED_PAYLOAD)
        digest = hashlib.md5()  # noqa: S324 - S3 ETag compatibility

        async def _stream() -> Any:
            """Yield the file in chunks while computing its digest."""
            sent = 0
            with open(local_path, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    sent += len(chunk)
                    if progress is not None:
                        await progress(sent, size)
                    yield chunk

        response = await self._client.put(url, headers=signed, content=_stream())
        self._raise_for_status(response, f"upload of {key}")
        LOGGER.info("Streamed %s (%s bytes) to R2 bucket %s", local_path, size, self.bucket)
        return {
            "key": key,
            "path": self.path(key),
            "size": size,
            "md5": digest.hexdigest(),
            "etag": response.headers.get("etag", "").strip('"'),
            "url": self.public_url(key) or self._object_url(key),
        }

    async def put_text(self, key: str, text: str, content_type: str = "text/markdown") -> Dict[str, Any]:
        """Upload a UTF-8 text document."""
        return await self.put_bytes(key, text.encode("utf-8"), content_type=content_type)

    async def get_bytes(self, key: str) -> bytes:
        """Download an object into memory."""
        url = self._object_url(key)
        signed = self._signed_headers("GET", url)
        response = await self._client.get(url, headers=signed)
        self._raise_for_status(response, f"download of {key}")
        return response.content

    async def get_text(self, key: str, encoding: str = "utf-8") -> str:
        """Download an object and decode it as text."""
        return (await self.get_bytes(key)).decode(encoding, errors="replace")

    async def get_file(
        self, key: str, local_path: str, progress: Optional[Any] = None
    ) -> bool:
        """Stream an object to ``local_path``. Returns ``True`` on success."""
        url = self._object_url(key)
        signed = self._signed_headers("GET", url)
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            async with self._client.stream("GET", url, headers=signed) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    self._raise_for_status(
                        httpx.Response(response.status_code, headers=response.headers, content=body),
                        f"download of {key}",
                    )
                with open(target, "wb") as handle:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        handle.write(chunk)
                        written += len(chunk)
                        if progress is not None:
                            await progress(written)
        except (R2Error, ConfigurationError, AuthenticationError, NotFoundError):
            raise
        except Exception as exc:  # noqa: BLE001 - surface as a storage error
            raise R2TransientError(f"Download of {key} failed: {exc}") from exc
        LOGGER.debug("Downloaded %s (%s bytes) from R2", key, written)
        return True

    async def delete(self, key: str) -> bool:
        """Delete an object (idempotent: a missing key is not an error)."""
        url = self._object_url(key)
        signed = self._signed_headers("DELETE", url)
        response = await self._client.delete(url, headers=signed)
        if response.status_code == 404:
            LOGGER.debug("R2 delete: %s was already gone", key)
            return False
        self._raise_for_status(response, f"delete of {key}")
        return True

    async def head(self, key: str) -> Dict[str, Any]:
        """Return an object's metadata without downloading it."""
        url = self._object_url(key)
        signed = self._signed_headers("HEAD", url)
        response = await self._client.head(url, headers=signed)
        self._raise_for_status(response, f"head of {key}")
        return {
            "key": key,
            "path": self.path(key),
            "size": int(response.headers.get("content-length") or 0),
            "etag": response.headers.get("etag", "").strip('"'),
            "last_modified": response.headers.get("last-modified", ""),
            "content_type": response.headers.get("content-type", ""),
            "type": "file",
        }

    async def list_objects(
        self, prefix: str = "", max_keys: int = 0, include_folders: bool = False
    ) -> List[Dict[str, Any]]:
        """List objects (and, optionally, ``<CommonPrefixes>`` folders).

        Args:
            prefix: Key prefix (remote-style paths are accepted).
            max_keys: Stop after this many objects (``0`` = everything).
            include_folders: Include folder entries for ``<CommonPrefixes>``.

        Returns:
            ``[{key, path, name, size, etag, last_modified, type}]``.
        """
        key_prefix = self.key(prefix) if prefix else self.prefix
        if key_prefix and not key_prefix.endswith("/"):
            key_prefix += "/"
        collected: List[Dict[str, Any]] = []
        token = ""
        while True:
            params: Dict[str, Any] = {"list-type": "2", "max-keys": _LIST_PAGE_SIZE}
            if key_prefix:
                params["prefix"] = key_prefix
            params["delimiter"] = "/"
            if token:
                params["continuation-token"] = token
            url = self._bucket_url()
            signed = self._signed_headers("GET", url, params=params)
            response = await self._client.get(url, headers=signed, params=params)
            self._raise_for_status(response, "list objects")
            payload = _parse_listing(response.text)
            entries = list(payload["objects"]) + (list(payload["folders"]) if include_folders else [])
            for entry in entries:
                entry.setdefault("name", str(entry.get("key", "")).rsplit("/", 1)[-1])
                if not str(entry.get("name") or "").strip("/"):
                    entry["name"] = str(entry.get("key", "")).rstrip("/").rsplit("/", 1)[-1]
                entry["path"] = self.path(str(entry.get("key", ""))).rstrip("/") or "/"
            collected.extend(entries)
            token = payload["next_token"]
            if not payload["truncated"] or not token:
                break
            if max_keys and len(collected) >= max_keys:
                break
        if max_keys:
            collected = collected[:max_keys]
        return collected

    async def head_bucket(self) -> Dict[str, Any]:
        """Check that the bucket exists and the credentials can reach it."""
        url = self._bucket_url()
        signed = self._signed_headers("HEAD", url)
        response = await self._client.head(url, headers=signed)
        self._raise_for_status(response, "head bucket")
        return {"ok": True, "bucket": self.bucket, "endpoint": self.endpoint}

    def public_url(self, key: str) -> str:
        """Return the public (custom domain or r2.dev) URL when configured."""
        base = (self.settings.R2_PUBLIC_BASE_URL or "").rstrip("/")
        if not base:
            return ""
        return f"{base}/{quote(self.key(key), safe='/-_.~')}"

    def presigned_url(self, key: str, expires: Optional[int] = None) -> str:
        """Return a time-limited URL for an object."""
        return presign_url(
            "GET",
            self._object_url(key),
            access_key=self.access_key_id,
            secret_key=self.secret_access_key,
            region=self.region,
            service="s3",
            expires=expires or self.settings.R2_PRESIGN_EXPIRES,
            session_token=self.session_token,
        )

    async def verify(self) -> bool:
        """Return ``True`` when the bucket is reachable with these credentials."""
        try:
            await self.head_bucket()
            return True
        except Exception as exc:  # noqa: BLE001 - verification is best effort
            LOGGER.warning("R2 bucket %s is not reachable: %s", self.bucket, exc)
            return False

    async def close(self) -> None:
        """Close the HTTP client (only when this instance created it)."""
        if self._owns_client:
            await self._client.aclose()


def _between(text: str, start: str, end: str) -> str:
    """Return the text between two markers (empty when absent)."""
    begin = text.find(start)
    if begin == -1:
        return ""
    begin += len(start)
    finish = text.find(end, begin)
    return text[begin:finish].strip() if finish != -1 else ""


def _parse_listing(text: str) -> Dict[str, Any]:
    """Parse an S3 ``ListObjectsV2`` XML response without an XML dependency."""
    import re

    objects: List[Dict[str, Any]] = []
    for block in re.findall(r"<Contents>(.*?)</Contents>", text, flags=re.DOTALL):
        key = _between(block, "<Key>", "</Key>")
        if not key:
            continue
        objects.append(
            {
                "key": key,
                "name": key.rsplit("/", 1)[-1],
                "size": int(_between(block, "<Size>", "</Size>") or 0),
                "etag": _between(block, "<ETag>", "</ETag>").strip('"'),
                "last_modified": _between(block, "<LastModified>", "</LastModified>"),
                "type": "file",
            }
        )
    folders: List[Dict[str, Any]] = []
    for block in re.findall(r"<CommonPrefixes>(.*?)</CommonPrefixes>", text, flags=re.DOTALL):
        prefix = _between(block, "<Prefix>", "</Prefix>")
        if prefix:
            folders.append(
                {
                    "key": prefix,
                    "name": prefix.rstrip("/").rsplit("/", 1)[-1],
                    "size": 0,
                    "etag": "",
                    "last_modified": "",
                    "type": "folder",
                }
            )
    return {
        "objects": objects,
        "folders": folders,
        "truncated": _between(text, "<IsTruncated>", "</IsTruncated>").lower() == "true",
        "next_token": _between(text, "<NextContinuationToken>", "</NextContinuationToken>"),
    }


def r2_accounts_from_settings(settings: Settings) -> List[Dict[str, str]]:
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
            LOGGER.error("R2_ACCOUNTS is not valid JSON: %s", exc)
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
                    LOGGER.warning("Skipping incomplete R2_ACCOUNTS entry #%s", index)
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
            LOGGER.error("R2_ACCOUNTS must be a JSON list or object, got %s", type(parsed).__name__)
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


def format_bytes(value: float) -> str:
    """Format a byte count for logs (``1.2 GiB``)."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PiB"


def now_iso() -> str:
    """Return the current UTC timestamp (second precision)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
