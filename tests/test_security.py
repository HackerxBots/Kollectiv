"""Unit tests for the write rate limiter and the upload size cap."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from config.settings import Settings
from src.api.ratelimit import WriteRateLimiter
from src.orchestrator.app import Orchestrator


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_rate_limiter_refuses_a_flood_and_recovers_after_a_minute() -> None:
    clock = _Clock()
    limiter = WriteRateLimiter(per_minute=3, clock=clock)
    assert [limiter.allow("1.2.3.4") for _ in range(3)] == [True, True, True]
    assert limiter.allow("1.2.3.4") is False
    assert limiter.allow("5.6.7.8") is True, "limits are per client"
    clock.now = 61.0
    assert limiter.allow("1.2.3.4") is True


def test_rate_limiter_zero_disables_the_limit() -> None:
    limiter = WriteRateLimiter(per_minute=0)
    assert all(limiter.allow("x") for _ in range(1000))


def test_upload_refuses_files_over_the_size_cap(tmp_path: Path) -> None:
    """A file inside the workspace but over MAX_UPLOAD_MB is refused before anything is sent."""
    settings = Settings(WORKSPACE_DIR=str(tmp_path / "workspace"), MAX_UPLOAD_MB=0)
    settings.workspace_path.mkdir(parents=True, exist_ok=True)
    big = settings.workspace_path / "big.bin"
    big.write_bytes(b"x")
    fake: Any = SimpleNamespace(settings=settings, pool=None)
    try:
        asyncio.run(Orchestrator.upload_project_file(fake, "prj_test", str(big)))
    except ValueError as exc:
        assert "MAX_UPLOAD_MB" in str(exc)
    else:
        raise AssertionError("an oversized upload was not refused")


def test_stream_ticket_expires_after_its_ttl() -> None:
    from src.api.auth import StreamTicketStore

    clock = _Clock()
    store = StreamTicketStore(ttl=60, clock=clock)
    ticket = store.issue("user_alice")
    clock.now = 59.0
    assert store.redeem(ticket) == "user_alice"
    clock.now = 60.0
    assert store.redeem(ticket) is None
    assert store.redeem("never-issued") is None


def test_stream_path_matcher_is_strict() -> None:
    from src.api.auth import is_stream_path

    assert is_stream_path("/projects/prj_1/events/stream")
    assert not is_stream_path("/projects/prj_1/status")
    assert not is_stream_path("/projects//events/stream/extra")


def test_stream_ticket_works_only_once() -> None:
    """A copied ticket cannot open a second stream."""
    from src.api.auth import StreamTicketStore

    store = StreamTicketStore(ttl=60, clock=_Clock())
    ticket = store.issue("user_alice")
    assert store.redeem(ticket) == "user_alice"
    assert store.redeem(ticket) is None


def test_access_log_lines_never_keep_a_stream_ticket() -> None:
    """The uvicorn access log is redacted before it is written."""
    import logging

    from src.api.auth import StreamTicketRedactor

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5000", "GET", "/projects/prj_1/events/stream?ticket=abc123&x=1", "1.1", 200),
        None,
    )
    assert StreamTicketRedactor().filter(record) is True
    line = record.getMessage()
    assert "abc123" not in line
    assert "ticket=REDACTED&x=1" in line


def test_mcp_refuses_unauthenticated_remote_binds() -> None:
    """The standalone MCP server has no auth, so it only listens on loopback unless told otherwise."""
    from src.api.mcp_server import remote_exposure_refused

    assert remote_exposure_refused("sse", "0.0.0.0", False) is True
    assert remote_exposure_refused("streamable-http", "10.0.0.5", False) is True
    assert remote_exposure_refused("sse", "0.0.0.0", True) is False
    assert remote_exposure_refused("sse", "127.0.0.1", False) is False
    assert remote_exposure_refused("sse", "localhost", False) is False
    assert remote_exposure_refused("stdio", "0.0.0.0", False) is False


def test_mcp_main_exits_before_serving_a_remote_bind(monkeypatch: Any) -> None:
    """main() returns 2 without building the server when the bind is refused."""
    from src.api import mcp_server

    built: list[bool] = []
    monkeypatch.setattr(mcp_server, "create_server", lambda *a, **k: built.append(True))
    monkeypatch.setattr(mcp_server, "get_settings", lambda: _settings_with(MCP_ALLOW_UNAUTHENTICATED_REMOTE=False))
    assert mcp_server.main(["--transport", "sse", "--host", "0.0.0.0"]) == 2
    assert built == []


def _settings_with(**overrides: Any) -> Any:
    from config.settings import Settings

    return Settings(**overrides)
