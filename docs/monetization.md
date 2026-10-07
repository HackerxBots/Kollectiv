# Monetization: the honest version

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

Kollektiv is free, MIT licensed, self-hosted by default and collects no
telemetry. It still has hosting bills, and the people who maintain it still eat.
This page is the whole strategy, written so that a contributor can disagree with
a specific decision instead of the general vibe. It answers two questions:

1. Should Kollektiv affiliate with Claude Code / Codex, or ship its own MCP
   server and gateway?
2. How do ad-supported coding agents (Freebuff and the "ads pay for your
   subscription while you wait" extensions) actually make money, and how do we
   get that on Kollektiv without becoming them?

Everything below is implemented unless it is explicitly marked **designed, not
built**. The knobs live in `config/settings.py` under the `SPONSOR_*` names, the
code lives in `src/sponsors/`, and the end-user commands are
`kollektiv sponsors ...`.

---

## 1. Short answer

**Do not chase an affiliate deal.** There is no public individual affiliate
programme for Claude Code or Codex to join: Anthropic's route is the Claude
Partner Network (enterprise services, free to join, no published revenue share)
and OpenAI's is the Partner Network plus the unpaid Codex Ambassadors. "Affiliate
with Claude Code" is not a revenue line, it is a hope.

**Do ship the MCP server we already have, and then a gateway.** `python -m
src.api.mcp_server` already speaks MCP, which means Claude Code, Codex, Cursor,
Windsurf, Zed and anything else with an MCP client can call Kollektiv's projects,
connectors, storage and handoffs. A gateway (one URL, per-client tokens, tool
namespacing, a local audit log) turns that from "a server you can add" into "the
way a team shares its agents". That is distribution, and distribution is what
actually makes an open-source project survive — not a referral link nobody
offers.

**Do the Freebuff trick, but only the honest half.** The trick is not "ads"; it
is *selling the dead time between agent turns*. Freebuff sells it always-on, in
five products, with prompt analysis choosing the ads. The Claude Code spinner
networks sell it 75%-to-the-developer and claim never to look at your code. We
take the second shape and tighten it: dead time only, opt-in, off by default,
labelled, self-declared categories only, a **local** ledger, and a payout the
operator sends by choice. No prompt, no code, no repository, no history is ever
read to choose a line.

---

## 2. Why there is no affiliate money

| Route | Reality (checked October 2026) |
| --- | --- |
| Anthropic individual affiliate | Does not exist. No commission schedule, no signup page. |
| Claude Partner Network | Enterprise services programme launched March 2026, free to join, no published revenue share. The June 2026 Services Track gates *Select* on 10 certified practitioners, 2 joint production customers and 1 public customer story — a company programme, not a link. |
| OpenAI affiliate | Does not exist. ChatGPT/API have partnership intake forms, not commissions. |
| Codex Ambassadors | Explicitly unpaid: credits and swag for community organisers, 2–4 h/week. |
| ChatGPT/Codex referral credits | Community reports of referrals stuck "Pending" and expiring. Do not build a business on it. |

The conclusion is not "affiliate later". It is that referral money for coding
agents does not exist, so anything we build must create value first — attention,
integrations, or hosting — and then, optionally, charge for it.

## 3. Own MCP server + gateway: what we ship and what we do not

**Shipped now**

- `python -m src.api.mcp_server` (stdio / SSE / streamable HTTP) exposing the
  project, connector, storage, handoff, sponsor and status tools.
- `AUTH_REQUIRED` + Clerk for the HTTP surface, so a shared gateway is a
  deployment decision rather than a rewrite.

**Next, small and useful: `kollektiv gateway` — designed, not built**

- One process, one port, many clients. Each client (Claude Code, Codex, Cursor,
  a teammate's IDE) gets its own token; every call is attributed to it.
- Tool namespacing: `github.*`, `storage.*`, `connectors.notion.*`, so two MCP
  servers with a `search` tool cannot collide.
- A local audit log (SQLite, on your disk, never uploaded) of tool, client,
  duration and outcome. The point is *your* debugging, not our analytics.
- Policy file: which tools are read-only, which need `confirm=true`, which are
  refused for a given client. Default deny for anything destructive.
- Kollektiv as an MCP *client* too, so a worker agent can attach a third-party
  MCP server without a plugin system.

**Rules for the gateway**

- Never proxy or store a vendor subscription credential. A user's Claude Code
  login is theirs; Kollektiv does not become a reseller of someone else's seat.
- Never ship a fork or a patch that evades a vendor's terms or rate limits. The
  gateway is our surface; their clients stay theirs.
- The gateway must be optional. `python -m src.api.mcp_server` and the REST API
  stay first-class forever, because a plugin-only product is not open source,
  it is a plugin.

---

## 4. How Freebuff and the spinner networks actually make money

### Freebuff (Codebuff's ad-supported agent)

- Ads are text lines inside the CLI, the desktop app, the web builder and the
  chat; the free allowance is denominated in "Freebucks", refilled daily; a
  paid Starter tier (~$8/month) buys capacity.
- Ads are **always on and not toggleable**; the published privacy policy says
  prompts and messages may be analysed to select, deliver and measure
  advertising. Advertisers never receive prompts, code or repositories — but
  the platform processes them, and code pasted into a prompt is part of the
  message.
- The code is Apache-2.0, the products are the funnel, the advertisers are the
  customer, and the developer is the product user who pays in attention.

### The "ads pay while you wait" extensions

- `claudecodeads.com` replaces Claude Code's *thinking spinner* line with a
  sponsored line: **75% of ad revenue to the developer**, bids from ~$1 per 1000
  impressions, floor of $10 before payout, paid weekly via PayPal. It states
  the extension has no access to code, prompts or responses — it watches file
  modification times to know a session is active.
- A widely-discussed VS Code/Cursor variant does the same split **50/50**, with
  self-declared category checkboxes as the only targeting and USDT payouts
  (a sibling project advertises the same shape).
- The economics are small and honest: at $1 CPM and a 75% share, 200 lines a
  day for 250 working days is 50,000 lines ≈ **$37 a year**. This pays a VPS,
  not a salary. Anyone promising more is selling the eye, not the line.

### What we copy, and what we refuse to copy

| Decision | Freebuff | Spinner ads | Kollektiv |
| --- | --- | --- | --- |
| Surface | CLI, desktop, web, chat, always on | One line in the thinking spinner | One line in dead time, plus an API for your own UI |
| Consent | On by default, not toggleable | Extension you install | `SPONSORS_ENABLED=true`, off in the box |
| Targeting | Prompt/message analysis | Self-declared categories | Self-declared categories only — and the code has no other input |
| What leaves the machine | Prompts, code, files, traces to the platform | Nothing (claimed) | A catalogue fetch, and nothing at all until you send a claim |
| Money to the developer | None (free access is the payment) | 50–75% of revenue | `SPONSOR_SHARE_BP=7500` (75% default), accruing locally |
| Verifiability | Platform-side | Platform-side | Signed claim you can verify offline with `kollektiv sponsors verify` |
| Where ads appear | Between turns | In the spinner | Never in generated code, files, answers or system messages (enforced by the catalogue validator) |

The one thing all three share, and the only genuinely new idea in this space, is
that **the wait is inventory**. Everything else is policy, and policy is where
we differ on purpose.

---

## 5. The Kollektiv model, in three parts

### 5.1 The line (`src/sponsors/line.py`)

A single labelled line, drawn only from a dead-time context —
`waiting`, `between-tasks`, `rate-limit` — at most once every
`SPONSOR_MIN_INTERVAL_SECONDS` (90 s) per process:

```
sponsored: Example: Branchy -- Postgres branches for every preview environment -- https://example.com/branchy
```

In a run, the interlude fires once, right before the pool blocks on agent work.
Nothing is added to project state, no line is ever written into a file or an
answer, and with the switch off the function returns `None` before touching the
network.

### 5.2 The catalogue (`src/sponsors/catalog.py`)

The operator owns the sponsor list: a JSON file
(`SPONSOR_CATALOG_PATH`, see `docs/sponsors.example.json`) or an HTTPS endpoint
(`SPONSOR_CATALOG_URL`) that should be Ed25519-signed
(`SPONSOR_CATALOG_PUBLIC_KEY` refuses unsigned catalogues). Every entry is
validated before it can be shown, and the validator is the advertiser terms in
code:

- 8–140 characters, single line, printable;
- may not contain `kollektiv`, `system`, `assistant`, `error`, `warning`,
  `traceback`, `ignore previous`, `you must`, `click here` — a line can never
  impersonate the tool, the model or a crash;
- `http(s)` URL only, always printed in full.

Selection is deterministic: a weighted pick keyed by an HMAC of the minute
bucket and the *self-declared* categories. Two people with the same clock and
the same checkboxes see the same sequence, and the choice is a local computation
with no random device state and no server round trip.

### 5.3 The ledger and the claim (`src/sponsors/ledger.py`)

The ledger is a rolling per-sponsor total in your own database: impressions, the
gross at the catalogue rate, and your share in basis points. Money is stored in
millicents so a 120-cent CPM and a 7500-bp share round exactly once.

```
$ kollektiv sponsors ledger
600 line(s) shown, 50 cents to you (66 gross, 75% share)
  hopper              300 lines     23 cents  Hopper
  gridline            300 lines     27 cents  Gridline
```

A payout is a **signed claim**, not a phone-home: an HMAC-SHA256 token over
`{net_cents, impressions, per-sponsor totals, payout_to, issued_at}` signed with
your `SECRET_KEY`. `kollektiv sponsors verify --claim <token>` on the other side
proves it. `kollektiv sponsors forget` deletes the tally; nothing is archived.

Honest caveat, in the code and here: a locally-counted tally is self-reported.
It is fine for "the sponsor pays the person who actually watched", and useless
as anti-fraud. That is the price of not collecting telemetry, and we think it is
the right price.

### 5.4 Hosted relay mode — designed, not built

For sponsors who need counted impressions, the relay is a tiny optional
service:

- The client fetches the next line from `SPONSOR_CATALOG_URL` with a
  random per-install token generated **locally** and stored locally. The relay
  sees a token, a line id and a timestamp. Not prompts, not code, not projects,
  not the user's identity, not even the client's IP address (the relay should
  hash and discard it).
- The relay returns the line plus a receipt; receipts are what reimburses the
  developer, and they are the only thing the relay knows.
- The relay may not join impressions to any other dataset, and the code that
  runs it is in this repository so that claim is auditable.

Until then, local mode is the whole feature, and it works offline.

---

## 6. The rest of the money (ranked honestly)

1. **GitHub Sponsors / Open Collective.** Zero new code, zero user tracking,
   works today. Put the buttons where deployment docs are.
2. **Paid hosting, not paid software.** A managed Kollektiv with a dashboard,
   backups and the gateway pre-configured, while self-hosting stays first-class
   and MIT. This is the only model where revenue scales with value.
3. **Sponsor slots (this page).** Direct-sold, small, opt-in, developer-shared —
   a way for the ecosystem (databases, hosting, API vendors) to fund the
   project they already benefit from.
4. **Support and integration contracts.** Teams that need connectors, policy or
   on-prem deployments pay for hours, not for a licence.
5. **Capacity tiers (a Freebuff lesson).** If a hosted version ever exists:
   higher agent concurrency and longer retention for a price, with the free
   path never crippled and no feature removed from the self-hosted edition.

Explicitly rejected: selling user data (there is none), "anonymised analytics"
(that is data), prompt-based targeting, always-on ads, injecting lines into
model output, per-token markup on free providers, and any scheme where the free
tier exists to make the paid tier feel safe.

---

## 7. Using it

```bash
# Turn it on (writes SPONSORS_ENABLED=true into .env) and point it at a catalogue
kollektiv sponsors enable
kollektiv sponsors catalog --set-url https://sponsors.example.org/catalog.json

# See what would happen
kollektiv sponsors status
kollektiv sponsors catalog
kollektiv sponsors line --context waiting

# Show your own category interests (the only targeting that exists)
SPONSOR_CATEGORIES=databases,devops

# Earn and claim
kollektiv sponsors ledger
kollektiv sponsors claim --payout-to you@example.com
kollektiv sponsors verify --claim <token>     # the sponsor runs this
kollektiv sponsors forget                     # delete the tally
```

HTTP surface: `GET /sponsors/status`, `GET /sponsors/line`,
`GET /sponsors/ledger`, `POST /sponsors/impressions` (your own UI reporting a
line it displayed), `POST /sponsors/claim`, `POST /sponsors/claim/verify`.
MCP tools: `sponsor_line`, `sponsor_ledger`.

### Advertiser terms, short version

Your line is one sentence, always labelled `sponsored:`, always with the
advertiser name and the full URL, shown only in dead time. It may not look like
system output, an error, a model answer or a recommendation from Kollektiv. No
targeting beyond checkboxes the user ticked themselves. Impressions are counted
locally and claimed by the operator. If your copy needs a trick to work, the
catalogue validator will refuse it.

---

## 8. What would make this real

- The relay (§5.4) and a payout rail a sponsor can actually use.
- Two or three ecosystem sponsors with the integrity to appear next to
  `sponsored:` and not look like spam.
- The gateway (§3), so the same deployment serves Claude Code, Codex and the
  dashboard's own agents.

Until then the feature costs nothing to ignore: it is off, it is opt-in, and it
cannot turn itself on.
