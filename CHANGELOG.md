# Changelog

All notable changes to Kollektiv are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html), and every release
on GitHub links to the section below for its version.

## [Unreleased]

_Nothing yet — open a PR and add a line here._

## [0.4.0] — 2026-10-06 — “continuity”

Sessions no longer lose the good part, Arena is the documented default worker
path, connectors are hardened against the most common user errors, and the
project documents that it collects nothing.

### Added

- **Session continuity** (`src/orchestrator/handoff.py`): dependency-aware resume
  briefings with next actions, blockers, recent history and the exact commands
  to continue. Written to `HANDOFF.md` after every run, served as JSON or
  Markdown by `GET /projects/{id}/handoff`, exposed to MCP clients as
  `get_handoff`, and printed by `kollektiv resume`.
- **Arena-first login**: `kollektiv login` (Arena by default, `--provider` for
  optional free providers) stores the session token encrypted in the database —
  never in `.env`, never in git. `kollektiv accounts` lists them masked and
  `kollektiv logout` revokes one.
- **Connector probes**: `kollektiv connectors --probe` and
  `POST /connectors/{name}/probe` run the cheapest read action and report
  reachability with latency, so "is it the token, the service or my typo?" is
  one command.
- **Parameter validation**: unknown parameters are rejected with the accepted
  list before any request leaves the process (typos were the most likely source
  of user-reported breakage).
- **Privacy section** in the README and a no-telemetry note in the dashboard
  footer: no analytics, no identifiers, no data collection, and nothing to opt
  out of.

### Changed

- The README explains storage as **one layer, two providers** (R2, TeraBox) plus
  the 9Drive-style pooling pattern, and rewrites the agent-runtime table in
  plain language with Arena as the default.
- `kollektiv bootstrap` and `.env.example` lead with the Arena login flow.

### Fixed

- `login`/`logout`/`accounts` now use the configured database
  (`db.models.bind_engine`) instead of whatever engine happened to be installed
  — the same class of bug fixed earlier for `init-db`/`bootstrap`.


## [0.3.0] — 2026-10-06 — “connectors”

Kollektiv can now reach the services around the project (mail, calendar, docs,
notes, chat, arbitrary APIs) and the README was rewritten as a professional
front page with a self-hosting checklist, a release policy and a sizing plan.

Until 1.0 every release is published as a **beta pre-release**
(`v0.3.0-beta.N`) because the configuration and HTTP surface still evolve.

### Added

- **Connector framework** (`src/connectors/`): every service exposes typed
  *actions* (name, description, parameters, `dangerous` flag) through one
  registry the brain, HTTP API, MCP server and CLI all share.
  - `github`: commits, diffs, PRs, issues, file/tree reads, PR comments.
  - `google`: Gmail search/read/send, Calendar list/create and Drive
    search/export over a single OAuth refresh token (cached in memory and in
    the encrypted token store).
  - `notion`: search, page/database reads, page creation and block appends.
  - `webhook`: broadcasts run events to `EVENT_WEBHOOKS` — Slack, Discord,
    n8n, Activepieces, Zapier or anything HTTP.
  - `rest`: declarative `CUSTOM_CONNECTORS` — any JSON API becomes a tool with
    one config entry (path placeholders, query/body params, bearer/header/query
    auth), no code.
- **Surfaces**: `GET /connectors`, `POST /connectors/{name}/call` (with
  `confirm` for dangerous actions), MCP tools `list_connectors` /
  `call_connector`, and `kollektiv connectors` / `kollektiv call`.
- **Run event broadcast**: run summaries are POSTed to `EVENT_WEBHOOKS` after
  every orchestrated run, alongside the Resend email.
- **Dashboard connectors panel**: lists services, status and actions, and runs
  safe actions with a JSON parameter prompt.
- **`examples/aider_shim.py`**: wraps any CLI coding agent (Aider, OpenHands,
  Codex CLI, OpenCode, Qwen Code, …) behind an HTTP worker endpoint.
- **README** rewritten: badges, "what it is / is not", connectors gallery,
  agent-runtime table, self-hosting checklist, beta release policy, performance
  plan and a FAQ covering self-hosting and worker alternatives.

