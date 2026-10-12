"""The sponsor line: validation, per-click campaigns, caps, counting, redirects and the ledger."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, cast

import httpx
import pytest

from src.api.auth import is_public_path
from src.api.routes import create_app
from src.sponsors import LABEL, current_line, go_target, ledger, load_catalog, validate_entry
from tests.test_api import StubOrchestrator

GOOD: Dict[str, Any] = {
    "id": "acme-cloud",
    "sponsor": "Acme Cloud",
    "text": "Host your side projects for free this month.",
    "url": "https://acme.example/offer",
    "cpc_cents": 25,
    "cap_clicks": 0,
}


def _entry(**changes: Any) -> Dict[str, Any]:
    entry = dict(GOOD)
    entry.update(changes)
    return entry


def _settings(path: str = "", enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(SPONSORS_ENABLED=enabled, SPONSORS_FILE=path, SPONSOR_ROTATE_SECONDS=300)


def test_a_good_entry_is_accepted_with_its_price() -> None:
    line, reason = validate_entry(GOOD)
    assert reason == "" and line is not None
    assert line.cpc_cents == 25 and line.cap_clicks == 0


@pytest.mark.parametrize(
    "change",
    [
        {"url": "http://acme.example/offer"},  # not https
        {"url": "https://acme.example@evil.example/offer"},  # user info in the link
        {"url": "javascript:alert(1)"},
        {"text": "Error: rate limit reached, upgrade now"},  # reads like an error
        {"text": "Warning: your usage is high"},
        {"text": "Kollektiv recommends this"},  # imitates the tool
        {"text": "line one\nline two"},  # must be one line
        {"text": "x" * 141},
        {"id": "Bad Id!"},
        {"sponsor": ""},
        {"cpc_cents": -1},
        {"cpc_cents": "25"},  # a string is not a price
        {"cpc_cents": True},  # a bool is not a price
        {"cap_clicks": 10_000_001},
    ],
)
def test_unsafe_misleading_or_malformed_entries_are_refused(change: Dict[str, Any]) -> None:
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
    assert [line.id for line in load_catalog(str(path))] == ["acme-cloud"]


def test_missing_or_broken_catalogue_gives_no_lines(tmp_path: Path) -> None:
    assert load_catalog("") == []
    assert load_catalog(str(tmp_path / "missing.json")) == []
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_catalog(str(broken)) == []


def test_the_public_line_has_a_counted_link_and_no_destination() -> None:
    line = validate_entry(GOOD)[0]
    assert line is not None
    public = line.as_public()
    assert public == {
        "label": LABEL,
        "id": "acme-cloud",
        "sponsor": "Acme Cloud",
        "text": "Host your side projects for free this month.",
        "go": "/sponsors/go/acme-cloud",
    }
    assert "url" not in public  # the dashboard cannot skip the counted redirect


def test_rotation_depends_only_on_the_clock_not_on_the_viewer(db_engine: Any) -> None:
    lines = [validate_entry(_entry(id=f"s-{i}"))[0] for i in range(3)]
    settings = _settings()
    first = current_line(settings, now=10_000, catalog=cast(List[Any], lines))
    same_window = current_line(settings, now=10_100, catalog=cast(List[Any], lines))
    next_window = current_line(settings, now=10_300, catalog=cast(List[Any], lines))
    assert first == same_window
    assert first is not None and next_window is not None
    assert first["id"] != next_window["id"]


def test_off_switch_shows_nothing_and_counts_nothing(db_engine: Any) -> None:
    line = validate_entry(GOOD)[0]
    assert current_line(_settings(enabled=False), catalog=[line] if line else None) is None
    assert go_target(_settings(enabled=False), "acme-cloud", catalog=[line] if line else None) is None
    assert ledger.summary([line] if line else []) == [
        {"id": "acme-cloud", "sponsor": "Acme Cloud", "views": 0, "clicks": 0, "cap_clicks": 0,
         "capped": False, "owed_cents": 0}
    ]


def test_views_and_clicks_are_counted_and_priced(db_engine: Any) -> None:
    line = validate_entry(GOOD)[0]
    assert line is not None
    current_line(_settings(), now=1, catalog=[line], record_view=True)
    current_line(_settings(), now=2, catalog=[line], record_view=True)
    assert go_target(_settings(), "acme-cloud", catalog=[line]) == GOOD["url"]
    row = ledger.summary([line])[0]
    assert (row["views"], row["clicks"], row["owed_cents"]) == (2, 1, 25)


def test_a_campaign_stops_being_shown_at_its_click_cap(db_engine: Any) -> None:
    capped = validate_entry(_entry(id="capped", cap_clicks=2))[0]
    open_ = validate_entry(_entry(id="open"))[0]
    assert capped is not None and open_ is not None
    catalog = [capped, open_]
    for _ in range(2):
        assert go_target(_settings(), "capped", catalog=catalog) == GOOD["url"]
    assert ledger.summary(catalog)[0]["capped"] is True
    # Only the open campaign is eligible from now on.
    for moment in range(0, 3000, 300):
        shown = current_line(_settings(), now=moment, catalog=catalog)
        assert shown is not None and shown["id"] == "open"
    # A reader who clicks a capped campaign is still sent on, but the click is not counted.
    assert go_target(_settings(), "capped", catalog=catalog) == GOOD["url"]
    assert ledger.summary(catalog)[0]["clicks"] == 2


def test_no_eligible_campaign_means_no_line(db_engine: Any) -> None:
    capped = validate_entry(_entry(id="only", cap_clicks=1))[0]
    assert capped is not None
    go_target(_settings(), "only", catalog=[capped])
    assert current_line(_settings(), catalog=[capped]) is None


def test_unknown_sponsor_ids_are_not_redirected(db_engine: Any) -> None:
    line = validate_entry(GOOD)[0]
    assert go_target(_settings(), "not-in-catalogue", catalog=[line] if line else None) is None


def test_the_public_endpoints_are_public_and_others_are_not() -> None:
    assert is_public_path("/sponsors/line")
    assert is_public_path("/sponsors/go/acme-cloud")
    assert not is_public_path("/sponsors/other")


@pytest.mark.asyncio
async def test_route_returns_null_when_disabled(settings: Any) -> None:
    off = settings.model_copy(update={"SPONSORS_ENABLED": False}, deep=True)
    app = create_app(settings=off, orchestrator=cast(Any, StubOrchestrator(off)))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        response = await c.get("/sponsors/line")
    assert response.status_code == 200
    assert response.json() == {"line": None}
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_route_serves_the_line_counts_the_view_and_redirects_clicks(settings: Any, tmp_path: Path) -> None:
    path = tmp_path / "sponsors.json"
    path.write_text(json.dumps([GOOD]), encoding="utf-8")
    enabled = settings.model_copy(update={"SPONSORS_ENABLED": True, "SPONSORS_FILE": str(path)}, deep=True)
    app = create_app(settings=enabled, orchestrator=cast(Any, StubOrchestrator(enabled)))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=transport, base_url="http://t", follow_redirects=False
    ) as c:
        body = (await c.get("/sponsors/line")).json()
        assert body["line"]["go"] == "/sponsors/go/acme-cloud"
        assert "url" not in body["line"]
        click = await c.get("/sponsors/go/acme-cloud")
        assert click.status_code == 302
        assert click.headers["location"] == GOOD["url"]
        assert click.headers["referrer-policy"] == "no-referrer"
        assert (await c.get("/sponsors/go/nobody")).status_code == 404
    row = ledger.summary([validate_entry(GOOD)[0]])[0]  # type: ignore[list-item]
    assert (row["views"], row["clicks"], row["owed_cents"]) == (1, 1, 25)


def test_the_sponsor_line_is_off_until_the_operator_turns_it_on() -> None:
    from config.settings import Settings

    assert Settings.model_fields["SPONSORS_ENABLED"].default is False
