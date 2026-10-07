# CLAUDE.md

Guidance for AI coding agents working in this repository.

## What this project is

Kollektiv is a **multi-agent collaborative dev team orchestrator**, designed to
run entirely on free tiers. One cheap LLM (the *brain*) plans and reviews work;
a pool of worker agents executes subtasks in parallel; Cloudflare R2 (or pooled
TeraBox accounts) holds the shared `PROJECT_STATE.md`; GitHub is the real-time
source of truth for what actually landed. Neon can serve the database, Clerk the
auth, Resend the notifications and Cloudflare Pages the dashboard — none of them
required, all of them free.

Pipeline: `brieF → planner → dispatcher (waves) → agents → collector → review →
state/GitHub sync`.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -q                      # 303 hermetic tests, ~11 s
pytest tests/test_api.py -q    # one module
ruff check .                   # lint (clean)
mypy src config examples       # types (clean)

kollektiv bootstrap            # schema + workspace + free-tier checklist
kollektiv connectors           # services the agents can call (+ what is missing)
kollektiv check                # what is configured / degraded
kollektiv serve-api            # uvicorn src.api.routes:app --port 8000
python -m src.api.mcp_server   # MCP tools, port 8001
```

Tests are fully offline: mocked `httpx` transports, in-memory SQLite, fakes in
`tests/conftest.py`. Never add a test that needs a real credential.

## Architecture map

| Path | Responsibility |
| --- | --- |
| `config/settings.py` | The single `Settings` object (pydantic-settings). Every module takes it as an optional argument and falls back to `get_settings()`. |
| `src/utils/errors.py` | Error hierarchy. **Permanent** errors (`ConfigurationError`, `AuthenticationError`, `GitHubError`, …) vs **transient** (`*TransientError`, subclasses of `RetryableError`). Only transient ones are retried. |
| `src/utils/retry.py` | `async_retry` / `sync_retry` with exponential backoff, `retry_on=` / `exclude=` filters and `KOLLEKTIV_RETRY_BASE_DELAY` / `_MAX_DELAY` overrides. |
| `src/utils/token_store.py` | Fernet-encrypted tokens in SQLite. All credentials go through it; nothing else touches tokens. |
| `src/utils/sigv4.py` | Dependency-free AWS SigV4 signing/presigning for R2. Verified against `botocore` in `tests/test_r2.py` — change it only with those vectors green. |
| `src/utils/resend_client.py` | `ResendNotifier`: run summaries, alerts, run-completion hooks. Never raises into the orchestrator. |
| `src/api/auth.py` | Clerk `ClerkVerifier` (RS256 + JWKS), `install_auth` middleware, `verify_svix_signature` for `/webhooks/clerk`. |
| `src/db/models.py` | SQLModel tables + `session_scope()` / `init_db()`. Pass `db_url` (or `sqlite:///:memory:`) in tests. |
| `src/storage/` | `TeraBoxClient`/`R2Client` (one account) → `PoolManager`/`R2Storage` (many accounts as **one drive**, routed by free space + health) → `StateManager` (`PROJECT_STATE.md`). `factory.build_storage()` picks R2 → TeraBox → local. |
| `src/agents/` | `ArenaClient` (one worker over HTTP), `AgentPool` (scheduling/failover), `SessionManager` (APScheduler maintenance). |
| `src/github/` | `GitHubClient` (REST) + `webhook_handler` (HMAC-verified, background work). |
| `src/orchestrator/` | `brain` → `planner` → `dispatcher` → `collector` → `sync_engine`, wired by `app.Orchestrator`. |
| `src/orchestrator/handoff.py` | Resume briefings: dependency-aware next actions, blockers and rendered Markdown, written to `HANDOFF.md` after every run and served by `GET /projects/{id}/handoff` + the `get_handoff` MCP tool. |
| `src/connectors/` | `base.py` (Connector/ConnectorAction/ConnectorRegistry) + one module per service (GitHub, Google, Notion, webhooks, declarative REST). Every connector is always registered; `configured` decides what runs, and `dangerous` actions require `confirm`. |
| `src/api/` | `routes.py` (FastAPI), `mcp_server.py` (MCP tools), `cli.py` (`kollektiv`). |
| `web/` | The static dashboard (`index.html` + `assets/`): no build step, no CDN, no telemetry. Served by the API at `/ui`, published by Cloudflare Pages or `pages.yml`. |
| `docs/` | The documentation set. `README.md` is a **front page**: pitch, features, quickstart, links — `tests/test_docs.py` enforces the budget and checks every local link and `file.md#anchor` in `README.md` and `docs/`. Depth goes in the matching page (`architecture`, `configuration`, `connectors`, `deployment`, `agent-runtimes`, `api`, `operations`, `faq`, …). |

