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
