"""The sponsor line: validation, rotation, the off-by-default switch, and the route."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, cast

import httpx
import pytest

from src.api.auth import is_public_path
from src.api.routes import create_app
from src.sponsors import LABEL, current_line, load_catalog, validate_entry
from tests.test_api import StubOrchestrator

GOOD: Dict[str, Any] = {
    "id": "acme-cloud",
    "sponsor": "Acme Cloud",
    "text": "Host your side projects for free this month.",
    "url": "https://acme.example/offer",
}


def _entry(**changes: Any) -> Dict[str, Any]:
    entry = dict(GOOD)
    entry.update(changes)
    return entry


def test_a_good_entry_is_accepted() -> None:
    line, reason = validate_entry(GOOD)
    assert reason == "" and line is not None
    assert line.as_dict()["label"] == LABEL


@pytest.mark.parametrize(
    "change",
    [
        {"url": "http://acme.example/offer"},  # not https
        {"url": "https://user:pw@acme.example/offer"},  # credentials in the link
        {"url": "javascript:alert(1)"},
        {"text": "Error: rate limit reached, upgrade now"},  # reads like an error
        {"text": "Warning: your usage is high"},
        {"text": "Kollektiv recommends this"},  # imitates the tool
        {"text": "line one\nline two"},  # must be one line
        {"text": "x" * 141},
        {"id": "Bad Id!"},
        {"sponsor": ""},
    ],
)
def test_unsafe_or_misleading_entries_are_refused(change: Dict[str, Any]) -> None:
    line, reason = validate_entry(_entry(**change))
    assert line is None
    assert reason


def test_inactive_entries_are_skipped_without_complaint() -> None:
    assert validate_entry(_entry(active=False)) == (None, "inactive")


def test_catalogue_file_skips_bad_entries_and_keeps_good_ones(tmp_path: Path) -> None:
    path = tmp_path / "sponsors.json"
    path.write_text(
        json.dumps({"sponsors": [GOOD, _entry(id="bad", url="http://x.example"), _entry(id="acme-cloud")]}),
        encoding="utf-8",
    )
    lines = load_catalog(str(path))
    assert [line.id for line in lines] == ["acme-cloud"]


def test_missing_or_broken_catalogue_gives_no_lines(tmp_path: Path) -> None:
    assert load_catalog("") == []
    assert load_catalog(str(tmp_path / "missing.json")) == []
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_catalog(str(broken)) == []


def test_off_by_default_so_nothing_shows_without_opt_in() -> None:
    settings = SimpleNamespace(SPONSORS_ENABLED=False, SPONSORS_FILE="", SPONSOR_ROTATE_SECONDS=300)
    assert current_line(settings, catalog=[]) is None
    assert current_line(SimpleNamespace(SPONSORS_ENABLED=False), catalog=[]) is None


def test_rotation_depends_only_on_the_clock_not_on_the_viewer() -> None:
    lines = [validate_entry(_entry(id=f"s-{i}"))[0] for i in range(3)]
    settings = SimpleNamespace(SPONSORS_ENABLED=True, SPONSORS_FILE="", SPONSOR_ROTATE_SECONDS=300)
    first = current_line(settings, now=10_000, catalog=cast(List[Any], lines))
    same_window = current_line(settings, now=10_100, catalog=cast(List[Any], lines))
    next_window = current_line(settings, now=10_300, catalog=cast(List[Any], lines))
    assert first == same_window  # every viewer in this window sees the same line
    assert first is not None and next_window is not None
    assert first["id"] != next_window["id"]
    assert first["label"] == LABEL


def test_the_sponsor_endpoint_is_public() -> None:
    assert is_public_path("/sponsors/line")
    assert is_public_path("/sponsors/line/")
    assert not is_public_path("/sponsors/other")


@pytest.mark.asyncio
async def test_route_returns_null_when_disabled(settings: Any) -> None:
    app = create_app(settings=settings, orchestrator=cast(Any, StubOrchestrator(settings)))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        response = await c.get("/sponsors/line")
    assert response.status_code == 200
    assert response.json() == {"line": None}
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_route_returns_the_line_when_enabled(settings: Any, tmp_path: Path) -> None:
    path = tmp_path / "sponsors.json"
    path.write_text(json.dumps([GOOD]), encoding="utf-8")
    enabled = settings.model_copy(update={"SPONSORS_ENABLED": True, "SPONSORS_FILE": str(path)}, deep=True)
    app = create_app(settings=enabled, orchestrator=cast(Any, StubOrchestrator(enabled)))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        response = await c.get("/sponsors/line")
    body = response.json()
    assert body["line"]["label"] == LABEL
    assert body["line"]["url"] == GOOD["url"]
