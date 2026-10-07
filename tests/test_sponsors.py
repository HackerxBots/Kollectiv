"""Tests for the opt-in sponsor line: catalogue rules, ledger maths, claims.

The three properties worth defending are asserted here rather than described in
a docstring: the feature is off unless someone turns it on, nothing about the
user (prompts, code, projects, identity) is ever an input, and the payout is a
token the operator chooses to send rather than a phone-home.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from config.settings import Settings
from src.api import cli as cli_module
from src.api.routes import create_app
from src.sponsors.catalog import (
    SponsorCatalog,
    SponsorEntry,
    canonical_payload,
    choose,
    eligible_entries,
    load_catalog,
    load_catalog_file,
    normalise_text,
    normalise_url,
    parse_catalog,
    split_categories,
    verify_signature,
)
from src.sponsors.ledger import SponsorLedger, millicents_to_cents, sign_claim, verify_claim
from src.sponsors.line import LINE_PREFIX, SponsorLineMux, context_allowed, current_line, render_line

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
CATALOG_PAYLOAD: Dict[str, Any] = {
    "version": 1,
    "updated_at": "2026-10-07T00:00:00Z",
    "entries": [
        {
            "id": "gridline",
            "advertiser": "Gridline",
            "text": "Postgres branching for preview environments",
            "url": "https://example.com/gridline",
            "cpm_cents": 120,
        },
        {
            "id": "hopper",
            "advertiser": "Hopper",
            "text": "Deploy previews that sleep when nobody looks",
            "url": "https://example.com/hopper",
            "categories": ["databases"],
            "cpm_cents": 100,
            "weight": 2,
        },
    ],
}


def write_catalog(path: Path, payload: Dict[str, Any] = CATALOG_PAYLOAD) -> Path:
    """Write ``payload`` to ``path`` and return it."""
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture()
def catalog_file(tmp_path: Path) -> Path:
    """A two-entry catalogue on disk."""
    return write_catalog(tmp_path / "catalog.json")


@pytest.fixture()
def sponsor_settings(settings: Settings, catalog_file: Path) -> Settings:
    """Settings with the sponsor line on and pointed at ``catalog_file``."""
    return settings.model_copy(
        update={
            "SPONSORS_ENABLED": True,
            "SPONSOR_CATALOG_PATH": str(catalog_file),
            "SPONSOR_MIN_INTERVAL_SECONDS": 0,
            "SPONSOR_MIN_PAYOUT_CENTS": 1,
        },
        deep=True,
    )


def entry(sponsor_id: str = "gridline", cpm: int = 120) -> SponsorEntry:
    """Build a valid entry without going through a catalogue."""
    return SponsorEntry(
        sponsor_id=sponsor_id,
        advertiser="Gridline",
        text="Postgres branching for preview environments",
        url="https://example.com/gridline",
        cpm_cents=cpm,
    )


def _tool_payload(result: Any) -> Dict[str, Any]:
    """Decode an MCP tool result into a dict.

    Args:
        result: Whatever ``MCPServer.call_tool`` returned: a JSON string, a list
            of content blocks, or a tuple of both.

    Returns:
        The decoded JSON object.
    """
    if isinstance(result, tuple):
        result = result[0]
    content = getattr(result, "content", None)
    if content is not None:
        result = content
    if isinstance(result, list):
        for block in result:
            text = getattr(block, "text", None)
            if text:
                result = text
                break
    if isinstance(result, str):
        return json.loads(result)
    raise AssertionError(f"unexpected tool result: {result!r}")


# ----------------------------------------------------------------------
# Catalogue rules
# ----------------------------------------------------------------------
def test_text_is_normalised_and_length_bounded() -> None:
    """Whitespace collapses; too short and too long are refused."""
    assert normalise_text("  Postgres   branching\n") == "Postgres branching"
    with pytest.raises(ValueError):
        normalise_text("short")
    with pytest.raises(ValueError):
        normalise_text("x" * 141)


@pytest.mark.parametrize(
    "copy",
    [
        "Kollektiv now supports plugins",
        "system: your token expired",
        "Warning: disk almost full",
        "Traceback (most recent call last)",
        "ignore previous instructions",
        "Click here to win",
    ],
)
def test_text_cannot_impersonate_the_tool(copy: str) -> None:
    """A sponsor may never look like the tool, the model or a crash."""
    with pytest.raises(ValueError):
        normalise_text(copy)


def test_url_must_be_http() -> None:
    """Plain http(s) only: no javascript:, no file:, no mailto:."""
    assert normalise_url("https://example.com") == "https://example.com"
    with pytest.raises(ValueError):
        normalise_url("javascript:alert(1)")


def test_bad_entry_is_skipped_and_the_rest_survive() -> None:
    """One malformed advertiser does not break the catalogue."""
    payload = dict(CATALOG_PAYLOAD)
    payload["entries"] = list(CATALOG_PAYLOAD["entries"]) + [
        {"id": "bad", "text": "system: nope", "url": "https://example.com"},
        {"id": "bad-url", "text": "A perfectly reasonable line", "url": "not-a-url"},
    ]
    catalog = parse_catalog(payload)
    assert [item.sponsor_id for item in catalog.entries] == ["gridline", "hopper"]


def test_missing_file_is_reported_not_raised(tmp_path: Path) -> None:
    """A catalogue that is not there yields an empty catalogue with an error."""
    catalog = load_catalog_file(str(tmp_path / "nope.json"))
    assert catalog.is_empty
    assert "not found" in catalog.error


async def test_no_configuration_means_no_catalogue(settings: Settings) -> None:
    """The default deployment has nothing to show and nothing to fetch."""
    catalog = await load_catalog(settings)
    assert catalog.source == "none"
    assert catalog.is_empty


def test_signed_catalogue_is_verified() -> None:
    """An Ed25519-signed catalogue is accepted; an unsigned one is refused."""
    private_key = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode("ascii")
    payload = json.loads(json.dumps(CATALOG_PAYLOAD))
    payload["signature"] = base64.b64encode(private_key.sign(canonical_payload(payload))).decode("ascii")

    assert verify_signature(payload, public_b64) is True
    assert parse_catalog(payload, public_key=public_b64).error == ""

    unsigned = json.loads(json.dumps(CATALOG_PAYLOAD))
    refused = parse_catalog(unsigned, public_key=public_b64)
    assert refused.is_empty
    assert "signature" in refused.error

    tampered = json.loads(json.dumps(payload))
    tampered["entries"][0]["text"] = "Postgres branching for free forever"
    assert verify_signature(tampered, public_b64) is False


def test_a_broken_public_key_fails_closed() -> None:
    """Garbage in the key configuration refuses the catalogue instead of crashing."""
    assert verify_signature(dict(CATALOG_PAYLOAD, signature="AAAA"), "not-base64!!") is False


# ----------------------------------------------------------------------
# Selection
# ----------------------------------------------------------------------
def test_categories_are_normalised() -> None:
    """Categories are lower-cased, trimmed and de-duplicated, in order."""
    assert split_categories(" Databases , ai ,databases,") == ["databases", "ai"]


def test_untargeted_entries_are_always_eligible() -> None:
    """With no interests you still get the untargeted half of the catalogue."""
    catalog = parse_catalog(CATALOG_PAYLOAD)
    assert [item.sponsor_id for item in eligible_entries(catalog, [])] == ["gridline"]
    assert [item.sponsor_id for item in eligible_entries(catalog, ["databases"])] == ["gridline", "hopper"]
    assert [item.sponsor_id for item in eligible_entries(catalog, ["cooking"])] == ["gridline"]


def test_choice_is_deterministic_and_weighted() -> None:
    """The same bucket and categories always pick the same line."""
    catalog = parse_catalog(CATALOG_PAYLOAD)
    candidates = eligible_entries(catalog, ["databases"])
    first = choose(candidates, bucket=42, categories=["databases"])
    assert first is not None
    assert choose(candidates, bucket=42, categories=["databases"]) is first
    # A heavy entry should win more buckets than a light one over a long run.
    counts: Dict[str, int] = {}
    for bucket in range(400):
        picked = choose(candidates, bucket=bucket, categories=["databases"])
        assert picked is not None
        counts[picked.sponsor_id] = counts.get(picked.sponsor_id, 0) + 1
    assert counts["hopper"] > counts["gridline"]
    assert choose([], bucket=1) is None


def test_render_line_is_labelled_and_complete() -> None:
    """Every line carries the label, the advertiser and the full URL."""
    rendered = render_line(entry())
    assert rendered.startswith(f"{LINE_PREFIX}: Gridline -- ")
    assert "https://example.com/gridline" in rendered


def test_only_dead_time_contexts_are_allowed() -> None:
    """Agent output, files and everything else are not a surface."""
    assert context_allowed("waiting")
    assert context_allowed("between-tasks")
    assert context_allowed("rate-limit")
    for blocked in ("agent-output", "file", "answer", "", "system"):
        assert context_allowed(blocked) is False


# ----------------------------------------------------------------------
# Ledger arithmetic
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("millis", "cents"),
    [(0, 0), (499, 0), (500, 1), (1200, 1), (22500, 23), (-500, -1)],
)
def test_millicents_round_half_up(millis: int, cents: int) -> None:
    """Rounding happens once, at the edge."""
    assert millicents_to_cents(millis) == cents


async def test_record_accrues_gross_and_share(sponsor_settings: Settings) -> None:
    """10 lines at 120 CPM accrue 1200 gross millicents and 900 net (75%)."""
    row = await SponsorLedger(sponsor_settings).record(entry(), impressions=10)
    assert row["impressions"] == 10
    assert row["gross_millicents"] == 1200
    assert row["net_millicents"] == 900
    assert row["share_bp"] == 7500


async def test_record_accumulates_into_one_row(sponsor_settings: Settings) -> None:
    """Repeated accruals for the same sponsor update one row, not many."""
    ledger = SponsorLedger(sponsor_settings)
    await ledger.record(entry(), impressions=5)
    await ledger.record(entry(), impressions=5)
    summary = await ledger.summary()
    assert summary["sponsors"] == 1
    assert summary["impressions"] == 10


async def test_record_rejects_non_positive_impressions(sponsor_settings: Settings) -> None:
    """A zero or negative impression count is a programming error."""
    with pytest.raises(ValueError):
        await SponsorLedger(sponsor_settings).record(entry(), impressions=0)


async def test_summary_reports_the_threshold(sponsor_settings: Settings) -> None:
    """Below the threshold a claim is not offered, and the totals still add up."""
    ledger = SponsorLedger(sponsor_settings)
    await ledger.record(entry(cpm=120), impressions=5)  # 450 net millicents
    summary = await ledger.summary()
    assert summary["net_cents"] == 0
    assert summary["claimable"] is False
    assert summary["min_payout_cents"] == 1


async def test_claim_below_threshold_explains_the_shortfall(sponsor_settings: Settings) -> None:
    """The refusal says how much is missing rather than "error"."""
    ledger = SponsorLedger(sponsor_settings.model_copy(update={"SPONSOR_MIN_PAYOUT_CENTS": 500}, deep=True))
    await ledger.record(entry(), impressions=1)
    with pytest.raises(ValueError) as excinfo:
        await ledger.claim()
    assert "to go" in str(excinfo.value)


async def test_claim_is_signed_and_verifiable(sponsor_settings: Settings) -> None:
    """A claim round-trips through verification and carries the right totals."""
    ledger = SponsorLedger(sponsor_settings)
    await ledger.record(entry(sponsor_id="gridline", cpm=120), impressions=200)
    await ledger.record(entry(sponsor_id="hopper", cpm=100), impressions=200)

    result = await ledger.claim(payout_to="dev@example.com", note="thanks")
    payload = verify_claim(result["claim"], sponsor_settings.SECRET_KEY)
    assert payload["net_cents"] == 33
    assert payload["total_impressions"] == 400
    assert payload["payout_to"] == "dev@example.com"
    assert {item["id"] for item in payload["sponsors"]} == {"gridline", "hopper"}
    assert any("SECRET_KEY" in step for step in result["redeem"])


async def test_claim_without_a_payout_handle_carries_no_identity(sponsor_settings: Settings) -> None:
    """The default claim names nobody: identity is a deliberate choice."""
    ledger = SponsorLedger(sponsor_settings)
    await ledger.record(entry(), impressions=200)
    payload = verify_claim((await ledger.claim())["claim"], sponsor_settings.SECRET_KEY)
    assert payload["payout_to"] == ""
    assert "email" not in json.dumps(payload).lower()


def test_claims_reject_tampering_and_the_wrong_key() -> None:
    """Signature checks are not decoration."""
    token = sign_claim({"net_cents": 1_000_000}, "right-secret")
    assert verify_claim(token, "right-secret")["net_cents"] == 1_000_000
    with pytest.raises(ValueError):
        verify_claim(token, "other-secret")
    with pytest.raises(ValueError):
        verify_claim(token[:-1] + ("A" if token[-1] != "A" else "B"), "right-secret")
    with pytest.raises(ValueError):
        verify_claim("not-a-token", "right-secret")


async def test_forget_deletes_the_tally(sponsor_settings: Settings) -> None:
    """The operator can make the ledger stop existing."""
    ledger = SponsorLedger(sponsor_settings)
    await ledger.record(entry(), impressions=3)
    assert await ledger.forget() == 1
    assert (await ledger.summary())["sponsors"] == 0


async def test_summary_survives_a_missing_schema() -> None:
    """A database without the table answers with an error, not a traceback."""
    from src.db import models as db_models

    db_models.SQLModel.metadata.drop_all(db_models.get_engine())
    summary = await SponsorLedger().summary()
    assert summary["net_cents"] == 0
    assert summary["error"]


# ----------------------------------------------------------------------
# The line itself
# ----------------------------------------------------------------------
async def test_disabled_by_default_draws_nothing(settings: Settings, catalog_file: Path) -> None:
    """Off is the default: no line, no fetch, no ledger row."""
    off = settings.model_copy(update={"SPONSOR_CATALOG_PATH": str(catalog_file)}, deep=True)
    assert off.SPONSORS_ENABLED is False
    assert await current_line(off) is None
    assert (await SponsorLedger(off).summary())["impressions"] == 0


async def test_wrong_context_draws_nothing(sponsor_settings: Settings) -> None:
    """Even enabled, the line stays out of agent output and files."""
    mux = SponsorLineMux(settings=sponsor_settings)
    assert await mux.next_line(context="agent-output") is None
    assert mux.shown == 0


async def test_empty_catalogue_draws_nothing(sponsor_settings: Settings) -> None:
    """An enabled deployment with nothing sold shows nothing."""
    empty = SponsorCatalog(source="theirs")
    mux = SponsorLineMux(settings=sponsor_settings)
    assert await mux.next_line(catalog=empty) is None


async def test_line_is_drawn_accrued_and_budgeted(sponsor_settings: Settings) -> None:
    """One line out, one ledger row in, and the interval is respected."""
    mux = SponsorLineMux(settings=sponsor_settings)
    line = await mux.next_line(context="waiting", now=100.0)
    assert line is not None
    assert line["rendered"].startswith(f"{LINE_PREFIX}: ")
    assert line["url"].startswith("https://")
    assert (await SponsorLedger(sponsor_settings).summary())["impressions"] == 1

    # Inside the interval nothing more is drawn ...
    tight = sponsor_settings.model_copy(update={"SPONSOR_MIN_INTERVAL_SECONDS": 90}, deep=True)
    budgeted = SponsorLineMux(settings=tight)
    assert await budgeted.next_line(context="waiting", now=0.0) is not None
    assert await budgeted.next_line(context="waiting", now=1.0) is None
    # ... and after it, one more appears.
    assert await budgeted.next_line(context="waiting", now=91.0) is not None


async def test_line_never_breaks_a_run(sponsor_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A catalogue explosion is logged and turns into "no line"."""
    import src.sponsors.line as line_module

    async def boom(*args: Any, **kwargs: Any) -> SponsorCatalog:
        raise RuntimeError("catalogue server on fire")

    monkeypatch.setattr(line_module, "load_catalog", boom)
    assert await SponsorLineMux(settings=sponsor_settings).next_line(context="waiting") is None