### Changed

- `kollektiv bootstrap` also reports Google, Notion and event-webhook status.
- `.env.example` documents the connector block (78 keys total).
- CI type-checks `examples/`; mypy covers 48 files.

### Fixed

- `redacted()` now masks `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`,
  `NOTION_TOKEN`, `CUSTOM_CONNECTORS` and `EVENT_WEBHOOKS`.
- `CUSTOM_CONNECTORS` entries with an unknown `auth` fail at build time with
  the allowed list instead of at the first call.
- A rejected GitHub token (or any other `KollektivError`) no longer aborts a
  sync pass: the failure is recorded in the summary and surfaced in `/health`.
- `tests/test_orchestrator.py` no longer talks to the real GitHub API (it
  passed locally only because this sandbox blocks egress and failed anywhere
  else, including CI); the fake orchestrator now installs a mock transport.

## [0.2.0] — 2026-10-06 — “free stack”

Kollektiv now runs end to end on free tiers, with no paid service on the
critical path, and ships the dashboard that makes it usable without a shell.

### Added

- **Cloudflare R2 shared storage** (`src/storage/r2_client.py`,
  `src/storage/r2_pool.py`): buckets/accounts pooled into one drive, health- and
  free-space-based routing, cooldowns, usage reporting, presigned download URLs.
- **Dependency-free AWS SigV4 signer** (`src/utils/sigv4.py`): pure-stdlib
  signing and presigning, byte-identical to `botocore` (vectors pinned and
  cross-checked in `tests/test_r2.py`).
- **Storage factory** (`src/storage/factory.py`): `STORAGE_BACKEND=auto` picks
  R2 → TeraBox → local workspace, so the same code runs with or without keys.
- **Clerk authentication** (`src/api/auth.py`): RS256 JWT verification against
  Clerk's JWKS, `azp` allow-list, `__session` cookie support, an auth middleware
  with public paths, `/auth/me`, and Svix-verified `/webhooks/clerk`.
- **Resend notifications** (`src/utils/resend_client.py`): run summaries,
  failures and operational alerts, sent after every orchestrated run and
  reported in `/health`.
- **Neon/Postgres support**: `DATABASE_URL` normalisation
  (`postgres://` → `postgresql+psycopg://`, `sslmode`), pooling knobs and the
  `[postgres]` extra.
- **Static dashboard** (`web/`): projects, tasks, agents, storage quota and
  health, deployable free on Cloudflare Pages or GitHub Pages
  (`.github/workflows/pages.yml`).
- **`kollektiv bootstrap`**: creates the schema/workspace and prints the exact
  free-tier checklist with your current status.
- **Release automation**: tag-triggered workflow that builds the sdist/wheel,
  validates that `CHANGELOG.md` documents the version, and publishes the GitHub
  release with artifacts.
- Tests for all of the above (`tests/test_r2.py`, `tests/test_integrations.py`)
  and a root `LICENSE` (MIT) plus this changelog.

### Changed

- `STORAGE_BACKEND` selects storage explicitly; without it, the factory still
  degrades to TeraBox or the local workspace.
- The database engine is bound through a single `bind_engine()` helper, so the
  orchestrator, the CLI and `init-db` all honour the configured `DATABASE_URL`.
- `GET /projects/{id}/files` entries and the new `/url` route expose presigned
  links when the backend supports them.

### Fixed

- The CLI could not be driven from inside a running event loop
  (`asyncio.run` inside a loop) — it now runs the coroutine in a worker thread.
- `kubernetes`-style `postgres://` URLs are normalised for SQLAlchemy.
- Webhook signature rejections are logged with the delivery id and event type.

## [0.1.0] — 2026-10-05

Initial public release: the orchestrator (brain, planner, dispatcher, collector,
sync engine), the pooled worker agents, TeraBox shared storage, the FastAPI/MCP
interfaces, the CLI, Docker Compose deployment and the hermetic test suite.

[Unreleased]: https://github.com/HackerxBots/Kollektiv/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/HackerxBots/Kollektiv/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/HackerxBots/Kollektiv/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/HackerxBots/Kollektiv/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/HackerxBots/Kollektiv/releases/tag/v0.1.0
