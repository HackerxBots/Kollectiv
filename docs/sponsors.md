# Sponsors: the sponsor line

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

Kollectiv is free for everyone, with no paid tier. The sponsor line is how it pays for
itself, the way ad-funded free tools do: one short, text-only,
clearly labelled line from a sponsor, shown in the dashboard, and paid for each click.

It is off until the operator adds at least one real campaign and sets `SPONSORS_ENABLED=true`.

This is Kollektiv's own per-click model. It is not a copy of Freebuff's terms, which we have not seen.

## What the operator sets

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPONSORS_ENABLED` | `false` | Turn the line on. Keep it off until you have a real sponsor. |
| `SPONSORS_FILE` | empty | Path to the campaign file (JSON, see below). Empty means no line. |
| `SPONSOR_ROTATE_SECONDS` | `300` | How often the line changes. The minimum is 30. |

## Campaigns

A JSON list, or `{"sponsors": [...]}`. Up to 50 campaigns.

```json
[
  {
    "id": "example-cloud",
    "sponsor": "Example Cloud",
    "text": "Deploy static sites free, with no tracking.",
    "url": "https://example.com/offer",
    "cpc_cents": 25,
    "cap_clicks": 2000,
    "active": true
  }
]
```

| Field | Rule |
| --- | --- |
| `id` | Lowercase letters, digits and dashes; at most 40 characters; unique. |
| `sponsor` | The name shown after the label; at most 60 characters. |
| `text` | One line, at most 140 characters. |
| `url` | `https` only, no user name or password, at most 300 characters. |
| `cpc_cents` | What the sponsor pays per click, in cents. Whole number, 0 to 100,000. |
| `cap_clicks` | The campaign ends after this many clicks. `0` means no cap. |
| `active` | `false` keeps a campaign in the file without running it. |

The label is always **Sponsored**, and a campaign cannot change it. A campaign is
refused, with the reason logged, when its text reads like an error, a warning, a usage
or rate-limit message, or the tool or a model name. A sponsor should never pass for part
of the product. A bad campaign is skipped and the rest still run.

## How the line is chosen

- The line rotates on the clock across the campaigns that have not reached their cap.
  Everyone in the same window sees the same line, so the server keeps nothing per viewer.
- A campaign at its cap is not shown. When every campaign is capped, nothing shows.
- Views and clicks are counted, with the price at that moment, on your own server.

## What is counted, and what is not

Each view and each click is one row in the `sponsor_events` table: the sponsor id, the
kind of event (`view` or `click`), the price, and the time. Nothing else is stored.

- **No identity.** No viewer, cookie, address, project, prompt or account is stored.
- **Clicks go through Kollectiv.** The dashboard links to `/sponsors/go/<id>`, which
  counts the click and redirects. The redirect sends `Referrer-Policy: no-referrer`, so the
  sponsor does not learn which page sent the reader.
- **Nothing leaves your server.** Kollectiv sends no data to any sponsor, ad network or
  payment provider.

## Seeing what is owed

```bash
kollektiv sponsors line      # the line that is live now
kollektiv sponsors ledger    # views, clicks, cap and amount owed per sponsor
kollektiv sponsors ledger --json
```

`owed` is clicks multiplied by the price each click was recorded at. Invoice sponsors
from those totals. Kollectiv does not take payments.

## What is not built yet

- **Payments.** There is no payment provider, invoice or payout. You invoice sponsors
  from the ledger yourself.
- **Targeting from prompts.** Planned as an opt-in only, and not built. Nothing is sent to any provider today. The rules are in [Privacy](privacy.md#prompt-based-sponsor-matching-planned-opt-in).
- **Revenue share to users.** Not built. There is no paid tier to spend credits on.
- **Ad-network delivery.** Campaigns are set by the operator in a file. Kollectiv does not
  connect to an ad network.

## Turning it on

```bash
SPONSORS_ENABLED=true
SPONSORS_FILE=/path/to/sponsors.json
```

Restart the API. Until both are set, `GET /sponsors/line` returns `{"line": null}` and nothing shows.
