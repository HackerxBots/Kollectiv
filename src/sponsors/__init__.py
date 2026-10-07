"""Advertising and revenue share, the Kollektiv way.

Kollektiv is free, MIT licensed and collects no telemetry. It also has to pay
for the servers and the maintainers somehow, and the industry's usual answers --
subscriptions, usage credits, or always-on ads fed by prompt analysis -- all
conflict with at least one of those properties. This package is the fourth
option:

* a **sponsor line** drawn only in dead time (while a worker thinks, while the
  pool waits out a rate limit), always labelled, always opt-in, never on by
  default (:mod:`src.sponsors.line`);
* a **catalogue** the operator controls, from a file or a signed URL, with rules
  that stop an advertiser impersonating the tool (:mod:`src.sponsors.catalog`);
* a **local ledger** that accrues the developer's share of what was shown and
  can produce a **signed claim** -- the tally never leaves the machine unless a
  human sends it (:mod:`src.sponsors.ledger`).

The design is documented in full, including what we deliberately refuse to copy
from ad-supported coding agents, in ``docs/monetization.md``.

Typical use::

    from src.sponsors import SponsorLineMux, SponsorLedger

    line = await SponsorLineMux().next_line(context="waiting")
    if line:
        print(line["rendered"])

    print(await SponsorLedger().summary())
"""

from __future__ import annotations

from src.sponsors.catalog import (
    SponsorCatalog,
    SponsorCatalogError,
    SponsorEntry,
    canonical_payload,
    choose,
    eligible_entries,
    fetch_catalog,
    load_catalog,
    load_catalog_file,
    normalise_text,
    normalise_url,
    parse_catalog,
    split_categories,
    verify_signature,
)
from src.sponsors.ledger import (
    SponsorLedger,
    claim_key,
    millicents_to_cents,
    sign_claim,
    verify_claim,
)
from src.sponsors.line import (
    ALLOWED_CONTEXTS,
    LINE_PREFIX,
    SponsorLineMux,
    context_allowed,
    current_line,
    render_line,
    sponsor_line_enabled,
)

__all__ = [
    "ALLOWED_CONTEXTS",
    "LINE_PREFIX",
    "SponsorCatalog",
    "SponsorCatalogError",
    "SponsorEntry",
    "SponsorLedger",
    "SponsorLineMux",
    "canonical_payload",
    "choose",
    "claim_key",
    "context_allowed",
    "current_line",
    "eligible_entries",
    "fetch_catalog",
    "load_catalog",
    "load_catalog_file",
    "millicents_to_cents",
    "normalise_text",
    "normalise_url",
    "parse_catalog",
    "render_line",
    "sign_claim",
    "split_categories",
    "sponsor_line_enabled",
    "verify_claim",
    "verify_signature",
]
