# Privacy

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

## Privacy: no telemetry, no accounts, no data collection

Kollektiv has no analytics, no crash reporting, no phoned-home pings, no
"anonymous usage statistics" and no accounts of its own. There is nothing to opt
out of, because nothing is collected.

- **The only network traffic is what you configure.** Every outbound request
  goes to an endpoint you put in `.env`: your LLM provider, your GitHub
  repository, your R2/TeraBox drive, your connectors. Nothing else leaves the
  process — grep the source for `httpx` and the list of hosts is exactly that.
- **Your code and prompts stay yours.** Plans, artifacts and the state document
  are written to your database and your storage. The maintainers never see them.
- **No identifiers.** No install IDs, no device fingerprinting, no email
  collection. `GET /health`, `kollektiv check` and the dashboard read the local
  configuration only.
- **Self-hosted by default** (see [Self-hosting checklist](deployment.md#self-hosting-checklist)):
  the free hosted tiers in this README (Cloudflare, Neon, Clerk, Resend) are
  conveniences you can replace with something you run, one at a time.
- **Auditable in one command.** `rg "httpx|requests" src/` lists every place a
  request can be made; the connector catalogue (`GET /connectors`) shows every
  service currently configured.

## The sponsor line does not change any of this

The opt-in advertising surface ([Monetization](monetization.md)) was built to be
compatible with this page rather than an exception to it:

- It is **off by default** (`SPONSORS_ENABLED=false`) and nothing is fetched,
  shown or recorded until you turn it on.
- **No prompt, no code, no repository, no project and no identity** is ever an
  input to choosing a line. The only targeting is category checkboxes you tick
  yourself, and the selection is a local HMAC over the minute and those
  categories.
- Impression counts live in **your** database, in aggregate per sponsor -- there
  is no per-event trail. `kollektiv sponsors forget` deletes the tally.
- A payout is a **token you send**, not a request Kollektiv makes: the signed
  claim contains the totals, the payout handle you typed, and nothing else.
- If you configure `SPONSOR_CATALOG_URL`, the only request is a fetch of the
  sponsor list; the hosted relay design (which would count impressions) sees a
  random per-install token, never content, and it is documented but not built.

There is no paid tier, no data resale and no business model that needs your
data. That is a promise the licence (MIT) lets anyone verify and fork.

---
