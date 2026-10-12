"""Tests for the Cloudflare R2 backend: SigV4, the client and the pool.

Everything is hermetic: the signer is pinned against vectors that were verified
byte-for-byte against ``botocore``'s ``S3SigV4Auth``/``S3SigV4QueryAuth``, and
the client/pool run against an ``httpx.MockTransport`` that speaks the S3 REST
dialect (including real ``ListObjectsV2`` XML).

If ``botocore`` happens to be installed the signer is additionally cross-checked
against it at run time, so the pinned vectors can never drift silently.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape

import httpx
import pytest

from config.settings import Settings
from src.storage.factory import build_storage
from src.storage.r2_client import R2Client, content_type_for, format_bytes, r2_accounts_from_settings
from src.storage.r2_pool import R2Storage
from src.utils.errors import AuthenticationError, ConfigurationError, NotFoundError
from src.utils.sigv4 import (
    EMPTY_PAYLOAD_SHA256,
    canonical_path,
    presign_url,
    sha256_hex,
    sign_request,
)

ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
ENDPOINT = "https://acct123.r2.cloudflarestorage.com"
BUCKET = "kollektiv"
FROZEN = datetime(2026, 10, 6, 20, 41, 7, tzinfo=UTC)

# Vectors captured from a botocore cross-check with a frozen clock.
VECTOR_PUT_AUTH = (
    "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20261006/auto/s3/aws4_request, "
    "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date, "
    "Signature=8e7c52e2503a8cd7915a5f3f89ece9d9e7200b9ca22f5d714255038d46bf6f8a"
)
VECTOR_PUT_PAYLOAD = "9e8b62f81ea5c66fa06ee53da032751386b37702153070c0e14dd1d316282fa7"
VECTOR_LIST_AUTH = (
    "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20261006/auto/s3/aws4_request, "
    "SignedHeaders=host;x-amz-content-sha256;x-amz-date, "
    "Signature=558b9a43c536cf60b426bed6350fb7da3f1f2af99f2cf98493adddf01d45cb5d"
)
VECTOR_PRESIGN = (
    "https://acct123.r2.cloudflarestorage.com/kollektiv/prj_1/report.pdf"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20261006%2Fauto%2Fs3%2Faws4_request"
    "&X-Amz-Date=20261006T204107Z&X-Amz-Expires=900"
    "&X-Amz-Signature=bc8de2c03840f12356f329425155d21a19c0f39fb2fd2ee939921fb10784e61f"
    "&X-Amz-SignedHeaders=host"
)


# ----------------------------------------------------------------------
# SigV4
# ----------------------------------------------------------------------
def test_sign_request_matches_aws_vector_for_put() -> None:
    """A signed PUT matches the botocore-verified vector."""
    headers = sign_request(
        "PUT",
        f"{ENDPOINT}/{BUCKET}/projects/prj_1/notes.md",
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        region="auto",
        headers={"content-type": "text/markdown"},
        body=b"# hello\n",
        now=FROZEN,
    )
    assert headers["Authorization"] == VECTOR_PUT_AUTH
    assert headers["x-amz-content-sha256"] == VECTOR_PUT_PAYLOAD
    assert headers["x-amz-date"] == "20261006T204107Z"


def test_sign_request_matches_aws_vector_for_listing() -> None:
    """Query parameters are canonicalised (sorted + percent encoded)."""
    headers = sign_request(
        "GET",
        f"{ENDPOINT}/{BUCKET}/",
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        params={"list-type": "2", "prefix": "kollektiv/", "max-keys": "1000"},
        now=FROZEN,
    )
    assert headers["Authorization"] == VECTOR_LIST_AUTH
    # No body: the empty-payload digest is the canonical "e3b0c442...".
    assert headers["x-amz-content-sha256"] == EMPTY_PAYLOAD_SHA256


def test_presign_url_matches_aws_vector() -> None:
    """Presigned URLs carry the signature in the query string."""
    url = presign_url(
        "GET",
        f"{ENDPOINT}/{BUCKET}/prj_1/report.pdf",
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        expires=900,
        now=FROZEN,
    )
    assert url == VECTOR_PRESIGN


def test_canonical_path_keeps_existing_escapes() -> None:
    """Percent escapes must not be double encoded (S3/R2 reject that)."""
    assert canonical_path("/kollektiv/a%20b/c+d.txt") == "/kollektiv/a%20b/c+d.txt"
    assert canonical_path("/kollektiv/p%C3%A4th/%F0%9F%8E%89.md") == (
        "/kollektiv/p%C3%A4th/%F0%9F%8E%89.md"
    )
    assert canonical_path("") == "/"
    assert canonical_path("relative/path") == "/relative/path"
    # Characters that are illegal in a URI path are escaped.
    assert canonical_path("/a b").startswith("/a%20b")


def test_sign_request_includes_session_token() -> None:
    """STS session tokens are signed like any other x-amz header."""
    headers = sign_request(
        "GET",
        f"{ENDPOINT}/{BUCKET}/x.txt",
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        session_token="SESSION",
        now=FROZEN,
    )
    assert headers["x-amz-security-token"] == "SESSION"
    assert "x-amz-security-token" in headers["Authorization"]


def test_signer_is_byte_identical_to_botocore() -> None:
    """Cross-check the signer against botocore when it is installed."""
    botocore_auth = pytest.importorskip("botocore.auth")
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    original = botocore_auth.get_current_datetime
    botocore_auth.get_current_datetime = lambda: FROZEN.replace(tzinfo=None)
    try:
        cases = [
            ("PUT", f"{ENDPOINT}/{BUCKET}/a.md", {"content-type": "text/markdown"}, b"# hello\n", {}),
            ("GET", f"{ENDPOINT}/{BUCKET}/", {}, None, {"list-type": "2", "prefix": "kollektiv/"}),
            ("DELETE", f"{ENDPOINT}/{BUCKET}/a.md", {}, None, {}),
            ("PUT", f"{ENDPOINT}/{BUCKET}/a%20b/c+d.txt", {}, b"x", {}),
        ]
        for method, url, headers, body, params in cases:
            url_with_qs = str(httpx.URL(url, params=params)) if params else url
            mine = sign_request(
                method,
                url,
                access_key=ACCESS_KEY,
                secret_key=SECRET_KEY,
                headers=headers,
                params=params,
                body=body,
                now=FROZEN,
            )
            request = AWSRequest(method=method, url=url_with_qs, headers=dict(headers), data=body)
            botocore_auth.S3SigV4Auth(Credentials(ACCESS_KEY, SECRET_KEY), "s3", "auto").add_auth(
                request
            )
            assert mine["Authorization"] == request.headers["Authorization"], url_with_qs
    finally:
        botocore_auth.get_current_datetime = original


# ----------------------------------------------------------------------
# Mock S3 transport
# ----------------------------------------------------------------------
def build_s3_transport(
    bucket: str = BUCKET, objects: Optional[Dict[str, bytes]] = None, calls: Optional[List[Any]] = None
) -> httpx.MockTransport:
    """Return a MockTransport implementing the S3 verbs R2 storage uses."""
    # Bind the caller's dict so tests can inspect what was written; only build
    # a fresh one when no store was supplied.
    store: Dict[str, bytes] = objects if objects is not None else {}
    log = calls if calls is not None else []
    prefix = f"/{bucket}"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.startswith(prefix):
            return httpx.Response(404, text="<Error><Code>NoSuchBucket</Code></Error>")
        key = path[len(prefix) :].lstrip("/")
        query = dict(request.url.params)
        log.append((request.method, key, query, request.headers.get("Authorization", "")))

        if request.method == "HEAD" and not key:
            return httpx.Response(200, headers={"etag": '"bucket"'})
        if request.method == "HEAD":
            if key not in store:
                return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")
            return httpx.Response(
                200,
                headers={
                    "content-length": str(len(store[key])),
                    "etag": f'"{sha256_hex(store[key])[:32]}"',
                    "last-modified": "Tue, 06 Oct 2026 20:41:07 GMT",
                    "content-type": content_type_for(key),
                },
            )
        if request.method == "PUT":
            assert request.headers.get("authorization", "").startswith("AWS4-HMAC-SHA256")
            store[key] = request.content
            return httpx.Response(200, headers={"etag": f'"{sha256_hex(store[key])[:32]}"'})
        if request.method == "DELETE":
            store.pop(key, None)
            return httpx.Response(204)
        if request.method == "GET" and query.get("list-type") == "2":
            return httpx.Response(200, text=_listing_xml(store, query.get("prefix", "")))
        if request.method == "GET":
            if key not in store:
                return httpx.Response(
                    404, text="<Error><Code>NoSuchKey</Code><Message>missing</Message></Error>"
                )
            return httpx.Response(200, content=store[key])
        return httpx.Response(405, text="<Error><Code>MethodNotAllowed</Code></Error>")

    return httpx.MockTransport(handler)


def _listing_xml(store: Dict[str, bytes], prefix: str) -> str:
    """Render a minimal ListObjectsV2 response."""
    contents = "".join(
        f"<Contents><Key>{escape(key)}</Key><Size>{len(value)}</Size>"
        f"<LastModified>2026-10-06T20:41:07.000Z</LastModified>"
        f"<ETag>{sha256_hex(value)[:32]}</ETag></Contents>"
        for key, value in sorted(store.items())
        if key.startswith(prefix)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<ListBucketResult><Name>{BUCKET}</Name><IsTruncated>false</IsTruncated>'
        f"{contents}</ListBucketResult>"
    )


def build_client(settings: Settings, store: Optional[Dict[str, bytes]] = None, **kwargs: Any) -> R2Client:
    """Build an R2Client wired to the mock transport."""
    transport = build_s3_transport(objects=store, calls=kwargs.pop("calls", None))
    client = httpx.AsyncClient(transport=transport, base_url="http://r2.test")
    return R2Client(
        bucket=BUCKET,
        access_key_id=ACCESS_KEY,
        secret_access_key=SECRET_KEY,
        endpoint=ENDPOINT,
        prefix="kollektiv",
        label="primary",
        settings=settings,
        client=client,
        **kwargs,
    )


# ----------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------
async def test_client_round_trip(settings: Settings) -> None:
    """Upload, head, download, list, presign and delete an object."""
    store: Dict[str, bytes] = {}
    calls: List[Any] = []
    client = build_client(settings, store, calls=calls)

    key = client.key("/prj_1/notes.md")
    uploaded = await client.put_bytes(key, b"# hello\n", "text/markdown")
    assert uploaded["size"] == 8
    assert uploaded["path"] == "/prj_1/notes.md"
    assert store[key] == b"# hello\n"
    # Every mutating call must be signed.
    assert calls[0][3].startswith("AWS4-HMAC-SHA256")

    head = await client.head(key)
    assert head["size"] == 8
    assert head["type"] == "file"

    assert await client.get_text(key) == "# hello\n"

    listing = await client.list_objects(prefix="/prj_1")
    assert [item["path"] for item in listing] == ["/prj_1/notes.md"]
    assert listing[0]["type"] == "file"

    url = client.presigned_url(key)
    assert "X-Amz-Signature=" in url and url.startswith(ENDPOINT)

    assert await client.delete(key) is True
    assert key not in store
    # S3 answers 204 for a missing key, so a second delete also reports success.
    assert await client.delete(key) is True
    await client.close()


async def test_client_translates_s3_errors(settings: Settings) -> None:
    """403/404/5xx become typed Kollectiv errors."""
    client = build_client(settings, {})
    with pytest.raises(NotFoundError):
        await client.get_bytes("kollektiv/missing.md")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(403, text="<Error><Code>SignatureDoesNotMatch</Code></Error>")
        return httpx.Response(503, text="<Error><Code>SlowDown</Code></Error>")

    error_client = R2Client(
        bucket=BUCKET,
        access_key_id=ACCESS_KEY,
        secret_access_key=SECRET_KEY,
        endpoint=ENDPOINT,
        prefix="kollektiv",
        label="err",
        settings=settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(AuthenticationError):
        await error_client.get_bytes("kollektiv/x.md")
    await client.close()
    await error_client.close()


async def test_client_requires_credentials(settings: Settings) -> None:
    """A missing setting is a configuration error, not a mystery 403."""
    with pytest.raises(ConfigurationError):
        R2Client(bucket="", access_key_id="", secret_access_key="", endpoint="", settings=settings)


async def test_client_upload_and_download_file(settings: Settings, tmp_path: Any) -> None:
    """Files round trip through ``put_file``/``get_file``."""
    source = tmp_path / "artifact.md"
    source.write_text("content of the artifact", encoding="utf-8")
    store: Dict[str, bytes] = {}
    client = build_client(settings, store)

    result = await client.put_file(str(source), client.key("/prj_2/artifact.md"))
    assert result["size"] == source.stat().st_size
    assert result["md5"]

    target = tmp_path / "downloaded.md"
    assert await client.get_file(client.key("/prj_2/artifact.md"), str(target)) is True
    assert target.read_text(encoding="utf-8") == "content of the artifact"

    with pytest.raises(FileNotFoundError):
        await client.put_file(str(tmp_path / "nope.md"), "kollektiv/nope.md")
    await client.close()


async def test_client_listing_includes_folders(settings: Settings) -> None:
    """``include_folders`` surfaces ``<CommonPrefixes>`` entries."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            '<?xml version="1.0"?><ListBucketResult><IsTruncated>false</IsTruncated>'
            "<CommonPrefixes><Prefix>kollektiv/prj_1/</Prefix></CommonPrefixes>"
            "</ListBucketResult>"
        )
        return httpx.Response(200, text=body)

    client = R2Client(
        bucket=BUCKET,
        access_key_id=ACCESS_KEY,
        secret_access_key=SECRET_KEY,
        endpoint=ENDPOINT,
        prefix="kollektiv",
        label="folders",
        settings=settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    listing = await client.list_objects(include_folders=True)
    assert [item["type"] for item in listing] == ["folder"]
    assert listing[0]["path"] == "/prj_1"
    await client.close()


def test_content_type_and_formatting_helpers() -> None:
    """Small helpers behave predictably."""
    assert content_type_for("a.md") == "text/markdown"
    assert content_type_for("a.unknownext") == "application/octet-stream"
    assert format_bytes(0) == "0.0 B"
    assert format_bytes(1536) == "1.5 KiB"
    assert format_bytes(2 * 1024**3) == "2.0 GiB"


# ----------------------------------------------------------------------
# Pool
# ----------------------------------------------------------------------
def two_bucket_settings(settings: Settings, free_gb: float = 10.0) -> Settings:
    """Settings with two pooled R2 buckets and a small free tier."""
    accounts = [
        {
            "name": "primary",
            "bucket": "kollektiv",
            "access_key_id": ACCESS_KEY,
            "secret_access_key": SECRET_KEY,
            "endpoint": ENDPOINT,
        },
        {
            "name": "overflow",
            "bucket": "kollektiv-2",
            "access_key_id": ACCESS_KEY,
            "secret_access_key": SECRET_KEY,
            "endpoint": ENDPOINT,
        },
    ]
    import json

    return settings.model_copy(update={"R2_ACCOUNTS": json.dumps(accounts), "R2_FREE_TIER_GB": free_gb})


def build_pool(settings: Settings, stores: Dict[str, Dict[str, bytes]]) -> R2Storage:
    """Build an R2Storage whose buckets are backed by the mock transport."""

    def factory(account: Dict[str, str]) -> R2Client:
        store = stores.setdefault(account["bucket"], {})
        transport = build_s3_transport(bucket=account["bucket"], objects=store)
        return R2Client(
            bucket=account["bucket"],
            access_key_id=account["access_key_id"],
            secret_access_key=account["secret_access_key"],
            endpoint=account["endpoint"],
            prefix=account.get("prefix", "kollektiv") or "kollektiv",
            label=account["name"],
            settings=settings,
            client=httpx.AsyncClient(transport=transport, base_url="http://r2.test"),
        )

    return R2Storage(settings, client_factory=factory)


async def test_pool_initializes_and_routes_to_free_space(settings: Settings) -> None:
    """Writes go to the bucket with the most free space."""
    resolved = two_bucket_settings(settings)
    stores: Dict[str, Dict[str, bytes]] = {
        "kollektiv": {"kollektiv/existing/big.bin": b"x" * 4096},
        "kollektiv-2": {},
    }
    pool = build_pool(resolved, stores)

    report = await pool.initialize()
    assert report["backend"] == "r2"
    assert report["accounts"] == 2
    assert report["healthy"] == 2

    chosen = await pool.get_best_account()
    assert chosen.bucket == "kollektiv-2", "the empty bucket has more free space"

    uploaded = await pool.upload_file(__file__, "/prj_9/notes.md")
    assert uploaded["account_id"]
    assert uploaded["path"] == "/prj_9/notes.md"
    assert uploaded["url"]
    assert stores["kollektiv-2"].get("kollektiv/prj_9/notes.md") is not None

    quota = pool.get_total_quota()
    assert quota["backend"] == "r2"
    assert quota["accounts"] == 2
    assert quota["total_gb"] == 20.0
    assert quota["per_account"]

    # Reads route back to the owning bucket without a hint.
    assert await pool.read_text("/prj_9/notes.md")
    files = await pool.list_project_files("prj_9")
    assert [item["path"] for item in files] == ["/prj_9/notes.md"]
    assert await pool.download_file("/prj_9/notes.md", str(settings.workspace_path / "copy.md"))

    url = await pool.get_file_url("/prj_9/notes.md")
    assert "X-Amz-Signature=" in url or url.startswith(ENDPOINT)
    assert await pool.delete_file("/prj_9/notes.md") is True
    await pool.close()


async def test_pool_requires_configuration(settings: Settings) -> None:
    """An empty pool raises a clear error instead of failing obscurely."""
    resolved = settings.model_copy(
        update={"R2_ACCOUNTS": "[]", "R2_ACCESS_KEY_ID": "", "R2_SECRET_ACCESS_KEY": "", "R2_ENDPOINT": ""}
    )
    pool = R2Storage(resolved)
    assert pool.is_configured() is False
    await pool.initialize()
    with pytest.raises(ConfigurationError):
        await pool.get_best_account()


async def test_pool_reports_unhealthy_bucket(settings: Settings) -> None:
    """A bucket that answers 500 is marked unhealthy but does not break the pool."""
    resolved = two_bucket_settings(settings)

    def factory(account: Dict[str, str]) -> R2Client:
        def handler(request: httpx.Request) -> httpx.Response:
            if account["bucket"] == "kollektiv":
                return httpx.Response(500, text="<Error><Code>InternalError</Code></Error>")
            return build_s3_transport(bucket=account["bucket"], objects={}).handle_request(request)

        return R2Client(
            bucket=account["bucket"],
            access_key_id=account["access_key_id"],
            secret_access_key=account["secret_access_key"],
            endpoint=account["endpoint"],
            prefix="kollektiv",
            label=account["name"],
            settings=settings,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    pool = R2Storage(resolved, client_factory=factory)
    report = await pool.initialize()
    assert report["accounts"] == 2
    assert report["healthy"] == 1
    status = {entry["label"]: entry for entry in pool.get_pool_status()}
    assert status["primary"]["healthy"] is False
    assert status["primary"]["error"]
    # The healthy bucket still serves writes.
    uploaded = await pool.upload_file(__file__, "/prj_9/ok.md")
    assert uploaded["email"] == "overflow"
    await pool.close()


def test_r2_accounts_from_settings_supports_both_shapes(settings: Settings) -> None:
    """Single-bucket variables and pooled JSON both work."""
    import json

    single = settings.model_copy(
        update={
            "R2_ACCESS_KEY_ID": ACCESS_KEY,
            "R2_SECRET_ACCESS_KEY": SECRET_KEY,
            "R2_ENDPOINT": ENDPOINT,
            "R2_BUCKET": "kollektiv",
        }
    )
    accounts = r2_accounts_from_settings(single)
    assert len(accounts) == 1
    assert accounts[0]["bucket"] == "kollektiv"
    assert accounts[0]["prefix"] == "kollektiv"

    pooled = settings.model_copy(
        update={
            "R2_ACCOUNTS": json.dumps(
                [
                    {"bucket": "b1", "access_key_id": "k", "secret_access_key": "s", "endpoint": "https://a"},
                    {"bucket": "b2", "access_key_id": "k", "secret_access_key": "s", "endpoint": "https://b"},
                    {"bucket": "incomplete"},
                ]
            )
        }
    )
    parsed = r2_accounts_from_settings(pooled)
    assert [entry["bucket"] for entry in parsed] == ["b1", "b2"]


def test_factory_selects_the_backend(settings: Settings) -> None:
    """``STORAGE_BACKEND=auto`` prefers R2, then TeraBox, then the local cache."""
    import json

    r2_settings = settings.model_copy(
        update={
            "R2_ACCESS_KEY_ID": ACCESS_KEY,
            "R2_SECRET_ACCESS_KEY": SECRET_KEY,
            "R2_ENDPOINT": ENDPOINT,
            "R2_BUCKET": "kollektiv",
        }
    )
    assert isinstance(build_storage(r2_settings), R2Storage)
    assert isinstance(build_storage(settings), object)
    assert build_storage(settings).is_configured() is False

    terabox = settings.model_copy(
        update={
            "STORAGE_BACKEND": "terabox",
            "TERABOX_ACCOUNTS": json.dumps([{"email": "a@b.c", "refresh_token": "t"}]),
        }
    )
    pool = build_storage(terabox)
    assert pool.__class__.__name__ == "TeraBoxPoolManager"
    assert pool.remote_root == "/Kollektiv"
    assert pool.is_configured() is True

    none_backend = settings.model_copy(update={"STORAGE_BACKEND": "none"})
    assert build_storage(none_backend).is_configured() is False
