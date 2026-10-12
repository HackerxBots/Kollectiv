# Sponsors: the opt-in sponsor line

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

Kollectiv can show **one** sponsor line in the dashboard, the way Freebuff shows ads
in its free tool. It is opt-in, text only, clearly labelled, and it never tracks.
It is off by default.

## What the operator sets

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPONSORS_ENABLED` | `false` | Turn the line on. Nothing is shown while this is `false`. |
| `SPONSORS_FILE` | empty | Path to the catalogue (JSON, see below). |
| `SPONSOR_ROTATE_SECONDS` | `300` | How often the line changes. The minimum is 30. |

## The catalogue

A JSON list, or `{"sponsors": [...]}`. Up to 50 entries.

```json
[
  {
    "id": "example-cloud",
    "sponsor": "Example Cloud",
    "text": "Deploy static sites free, with no tracking.",
    "url": "https://example.com/offer",
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
| `active` | `false` keeps an entry in the file without showing it. |

The label is always **Sponsored**. A catalogue cannot change it.

An entry is refused, with the reason logged, when its text reads like an error, a
warning, a usage or rate-limit message, or the tool or a model name. The point is
that a sponsor can never pass for part of the product.

A bad entry is skipped. The rest of the file still works. A missing or broken file
gives no line and never breaks the dashboard.

## How the line is picked

- The line is chosen from the clock alone. Every viewer in the same window sees the
  same line, so the server keeps nothing per viewer.
- The endpoint is `GET /sponsors/line`. It takes no input, sets no cookie, and
  replies `{"line": null}` when sponsors are off.
- The dashboard makes that one request. It sends no token and no identifier, and it
  inserts the text as plain text, never as HTML.

## What is never done

- No tracking: no identifiers, no impression or click pings, no pixels, no
  third-party scripts. The dashboard's only outside link is the one the sponsor
  supplies, and it opens in a new tab with `rel="noopener noreferrer sponsored"`.
- No sponsor reaches generated code, prompts, answers or project files.
- Nothing is sent to a sponsor. Payment to the operator is arranged outside the
  product, as a flat sponsorship. There is no per-impression accounting, because
  there is no measurement.

Your web server still logs requests the way every web server does. That is the
normal log of your own host, and Kollectiv does not read or send it anywhere.

## Turning it on

```bash
SPONSORS_ENABLED=true
SPONSORS_FILE=/path/to/sponsors.json
```

Restart the API. Check it with `curl http://localhost:8000/sponsors/line`.
