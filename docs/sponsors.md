# Sponsors: the sponsor line

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

Kollectiv is free for everyone, with no paid tier. The sponsor line is how it pays for
itself, the same way Freebuff's free tools are paid for by ads: one short, text-only,
clearly labelled line from a sponsor, shown in the dashboard, and paid for each click.

It is on by default, but it shows nothing until an operator adds at least one campaign.

## What the operator sets

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPONSORS_ENABLED` | `true` | Turn the line on or off. |
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
- **Targeting from prompts.** Freebuff selects ads from prompts and messages. Kollectiv
  does not: sending your prompts to an ad provider would break the no-tracking promise in
  [Privacy](privacy.md). Choosing by prompt would need your written decision and a
  privacy-policy change first.
- **Revenue share to users.** Freebuff lets developers earn credits by viewing ads. There
  is no paid tier to spend credits on, so this is not built.
- **Ad-network delivery.** Campaigns are set by the operator in a file. Kollectiv does not
  connect to an ad network.

## Turning it off

```bash
SPONSORS_ENABLED=false
```

Restart the API. `GET /sponsors/line` then returns `{"line": null}`.