## Conventions

- **Everything async.** All I/O is `await`ed; use `httpx.AsyncClient` only.
- **Never crash silently.** Catch, log with context, and either degrade or raise
  a typed error. `Orchestrator.start()` collects `config_warnings()` instead of
  refusing to boot.
- **Module docstring + type hints on every function.** No placeholders.
- **Retry external calls** (3 attempts, exponential backoff). Pass
  `exclude=(RateLimitError,)` for clients that must fail over instead of
  sleeping on a 429 — the pool/brain own that decision.
- **Degrade, don't die.** Missing brain key → heuristic planner/reviewer.
  Missing R2/TeraBox → local file fallback. Missing GitHub → sync reports the
  error. `/health` and `kollektiv check` must always describe what is degraded.
- **Never add telemetry.** No analytics, crash reporting, install IDs, or
  "anonymous usage" pings — not in the API, the CLI or the dashboard. The only
  outbound requests are the endpoints the operator configured; the README's
  privacy section states this and the dashboard footer repeats it.
- **Credentials go through `TokenStore`.** `kollektiv login` writes them to the
  encrypted database (via `db.models.bind_engine()`, so the *configured*
  database is used), never to `.env` and never to a log.
- **Secrets never reach logs.** Use `mask_email` / `redact_token` /
  `Settings.redacted()`.

## Gotchas

- `data/` holds the dev SQLite DB **and** `data/workspace`. Tests must not read
  it: build `Settings(_env_file=None, ...)` copies and pass an in-memory
  `db_url`.
- Task ids are project-local: the `tasks` table is keyed by `(id, project_id)`
  and `session.get(Task, ...)` takes a tuple **in that order**. The
  dispatcher's task persistence warns (never silently drops) on failure.
- An `Orchestrator(settings=...)` rebinds the global database engine when its
  `DATABASE_URL` differs from the installed one (`db.models.bind_engine()`, also
  used by the CLI); tests keep their pre-seeded in-memory engine because
  matching engines are reused. `init-db`/`bootstrap` go through the same helper
  — never call `get_engine()` directly for a configured database.
- The CLI must stay runnable from inside a running event loop (notebooks,
  embedders): async commands go through `cli._run_async()`, never raw
  `asyncio.run()`.
- New free-stack interfaces are covered in `tests/test_integrations.py` (Clerk
  RS256/JWKS/Svix, Resend, settings helpers, bootstrap, dashboard); R2 in
  `tests/test_r2.py`; connectors in `tests/test_connectors.py` (MockTransport
  clients + a dict token store). Keep them hermetic — no real accounts in CI.
- The dashboard is a static multi-file site (`web/index.html` +
  `web/assets/{styles.css,app.js,favicon.svg}`) — **never collapse it back into
  a single HTML file**, and keep hover/focus/reduced-motion states in the CSS.
  `tests/test_dashboard.py` parses the shipped JS and fails when it calls an
  endpoint that is not in the OpenAPI schema, when an inline `<style>`/`<script>`
  appears, or when a tracker/CDN origin shows up. `docs/ui-prompt.md` is the
  prompt of record; update it with the assets.
- Every value that becomes part of a filesystem or bucket path (`project_id`,
  account id, `file_path`) goes through `src/utils/paths.py` — never join a raw
  request value onto a directory or a bucket prefix. `ValueError` from those
  helpers is a client error: the API answers `400` (global handler in
  `src/api/routes.py`), and `tests/test_path_safety.py` pins the rules.