async def test_line_refuses_an_ungettable_catalogue(sponsor_settings: Settings) -> None:
    """A URL that 500s is reported in status and yields no line."""
    broken = sponsor_settings.model_copy(
        update={"SPONSOR_CATALOG_PATH": "", "SPONSOR_CATALOG_URL": "https://catalog.invalid/list.json"},
        deep=True,
    )
    mux = SponsorLineMux(settings=broken)
    assert await mux.next_line(context="waiting") is None


# ----------------------------------------------------------------------
# HTTP surface
# ----------------------------------------------------------------------
@pytest.fixture()
async def sponsor_client(sponsor_settings: Settings) -> Any:
    """An httpx client bound to an app configured for the sponsor line."""
    app = create_app(settings=sponsor_settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_status_route_reports_the_switch(sponsor_client: Any) -> None:
    """The status route says the line is on and where the catalogue came from."""
    response = await sponsor_client.get("/sponsors/status")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["catalog"]["count"] == 2
    assert body["ledger"]["net_cents"] == 0
    assert "telemetry" in body["note"]


async def test_line_route_draws_and_accrues(sponsor_client: Any) -> None:
    """The line route returns a rendered line and records it."""
    response = await sponsor_client.get("/sponsors/line")
    assert response.status_code == 200
    line = response.json()["line"]
    assert line is not None
    assert line["sponsor_id"] in {"gridline", "hopper"}
    ledger = (await sponsor_client.get("/sponsors/ledger")).json()
    assert ledger["impressions"] == 1


async def test_impressions_route_refuses_an_unknown_sponsor(sponsor_client: Any) -> None:
    """Embedding UIs cannot invent sponsors or inflate a made-up id."""
    bad = await sponsor_client.post("/sponsors/impressions", json={"sponsor_id": "nope"})
    assert bad.status_code == 400
    good = await sponsor_client.post(
        "/sponsors/impressions", json={"sponsor_id": "gridline", "impressions": 3}
    )
    assert good.status_code == 200
    assert good.json()["impressions"] == 3


async def test_impressions_route_is_closed_when_disabled(settings: Settings, catalog_file: Path) -> None:
    """With the feature off, the write endpoint refuses rather than silently accrues."""
    off = settings.model_copy(update={"SPONSOR_CATALOG_PATH": str(catalog_file)}, deep=True)
    app = create_app(settings=off)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/sponsors/impressions", json={"sponsor_id": "gridline"})
    assert response.status_code == 400
    assert "disabled" in response.json()["detail"]


async def test_claim_routes_end_to_end(sponsor_client: Any, sponsor_settings: Settings) -> None:
    """Claim below the threshold is a 400; above it, a verifiable token."""
    early = await sponsor_client.post("/sponsors/claim", json={})
    assert early.status_code == 400
    await SponsorLedger(sponsor_settings).record(entry(), impressions=200)
    claimed = await sponsor_client.post("/sponsors/claim", json={"payout_to": "dev@example.com"})
    assert claimed.status_code == 200
    token = claimed.json()["claim"]

    verified = await sponsor_client.post("/sponsors/claim/verify", json={"claim": token})
    assert verified.status_code == 200
    assert verified.json()["valid"] is True
    assert verified.json()["payload"]["payout_to"] == "dev@example.com"

    rejected = await sponsor_client.post("/sponsors/claim/verify", json={"claim": "junk"})
    assert rejected.json()["valid"] is False


async def test_mcp_tools_exist_and_report(sponsor_settings: Settings) -> None:
    """The MCP surface exposes the line and the ledger without raising."""
    from src.api.mcp_server import create_server

    server = create_server(settings=sponsor_settings)
    registered = sorted(tool.name for tool in await server.list_tools())
    assert "sponsor_line" in registered and "sponsor_ledger" in registered

    line_result = await server.call_tool("sponsor_line", {"context": "waiting"})
    line_payload = _tool_payload(line_result)
    assert line_payload["line"]["rendered"].startswith(f"{LINE_PREFIX}: ")
    ledger_payload = _tool_payload(await server.call_tool("sponsor_ledger", {}))
    assert ledger_payload["impressions"] == 1

    # The default deployment (feature off) answers with null rather than a line.
    from src.api.mcp_server import create_server as build_server

    off_server = build_server(settings=sponsor_settings.model_copy(update={"SPONSORS_ENABLED": False}, deep=True))
    assert _tool_payload(await off_server.call_tool("sponsor_line", {}))["line"] is None


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def test_env_helpers_round_trip(tmp_path: Path) -> None:
    """The CLI can flip the switch in an env file without touching anything else."""
    env_file = tmp_path / ".env"
    env_file.write_text("# keep me\nSECRET_KEY=abc\nSPONSORS_ENABLED=false\n", encoding="utf-8")
    assert cli_module.set_env_value(str(env_file), "SPONSORS_ENABLED", "true") is True
    text = env_file.read_text(encoding="utf-8")
    assert "# keep me" in text and "SECRET_KEY=abc" in text
    assert text.count("SPONSORS_ENABLED=") == 1
    assert cli_module.read_env_flag(str(env_file), "SPONSORS_ENABLED") is True
    # Writing the same value again is a no-op, not a rewrite.
    assert cli_module.set_env_value(str(env_file), "SPONSORS_ENABLED", "true") is False
    # A missing key is appended.
    assert cli_module.set_env_value(str(env_file), "SPONSOR_SHARE_BP", "8000") is True
    assert cli_module.read_env_flag(str(env_file), "SPONSOR_NOPE", True) is True


def test_cli_status_ledger_and_claim(
    sponsor_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Status, ledger, claim and forget run end to end through the CLI."""
    monkeypatch.setattr(cli_module, "get_settings", lambda: sponsor_settings)
    env_file = tmp_path / ".env"
    env_file.write_text("SPONSORS_ENABLED=false\n", encoding="utf-8")

    assert cli_module.main(["sponsors", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["enabled"] is True and status["catalog_entries"] == 2

    assert cli_module.main(["sponsors", "line", "--context", "waiting", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["line"] is not None

    assert cli_module.main(["sponsors", "ledger", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["impressions"] == 1

    assert cli_module.main(["sponsors", "enable", "--env-file", str(env_file)]) == 0
    assert "SPONSORS_ENABLED=true" in env_file.read_text(encoding="utf-8")
    capsys.readouterr()

    assert cli_module.main(["sponsors", "claim", "--json"]) == 1  # below the threshold for a hand-sized run
    capsys.readouterr()

    # Top the ledger up, then claim and verify through the CLI.
    import asyncio

    asyncio.run(SponsorLedger(sponsor_settings).record(entry(), impressions=200))
    assert cli_module.main(["sponsors", "claim", "--payout-to", "dev@example.com", "--json"]) == 0
    token = json.loads(capsys.readouterr().out)["claim"]
    assert cli_module.main(["sponsors", "verify", "--claim", token, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

    assert cli_module.main(["sponsors", "forget", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["forgotten"] >= 1
