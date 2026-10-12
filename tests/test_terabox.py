"""Tests for the TeraBox storage layer.

Covers OAuth (initial access token, refresh, expiry), the four-step sharded
upload, streaming download, delete, quota and the multi-account pool routing.
All HTTP is mocked with ``httpx.MockTransport`` -- no network access.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest

from src.storage.pool_manager import TeraBoxPoolManager
from src.storage.terabox_client import TeraBoxClient
from src.utils.errors import AuthenticationError, ConfigurationError, TeraBoxError


# ----------------------------------------------------------------------
# Transport fixtures
# ----------------------------------------------------------------------
def build_transport(router: Any, upload_bytes: Dict[str, bytes] | None = None) -> httpx.MockTransport:
    """Build a transport simulating the TeraBox Open Platform surface."""
    blobs = upload_bytes or {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = dict(request.url.params)

        if path.endswith("/oauth/2.0/token"):
            grant = query.get("grant_type")
            if grant == "refresh_token" and query.get("refresh_token"):
                return httpx.Response(
                    200,
                    json={
                        "access_token": "fresh-access-token",
                        "refresh_token": "rotated-refresh-token",
                        "expires_in": 172800,
                        "scope": "basic",
                    },
                )
            if grant == "client_credentials":
                return httpx.Response(200, json={"access_token": "app-token", "expires_in": 2592000})
            return httpx.Response(200, json={"error": "invalid_grant"})

        if path.endswith("/xpan/nas"):
            return httpx.Response(200, json={"total": 10 * 1024**3, "used": 2 * 1024**3})

        if path.endswith("/xpan/multimedia"):
            targets = json.loads(query.get("targets", "[]"))
            known = set(blobs) | {"/Kollektiv/PROJECT_STATE.md"}
            if targets and targets[0] in known:
                return httpx.Response(
                    200, json={"errno": 0, "info": [{"path": targets[0], "dlink": f"https://dl.example{targets[0]}"}]}
                )
            return httpx.Response(200, json={"errno": -9, "errmsg": "file not found"})

        if path.endswith("/xpan/file"):
            method = query.get("method")
            if method == "list":
                return httpx.Response(
                    200,
                    json={
                        "errno": 0,
                        "list": [
                            {
                                "server_filename": "app.py",
                                "path": "/Kollektiv/app.py",
                                "size": 42,
                                "isdir": 0,
                                "fs_id": 1,
                                "md5": "d41d8cd98f00b204e9800998ecf8427e",
                            },
                            {
                                "server_filename": "artifacts",
                                "path": "/Kollektiv/artifacts",
                                "size": 0,
                                "isdir": 1,
                                "fs_id": 2,
                            },
                        ],
                    },
                )
            if method == "create":
                return httpx.Response(200, json={"errno": 0, "path": query.get("path")})
            if method == "precreate":
                return httpx.Response(200, json={"errno": 0, "uploadid": "upload-123", "block_list": []})
            if method == "upload":
                return httpx.Response(200, json={"errno": 0, "md5": "abc"})
            if method == "merge":
                return httpx.Response(200, json={"errno": 0})
            if method == "delete":
                return httpx.Response(200, json={"errno": 0})
        return httpx.Response(404, json={"errno": -1, "errmsg": f"unmocked {path}"})

    return httpx.MockTransport(handler)


def build_client(settings: Any, account: Dict[str, str], transport: httpx.MockTransport) -> TeraBoxClient:
    """Create a client bound to a mock transport."""
    client = httpx.AsyncClient(base_url=settings.TERABOX_BASE_URL, transport=transport)
    return TeraBoxClient(account, settings=settings, client=client)


@pytest.fixture()
def download_payload() -> Dict[str, bytes]:
    """The blob served for ``/Kollektiv/PROJECT_STATE.md``."""
    return {"/Kollektiv/PROJECT_STATE.md": b"# PROJECT_STATE\n\nhello from TeraBox\n"}


# ----------------------------------------------------------------------
# Authentication
# ----------------------------------------------------------------------
async def test_authenticate_with_refresh_token(settings: Any) -> None:
    """A refresh token is exchanged for an access token and persisted."""
    account = {"email": "box@example.com", "refresh_token": "rt-1"}
    client = build_client(settings, account, build_transport(None))
    try:
        token = await client.authenticate()
        assert token == "fresh-access-token"
        assert client.refresh_token_value == "rotated-refresh-token"
        assert client.token_expires_at > time.time()
    finally:
        await client.close()


async def test_authenticate_reuses_valid_token(settings: Any) -> None:
    """A still-valid access token is reused without hitting the OAuth endpoint."""
    account = {"email": "box@example.com", "access_token": "cached-token", "refresh_token": "rt-1"}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"access_token": "should-not-be-used"})

    client = build_client(settings, account, httpx.MockTransport(handler))
    try:
        assert await client.authenticate() == "cached-token"
        assert calls == []
    finally:
        await client.close()


async def test_authenticate_without_credentials_raises(settings: Any) -> None:
    """No tokens and no app credentials is a clear configuration error."""
    client = build_client(settings, {"email": "box@example.com"}, build_transport(None))
    try:
        with pytest.raises(AuthenticationError):
            await client.authenticate()
    finally:
        await client.close()


async def test_client_credentials_grant(settings: Any) -> None:
    """App credentials produce a token when no user token exists."""
    app_settings = settings.model_copy(update={"TERABOX_APP_ID": "app-id", "TERABOX_APP_KEY": "app-key"}, deep=True)
    client = build_client(app_settings, {"email": "box@example.com"}, build_transport(None))
    try:
        token = await client.authenticate()
        assert token == "app-token"
    finally:
        await client.close()


async def test_ensure_token_refreshes_expired_token(settings: Any) -> None:
    """An expired token triggers a refresh on the next call."""
    account = {"email": "box@example.com", "access_token": "stale", "refresh_token": "rt-1"}
    client = build_client(settings, account, build_transport(None))
    try:
        client.token_expires_at = time.time() - 10
        assert await client.ensure_token() == "fresh-access-token"
    finally:
        await client.close()


# ----------------------------------------------------------------------
# Files
# ----------------------------------------------------------------------
async def test_list_files_returns_normalised_entries(settings: Any) -> None:
    """Directory listings are normalised into the documented shape."""
    client = build_client(settings, {"email": "b@e.com", "access_token": "t"}, build_transport(None))
    try:
        entries = await client.list_files("/Kollektiv")
        assert [entry["name"] for entry in entries] == ["app.py", "artifacts"]
        assert entries[0]["type"] == "file"
        assert entries[0]["size"] == 42
        assert entries[1]["type"] == "folder" and entries[1]["isdir"] is True
    finally:
        await client.close()


async def test_create_folder_is_recursive(settings: Any) -> None:
    """Nested folders are created level by level (path travels in the body)."""
    created: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode() if request.content else ""
        params = dict(httpx.QueryParams(body)) if body else {}
        created.append(params.get("path", ""))
        return httpx.Response(200, json={"errno": 0})

    client = build_client(settings, {"email": "b@e.com", "access_token": "t"}, httpx.MockTransport(handler))
    try:
        assert await client.create_folder("/Kollektiv/prj_1/state") is True
        assert "/Kollektiv" in created
        assert "/Kollektiv/prj_1" in created
        assert created[-1] == "/Kollektiv/prj_1/state"
    finally:
        await client.close()


async def test_upload_file_runs_precreate_upload_merge(settings: Any, tmp_path: Path) -> None:
    """A small file goes through precreate -> upload -> merge."""
    source = tmp_path / "artifact.txt"
    payload = "kollektiv" * 10
    source.write_text(payload, encoding="utf-8")

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = dict(request.url.params).get("method", "")
        seen.append(method or request.url.path)
        if method == "precreate":
            return httpx.Response(200, json={"errno": 0, "uploadid": "u-1", "block_list": []})
        if method == "upload":
            return httpx.Response(200, json={"errno": 0})
        if method == "merge":
            return httpx.Response(200, json={"errno": 0})
        if method == "create":
            return httpx.Response(200, json={"errno": 0})
        if request.url.path.endswith("/xpan/file"):
            return httpx.Response(200, json={"errno": 0, "list": [{"server_filename": "artifact.txt",
                                                                     "path": "/Kollektiv/artifact.txt",
                                                                     "size": len(payload.encode()),
                                                                     "isdir": 0}]})
        if request.url.path.endswith("/xpan/multimedia"):
            return httpx.Response(200, json={"errno": 0, "info": [{"dlink": "https://dl.example/artifact.txt"}]})
        return httpx.Response(404, json={"errno": -1})

    client = build_client(settings, {"email": "b@e.com", "access_token": "t"}, httpx.MockTransport(handler))
    try:
        result = await client.upload_file(str(source), "/Kollektiv/artifact.txt")
        assert result["path"] == "/Kollektiv/artifact.txt"
        assert result["size"] == len(payload.encode())
        assert "precreate" in seen and "upload" in seen and "merge" in seen
        assert result["url"].startswith("https://dl.example/artifact.txt")
    finally:
        await client.close()


async def test_upload_file_missing_local_file(settings: Any) -> None:
    """Uploading a missing file raises FileNotFoundError."""
    client = build_client(settings, {"email": "b@e.com", "access_token": "t"}, build_transport(None))
    try:
        with pytest.raises(FileNotFoundError):
            await client.upload_file("/does/not/exist.bin", "/Kollektiv/x.bin")
    finally:
        await client.close()


async def test_upload_uses_block_md5_list(settings: Any, tmp_path: Path) -> None:
    """The precreate payload carries one MD5 per 4 MiB block."""
    source = tmp_path / "big.bin"
    source.write_bytes(b"a" * (4 * 1024 * 1024 + 5))
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        method = dict(request.url.params).get("method", "")
        if method == "precreate":
            captured.update(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, json={"errno": 0, "uploadid": "u", "block_list": []})
        if method == "upload":
            return httpx.Response(200, json={"errno": 0})
        if method == "merge":
            return httpx.Response(200, json={"errno": 0})
        if method == "create":
            return httpx.Response(200, json={"errno": 0})
        if request.url.path.endswith("/xpan/file"):
            return httpx.Response(200, json={"errno": 0, "list": []})
        return httpx.Response(200, json={"errno": 0, "info": [{"dlink": "https://dl/x"}]})

    client = build_client(settings, {"email": "b@e.com", "access_token": "t"}, httpx.MockTransport(handler))
    try:
        result = await client.upload_file(str(source), "/Kollektiv/big.bin", verify=False)
        blocks = json.loads(captured["block_list"])
        assert len(blocks) == 2
        assert all(len(block) == 32 for block in blocks)
        assert result["size"] == source.stat().st_size
    finally:
        await client.close()


async def test_download_file_streams_content(settings: Any, tmp_path: Path, download_payload: Dict[str, bytes]) -> None:
    """A remote file is streamed to a local path."""
    body = download_payload["/Kollektiv/PROJECT_STATE.md"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/xpan/multimedia"):
            return httpx.Response(200, json={"errno": 0, "info": [{"dlink": "https://dl.example/state.md"}]})
        if request.url.host == "dl.example":
            return httpx.Response(200, content=body)
        return httpx.Response(404, json={"errno": -1})

    client = build_client(settings, {"email": "b@e.com", "access_token": "t"}, httpx.MockTransport(handler))
    destination = tmp_path / "state" / "PROJECT_STATE.md"
    try:
        assert await client.download_file("/Kollektiv/PROJECT_STATE.md", str(destination)) is True
        assert destination.read_bytes() == body
    finally:
        await client.close()


async def test_download_missing_file_returns_false(settings: Any, tmp_path: Path) -> None:
    """A missing remote file is reported as a failure, not an exception."""
    client = build_client(settings, {"email": "b@e.com", "access_token": "t"}, build_transport(None))
    try:
        assert await client.download_file("/Kollektiv/missing.md", str(tmp_path / "missing.md")) is False
    finally:
        await client.close()


async def test_delete_and_quota(settings: Any) -> None:
    """Delete returns success and quota is reported in bytes."""
    client = build_client(settings, {"email": "b@e.com", "access_token": "t"}, build_transport(None))
    try:
        assert await client.delete_file("/Kollektiv/app.py") is True
        quota = await client.get_quota()
        assert quota.total == 10 * 1024**3
        assert quota.used == 2 * 1024**3
        assert quota.free == 8 * 1024**3
    finally:
        await client.close()


async def test_auth_error_code_clears_token(settings: Any) -> None:
    """An auth error code clears the token, refreshes it and retries the call."""

    state = {"attempts": 0, "refreshed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/2.0/token"):
            state["refreshed"] = True
            return httpx.Response(200, json={"access_token": "refreshed-token", "expires_in": 172800})
        state["attempts"] += 1
        if state["attempts"] == 1:
            return httpx.Response(200, json={"errno": -6, "errmsg": "invalid token"})
        return httpx.Response(200, json={"errno": 0, "list": []})

    client = build_client(
        settings, {"email": "b@e.com", "access_token": "t", "refresh_token": "r"}, httpx.MockTransport(handler)
    )
    try:
        assert await client.list_files("/") == []
        assert state["refreshed"] is True
        assert client.access_token == "refreshed-token"
    finally:
        await client.close()


async def test_write_and_read_text_roundtrip(settings: Any, tmp_path: Path) -> None:
    """Text helpers upload and fetch a document."""
    store: Dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        method = dict(request.url.params).get("method", "")
        if method in {"precreate", "upload", "merge", "create"}:
            return httpx.Response(200, json={"errno": 0, "uploadid": "u"})
        if method == "precreate":
            return httpx.Response(200, json={"errno": 0, "uploadid": "u"})
        if request.url.path.endswith("/xpan/file"):
            store["uploaded"] = request.content
            return httpx.Response(200, json={"errno": 0, "list": []})
        if request.url.path.endswith("/xpan/multimedia"):
            return httpx.Response(200, json={"errno": 0, "info": [{"dlink": "https://dl.example/doc.md"}]})
        if request.url.host == "dl.example":
            return httpx.Response(200, content=b"# doc\n")
        return httpx.Response(404, json={"errno": -1})

    client = build_client(settings, {"email": "b@e.com", "access_token": "t"}, httpx.MockTransport(handler))
    try:
        result = await client.write_text("/Kollektiv/doc.md", "# doc\n")
        assert result["path"] == "/Kollektiv/doc.md"
        assert await client.read_text("/Kollektiv/doc.md") == "# doc\n"
    finally:
        await client.close()


# ----------------------------------------------------------------------
# Pool manager
# ----------------------------------------------------------------------
async def test_pool_initialises_all_accounts(settings: Any, terabox_accounts: list) -> None:
    """Every pooled account is authenticated and its quota loaded."""
    pool = TeraBoxPoolManager(
        terabox_accounts,
        settings=settings,
        client_factory=lambda account, cfg: build_client(cfg, account, build_transport(None)),
    )
    try:
        report = await pool.initialize()
        assert report["accounts"] == 2
        assert report["healthy"] == 2
        quota = await pool.get_total_quota()
        assert quota["total_gb"] == pytest.approx(20.0, rel=0.01)
        assert quota["free_gb"] == pytest.approx(16.0, rel=0.01)
    finally:
        await pool.close()


async def test_pool_routes_upload_to_account_with_most_space(settings: Any, terabox_accounts: list, tmp_path: Path) -> None:
    """Uploads land on the account with the most free space.

    box1 has 9 GiB used (1 GiB free), box2 has 1 GiB used (9 GiB free), so the
    upload must be routed to box2.
    """
    quotas = {"box1@example.com": (10 * 1024**3, 9 * 1024**3), "box2@example.com": (10 * 1024**3, 1 * 1024**3)}

    def factory(account: Dict[str, Any], cfg: Any) -> TeraBoxClient:
        total, used = quotas[account["email"]]

        def handler(request: httpx.Request) -> httpx.Response:
            method = dict(request.url.params).get("method", "")
            if request.url.path.endswith("/xpan/nas"):
                return httpx.Response(200, json={"total": total, "used": used})
            if method in {"precreate", "upload", "merge", "create"}:
                return httpx.Response(200, json={"errno": 0, "uploadid": "u"})
            if request.url.path.endswith("/xpan/file"):
                return httpx.Response(200, json={"errno": 0, "list": []})
            if request.url.path.endswith("/xpan/multimedia"):
                return httpx.Response(200, json={"errno": 0, "info": [{"dlink": "https://dl/x"}]})
            return httpx.Response(404, json={"errno": -1})

        client = httpx.AsyncClient(base_url=cfg.TERABOX_BASE_URL, transport=httpx.MockTransport(handler))
        return TeraBoxClient(account, settings=cfg, client=client)

    pool = TeraBoxPoolManager(terabox_accounts, settings=settings, client_factory=factory)
    source = tmp_path / "f.txt"
    source.write_text("data", encoding="utf-8")
    try:
        await pool.initialize()
        result = await pool.upload_file(str(source), "/Kollektiv/f.txt")
        expected = hashlib.sha256(b"box2@example.com").hexdigest()[:16]
        assert result["account_id"] == expected
        state = pool.get_state(result["account_id"])
        assert state is not None and state.free_gb > 8.0  # the 9 GiB-free account was chosen
    finally:
        await pool.close()


async def test_pool_lists_and_downloads_across_accounts(settings: Any, terabox_accounts: list, tmp_path: Path) -> None:
    """Listing aggregates accounts and find/download locates the file."""
    files = {"/Kollektiv/only-on-box2.md": b"payload"}

    def factory(account: Dict[str, Any], cfg: Any) -> TeraBoxClient:
        def handler(request: httpx.Request) -> httpx.Response:
            method = dict(request.url.params).get("method", "")
            if request.url.path.endswith("/xpan/nas"):
                return httpx.Response(200, json={"total": 1024**3, "used": 0})
            if method == "list":
                if account["email"] == "box2@example.com":
                    return httpx.Response(
                        200,
                        json={
                            "errno": 0,
                            "list": [
                                {
                                    "server_filename": "only-on-box2.md",
                                    "path": "/Kollektiv/only-on-box2.md",
                                    "size": 7,
                                    "isdir": 0,
                                }
                            ],
                        },
                    )
                return httpx.Response(200, json={"errno": 0, "list": []})
            if request.url.path.endswith("/xpan/multimedia"):
                targets = json.loads(dict(request.url.params).get("targets", "[]"))
                if account["email"] == "box2@example.com" and targets and targets[0] in files:
                    return httpx.Response(200, json={"errno": 0, "info": [{"dlink": "https://dl.example/f.md"}]})
                return httpx.Response(200, json={"errno": -9, "errmsg": "not found"})
            if request.url.host == "dl.example":
                return httpx.Response(200, content=files["/Kollektiv/only-on-box2.md"])
            return httpx.Response(404, json={"errno": -1})

        client = httpx.AsyncClient(base_url=cfg.TERABOX_BASE_URL, transport=httpx.MockTransport(handler))
        return TeraBoxClient(account, settings=cfg, client=client)

    pool = TeraBoxPoolManager(terabox_accounts, settings=settings, client_factory=factory)
    try:
        await pool.initialize()
        listings = await pool.list_all_files("/Kollektiv")
        assert len(listings) == 1
        assert listings[0]["path"] == "/Kollektiv/only-on-box2.md"
        assert listings[0]["locations"] == [listings[0]["account_id"]]

        destination = tmp_path / "f.md"
        assert await pool.download_file("/Kollektiv/only-on-box2.md", str(destination)) is True
        assert destination.read_bytes() == b"payload"
    finally:
        await pool.close()


async def test_pool_rejects_when_empty(settings: Any) -> None:
    """An empty pool produces a clear configuration error."""
    pool = TeraBoxPoolManager([], settings=settings)
    try:
        await pool.initialize()
        assert pool.is_configured() is False
        with pytest.raises(ConfigurationError):
            await pool.get_best_account()
    finally:
        await pool.close()


async def test_pool_min_free_space_is_respected(settings: Any, tmp_path: Path) -> None:
    """A full account is skipped in favour of one with room."""
    accounts = [{"email": "full@example.com", "access_token": "t"}, {"email": "empty@example.com", "access_token": "t"}]
    quotas = {"full@example.com": (1024**3, 1024**3 - 10), "empty@example.com": (1024**3, 0)}

    def factory(account: Dict[str, Any], cfg: Any) -> TeraBoxClient:
        total, used = quotas[account["email"]]

        def handler(request: httpx.Request) -> httpx.Response:
            method = dict(request.url.params).get("method", "")
            if request.url.path.endswith("/xpan/nas"):
                return httpx.Response(200, json={"total": total, "used": used})
            if method in {"precreate", "upload", "merge", "create"}:
                return httpx.Response(200, json={"errno": 0, "uploadid": "u"})
            if request.url.path.endswith("/xpan/file"):
                return httpx.Response(200, json={"errno": 0, "list": []})
            return httpx.Response(200, json={"errno": 0, "info": [{"dlink": "https://dl/x"}]})

        client = httpx.AsyncClient(base_url=cfg.TERABOX_BASE_URL, transport=httpx.MockTransport(handler))
        return TeraBoxClient(account, settings=cfg, client=client)

    pool = TeraBoxPoolManager(accounts, settings=settings, client_factory=factory, min_free_bytes=1024)
    source = tmp_path / "f.txt"
    source.write_text("data", encoding="utf-8")
    try:
        await pool.initialize()
        result = await pool.upload_file(str(source), "/Kollektiv/f.txt")
        # The account with only 10 bytes free must be skipped.
        expected = hashlib.sha256(b"empty@example.com").hexdigest()[:16]
        assert result["account_id"] == expected
    finally:
        await pool.close()


async def test_upload_surfaces_error_from_every_account(settings: Any, tmp_path: Path) -> None:
    """When all accounts fail the pool raises with per-account detail."""

    def factory(account: Dict[str, Any], cfg: Any) -> TeraBoxClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/xpan/nas"):
                return httpx.Response(200, json={"total": 1024**3, "used": 0})
            if request.url.path.endswith("/xpan/file"):
                return httpx.Response(200, json={"errno": -1, "errmsg": "quota exceeded"})
            return httpx.Response(404, json={"errno": -1})

        client = httpx.AsyncClient(base_url=cfg.TERABOX_BASE_URL, transport=httpx.MockTransport(handler))
        return TeraBoxClient(account, settings=cfg, client=client)

    pool = TeraBoxPoolManager([{"email": "a@e.com", "access_token": "t"}], settings=settings, client_factory=factory)
    source = tmp_path / "f.txt"
    source.write_text("data", encoding="utf-8")
    try:
        await pool.initialize()
        with pytest.raises(TeraBoxError) as excinfo:
            await pool.upload_file(str(source), "/Kollektiv/f.txt")
        assert "every account" in str(excinfo.value)
    finally:
        await pool.close()