- **A new subpackage must be added to `[tool.setuptools] packages`** in
  `pyproject.toml`. `src/connectors` once shipped missing, which made
  `pip install kollektiv` fail at import while CI stayed green (it installs
  editable). `tests/test_packaging.py` checks the list against the tree and
  the CI `package` job installs the built wheel and asserts `/ui` is mounted.
- Static assets that the runtime reads (`web/`) ship via `package-data` and
  `MANIFEST.in`, and the Dockerfile copies them too — the image runs uvicorn
  from the source tree, where the lookup resolves to `/app/web`.
- Errors that reach a client are generic: log the exception with
  `exc_info=True`, answer with a fixed message (an exception *class* name is the
  most detail that goes out). CodeQL watches for `py/stack-trace-exposure`.
- `examples/*_shim.py` wrap CLI coding agents as workers. They must stay
  supervised-friendly: no shell, prompt as a single argv element (or stdin),
  ads/ANSI stripped, and Kollektiv's fenced-block contract returned — never a
  raw transcript when the agent edited files. `tests/test_freebuff_shim.py`
  pins that behaviour.
- Live updates use Server-Sent Events (`GET /projects/{id}/events/stream`,
  `event: state` when the project's state digest changes, `: keep-alive`
  otherwise). Note for tests: `httpx.ASGITransport` buffers whole responses, so
  a streaming endpoint must be driven through the route function (see
  `tests/test_api.py::test_project_event_stream_emits_state`).
- Connectors must never raise into the orchestrator: report in `status()`,
  log, and keep read actions safe. Mark anything that sends/creates/comments as
  `dangerous=True` and let the registry enforce `confirm`.
- `mcp` has two API generations. `src/api/mcp_server.py` adapts to both
  (`FastMCP` in v1, `MCPServer` in v2). SDK v2's `server.call_tool(...)`
  returns a `CallToolResult` (read `.content[0].text`), not a JSON string.
- The agent-pool `account_id` is `sha256(email)[:16]`; logs mask emails, so
  assertions must use `account_id`.
- Worker output contract: fenced blocks tagged with a path
  (` ```python path=src/app.py `). Untagged fences are snippets and are never
  written to disk — `src/orchestrator/collector.py` enforces path safety.
- FastAPI wraps included routers (0.142+); enumerate routes via
  `app.router.routes` + `getattr(route, "path", None)` or `app.openapi()`.
- TeraBox upload-shard details differ between API revisions; the uncertain
  spots are marked with `TODO` in `src/storage/terabox_client.py` (rule: keep
  the TODO until verified against a live account).

## Definition of done for a change

1. `pytest -q` green (add tests alongside behavior changes).
2. `ruff check .` and `mypy src config` clean.
3. `README.md` / this file updated if interfaces, config keys or commands moved.
4. Commit with a `feat:` / `fix:` / `test:` / `docs:` prefix.

### Release checklist (README + releases stay current)

Every change that alters behaviour also updates the front page and the log — the
project is judged by its README and its releases, so they ship together:

1. Bump `version` in `pyproject.toml` **and** `src/__init__.py` (SemVer).
2. Move `CHANGELOG.md`'s `[Unreleased]` entries under the new version with
   today's date; add the compare links at the bottom. **Every release is a
   beta for now**: tag `vX.Y.Z-beta.N`; the workflow adds the beta banner and
   publishes it as a normal release so it stays findable (see the policy in
   `README.md`).
3. Update the README: the "peak" block and badges at the top (status, test
   count) plus, for anything deeper, the matching page in `docs/` — the README is
   a front page and `tests/test_docs.py` fails if it grows past its budget.
4. Tag `v<version>` and push the tag — `.github/workflows/release.yml` builds
   the artifacts, refuses to publish when the changelog lacks the version, and
   creates the release with notes from `CHANGELOG.md`.
5. Never rewrite a published tag; cut a patch release instead.
