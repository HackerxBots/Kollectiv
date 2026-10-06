# Changelog

All notable changes to Kollektiv are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html), and every release
on GitHub links to the section below for its version.

## [Unreleased]

_Nothing yet — open a PR and add a line here._

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

[Unreleased]: https://github.com/HackerxBots/Kollektiv/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/HackerxBots/Kollektiv/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/HackerxBots/Kollektiv/releases/tag/v0.1.0
