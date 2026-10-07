# Changelog

All notable changes to Kollektiv are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html), and every release
on GitHub links to the section below for its version.

## [Unreleased]

### Added

- **Live project stream** — `GET /projects/{id}/events/stream` speaks Server-Sent
  Events: an `event: state` frame whenever the project's status, tasks, files,
  history or queue changes, `: keep-alive` heartbeats otherwise, a 0.5–30 s
  `interval` parameter, `Cache-Control: no-cache` and `X-Accel-Buffering: no` so
  reverse proxies do not buffer it. The dashboard's project drawer now updates
  without polling.
- **Multi-file dashboard** — `web/index.html` + `web/assets/styles.css` +
  `web/assets/app.js` + `web/assets/favicon.svg`: four views (overview,
  projects, connectors, agents & storage), a project drawer with the live task
  table and artifact links, a command palette (⌘K / Ctrl-K) over commands *and*
  projects, a confirmation dialog before dangerous connector actions, toasts,
  skeletons, hash routing and a 30-second background refresh. No build step, no
  framework, no CDN, no web fonts, no analytics; dark first, light via
  `prefers-color-scheme` or the persisted toggle, `prefers-reduced-motion`
  respected, and hover/active/focus-visible states on every control.
  `docs/ui-prompt.md` is the prompt of record and `web/README.md` documents the
  Cloudflare Pages (or GitHub Pages) deployment.
- **Community health files** — `SECURITY.md` (supported versions, private
  reporting through GitHub advisories, threat model, operator hardening
  checklist), `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1 with a 4-step
  enforcement ladder), `CONTRIBUTING.md` (quick start, the three gates, six
  non-negotiables, connector recipe, release checklist),
  `.github/ISSUE_TEMPLATE/{bug_report,feature_request,question}.yml` +
  `config.yml`, and `.github/pull_request_template.md`.
- **Freebuff worker shim** (`examples/freebuff_shim.py`) — runs the free,
  ad-supported [Freebuff](https://freebuff.com) agent as a Kollektiv worker.
  Two things make it more than a wrapper: `strip_ansi`/`strip_ads` keep the
  free tier's terminal ads and escape codes out of every artifact (fenced
  blocks are never touched, long lines are never cut), and `collect_changes`
  turns `git status` into Kollektiv's fenced-block contract so the worker's
  answer is *the files it changed*, with paths validated by
  `src/utils/paths.py` and binaries/oversized files reported instead of
  inlined. Git reads are retried (index-lock contention); the agent itself is
  never retried, because each run edits the tree. 19 tests in
  `tests/test_freebuff_shim.py` cover ad filtering, the artifact contract, the
  binary/oversize/deleted cases, both prompt-delivery modes, the timeout and
  the authenticated endpoints. **Documented caveat:** Freebuff's terms (as
  reported by reviewers) expect an operator to stay present, so the README
  presents it as a supervised single worker, not an unattended fleet.
- **Maintainer checklist** (`docs/repo-settings.md`) — the copy-paste
  repository description and topic list, the security toggles to enable
  (private vulnerability reporting, Dependabot alerts and security updates, code
  scanning → *Advanced* so it keeps the shipped `codeql.yml`), a suggested
  ruleset for `main`, the Pages sources, and why `SECURITY.md`/`codeql.yml` only
  take effect on the default branch. It also records which repository settings a
  repository-scoped token cannot change (`403 Resource not accessible by
  integration`) so nobody hunts for an API call that does not exist.
- **Automated hygiene** — `.github/workflows/codeql.yml` (CodeQL
  `security-and-quality` on pull requests, `security-extended` weekly, and
  `security-events: write` so code-scanning alerts light up) and
  `.github/dependabot.yml` (weekly grouped pip updates, GitHub Actions, monthly
  Docker) with `chore(deps)`/`chore(ci)` commit prefixes.
- **Tests** — `tests/test_dashboard.py` (6 tests) keeps the UI honest: every
  endpoint the JavaScript calls must exist in the OpenAPI schema with the method
  it uses, the SSE consumer must point at a registered route, the UI must stay
  split across files (no inline `<style>`/`<script>`), the interactive components
  must define hover/focus/reduced-motion states, and no tracker, CDN or
  third-party origin may appear. `tests/test_api.py` adds two SSE tests
  (state frame + faithful copy of `/status`, and an `event: error` frame for an
  unknown project). `tests/test_path_safety.py` covers the path rules end to end,
  from the helpers to the API's `400`.

- **Five optional chat/work connectors** — `telegram`, `discord`, `slack`,
  `linear` and `whatsapp`, taking the registry to **9 connectors / 41 actions**.
  Each is registered but inert until credentials exist, so nothing that worked
  before needs a new variable:
  - **Telegram** (`get_me`, `get_updates`, `send_message`, `send_document`) —
    Bot API with `{ok, result}` unwrapping, so an HTTP 200 error is still an
    error.
  - **Discord** (`get_me`, `list_channels`, `send_message`, `send_webhook`) —
    bot token *or* an incoming webhook alone; messages are chunked at Discord's
    2000-character limit.
  - **Slack** (`auth_test`, `list_channels`, `post_message`, `send_webhook`) —
    bot token *or* a webhook; the `{"ok": false, "error": …}` envelope Slack
    returns with HTTP 200 is treated as a failure, and long messages are chunked
    at 3000 characters.
  - **Linear** (`viewer`, `list_teams`, `list_issues`, `create_issue`,
    `comment_issue`) — GraphQL with errors surfaced instead of dropped, and an
    out-of-range `priority` refused rather than silently clamped.
  - **WhatsApp** (`status`, `send_message`) with **two backends**: Meta's
    official Business Cloud API, and the **OpenClaw-style linked-device bridge**
    (Baileys / `whatsapp-web.js`, paired once by QR) that the project asked for.
    The bridge is **opt-in** (`WA_ALLOW_UNOFFICIAL=true`), because automating a
    personal number breaks Meta's terms and can get it banned — until the flag
    is set the connector reports `not configured` and says exactly why. Long
    messages are chunked rather than truncated. Gmail and Drive stay covered by
    the existing `google` connector.
- **The MCP gateway** (`src/gateway/`, `docs/gateway.md`) — the thing
  `docs/monetization.md` §3 promised: `kollektiv gateway serve` publishes one MCP
  endpoint (`GATEWAY_MCP_PATH`) plus a small REST surface (`GET /toolkits`,
  `POST /call`, `GET /audit`, `GET /health`) with
  - **per-client tokens** (`kgw_…`, generated with `secrets.token_urlsafe`,
    encrypted at rest in `TokenStore`, shown once, rotated or revoked by the
    CLI) — `kollektiv gateway init|clients|revoke`;
  - **per-client policies** — `read-only`, `dashboard`, `worker`, `messenger`,
    `admin` presets plus `allow`/`deny`/`confirm` globs, overridable from a JSON
    file (`GATEWAY_POLICY_PATH`) so permissions can be reviewed in git. Empty
    `allow` denies everything: a policy that fails open is not a policy;
  - a **namespaced catalogue** (`projects.*`, `storage.*`, `agents.*`,
    `sync.*`, `connectors.<service>.<action>`, `gateway.*`), with dangerous
    tools marked so `read_only` and `confirm` mean something;
  - a **local audit log** — client, tool, duration, result, argument **names**
    only, never values, never uploaded — readable with `kollektiv gateway audit`
    and clearable with `--clear`;
  - a new **`kollektiv gateway` CLI** (`status|init|serve|token|clients|revoke|
    policy|presets|tools|audit`) and the `kollektiv-gateway` entry point.
  `GATEWAY_ENABLED=false` by default: `kollektiv-mcp` and the HTTP API stay
  first-class, and the gateway is an addition, never a requirement.
- **`/health` and `kollektiv check` now describe the new surface** — the health
  report gained a `gateway` block, and `kollektiv check` reports the connector
  count, which connectors are configured, and where the gateway would listen.
  `build_check_report()` returns that report as data instead of a terminal
  scrape.
- **Tests** — `tests/test_gateway.py` (49 tests: policy precedence and fail-closed
  behaviour, token issue/rotate/revoke and the encrypted-store path, the audit
  log's argument-name guarantee, the catalogue, every REST route, the real MCP
  handshake behind the token wrapper, and the CLI lifecycle) plus 37 new
  connector tests (envelope failures, chunking, retries, the WhatsApp bridge
  gate), and `tests/test_budget.py` (21 tests: both config parsers, the
  estimate's arithmetic and verdicts, caps, the ledger's totals and its
  failure-is-a-warning behaviour, the CLI, and a real offline orchestrator
  refusing an over-budget run), `tests/test_perf.py` (7 performance tripwires)
  and three more dashboard tests for the PWA), plus `tests/test_agent_links.py`
  (15 tests: naming, the pool beyond four agents, the link lifecycle over HTTP,
  enforcement with reasons, persistence, the CLI and the MCP tools).
  **479 tests total.**

- **Budgets: estimate, cap, and record.** A run can no longer cost more than you
  expected without saying so first.
  - **`.kollektiv.yml`** (`kollektiv init-config`) carries per-project settings —
    `project.n_agents`, `project.max_concurrency`, `budget.max_usd`,
    `budget.warn_at`, `brain.provider`, `storage.backend` — read by the CLI, the
    API, the MCP server and the gateway. Parsed with PyYAML when installed and
    with a new **strict built-in subset reader** (`src/utils/yaml_subset.py`)
    otherwise; anchors, tags, block scalars and flow mappings are refused with a
    line number instead of guessed at, and a broken file is ignored *loudly*
    (`ProjectConfig.problems`) rather than stopping a run.
  - **`kollektiv estimate --project-id`**, **`kollektiv run --dry-run`** and
    **`GET /projects/{id}/estimate`** return a cost estimate — tasks, waves,
    brain/worker calls, tokens and dollars — computed from the plan, your
    configured prices (`BUDGET_PRICE_*`, workers free by default) and what the
    project has already spent. Every response says `estimate_only: true`.
  - **Caps that refuse before anything is dispatched**: `budget.max_usd` (per
    project, the file wins), `BUDGET_MAX_USD` and `BUDGET_DAILY_MAX_USD`
    (deployment-wide). Over a cap → `BudgetError` → CLI exit 3 with the three
    ways forward, API `402 Payment Required` with the numbers, or
    `--allow-over-budget` / `allow_over_budget` for an explicit override.
    `budget.warn_at` warns without blocking, because a warning that blocks is a
    cap.
  - **A local spend ledger** (`budget_ledger`, one row per project per day) with
    `kollektiv budget`, `GET /budget` and the `budget_report` MCP tool. Brain
    tokens are **measured** from the provider's `usage` block (now accumulated in
    `OrchestratorBrain`); worker tokens are estimates and the row says so.
    Tokens and dollars only — never prompts, files or identifiers.
  - `OrchestratorBrain.stats()` gained `tokens_in`/`tokens_out`; `/health` gained
    a `budget` block that reports configuration *without* querying (health must
    answer instantly); the MCP server gained `estimate_cost` and
    `budget_report`.

- **Performance, measured instead of guessed.** `scripts/benchmark.py` reports
  what the Python side of Kollektiv actually costs (estimator, config parsing,
  the connector registry, the 57-tool gateway catalogue, planning, the ledger,
  `/health`, CLI cold start), and `tests/test_perf.py` guards the shapes that
  matter: the estimator stays linear, `/health` never queries the ledger, the
  config parser stays cheap enough to run on every command. `docs/performance.md`
  puts the numbers next to the "should this be Rust?" question and answers it
  with the escalation ladder (concurrency → 3.14 free-threading → PyO3 for one
  profiler-named function → more processes → never a rewrite).
- **The dashboard installs as an app.** `web/sw.js` pre-caches the shell, serves
  navigations network-first and pages offline, and **never caches API
  responses**; the manifest gained an id, shortcuts and `display_override`; the
  sidebar shows an install button when the browser offers one and an Add to Home
  Screen hint on iOS. Tauri (a real desktop build, with the API as a sidecar) is
  written down as a recipe in `docs/performance.md#browser-or-desktop-app` —
  deliberately not built yet, because it needs signed builds for three platforms
  and a frozen Python per OS to be anything other than a toy.

- **Agents have names, and there is no four-agent ceiling.** Every worker gets a
  display name — the operator's `name` from `ARENA_ACCOUNTS` when given, otherwise
  a stable friendly one derived from the account id (`src/agents/names.py`), made
  unique within the pool. `GET /agents/status`, `/health` and the pool snapshot
  carry it, so a dashboard shows "Nova, Atlas, Vega…" instead of masked emails.
  The old `MAX_AGENT_COUNT: 12` (and the request bound that mirrored it) was a
  typo in spirit and is now 64, with the docs saying plainly that the pool itself
  is uncapped: add accounts, get agents.
- **Linking agents to connectors, as its own surface.** `GET/POST /links` and
  `DELETE /links/{link_id}`, `kollektiv links|link|unlink`, and the MCP tools
  `agent_links`, `link_agent`, `unlink_agent` record *grants*: which worker agent
  may use which connector. Deliberately **not** a second copy of a connector's
  actions — those stay in the registry, the CLI, the API and the gateway — because
  a dashboard that lists actions drifts from the registry and conflicts with it.
  A connector with no links is open (fresh installs and existing workflows are
  untouched); once it has links, a call that names an agent must come from a
  linked one (`403` with a reason), while operator surfaces stay unconstrained.
  Links live in the `agent_links` table, unique per `(agent_id, connector)`, and
  `GET /connectors` now carries `linked_agents` per connector so a UI can render
  link/unlink with no other calls.

### Changed

- **The dashboard got its colour back.** `web/` is now "aurora glass": four
  blurred, slowly drifting colour fields behind every surface (pure CSS
  gradients plus a grain layer so they never band), frosted panels with a 1px
  inner highlight (`backdrop-filter` at 16–30px), 14/22/32px radii and pill
  controls, a per-subsystem hue that follows each card into its icon, glow and
  progress bar, gradient headlines, and 3D tilt with a pointer spotlight on
  every card (±7°, off for coarse pointers and `prefers-reduced-motion`). The
  palette moves from muted slate to candy: grape, cyan, pink, lime, peach, sun,
  sky — in both themes.
- **Onboarding, mobile chrome and installability.** A three-step first-run
  overlay (what Kollektiv does → point it at your API → keyboard shortcuts) with
  a hue-rotating gradient tile, progress dots, a confetti finish and
  `kollektiv.onboarded` in `localStorage`; reopenable from "Show me around".
  Under 900px the sidebar becomes a slide-in panel, a floating frosted tab bar
  appears, and tables become cards with their labels preserved via `data-label`.
  A local web manifest makes the shell installable as a standalone app. Three
  new dashboard tests enforce the visual language, the onboarding/mobile hooks
  and the manifest.

### Added

- **The opt-in sponsor line, and the honest money page behind it.**
  `docs/monetization.md` is the full strategy: why there is no affiliate deal
  to chase with Claude Code or Codex, why we ship our own MCP server (and,
  next, a gateway) instead, exactly how Freebuff and the Claude Code spinner-ad
  networks earn (75% of the revenue to the developer, always-on versus
  installed), and which half of that we copy. In code:
  `src/sponsors/catalog.py` (a catalogue from a file or a signed HTTPS URL, with
  a validator that refuses copy impersonating the tool, the model or an error),
  `src/sponsors/line.py` (one labelled line, only in dead time -- `waiting`,
  `between-tasks`, `rate-limit` -- at most once per `SPONSOR_MIN_INTERVAL_SECONDS`,
  and `None` before any network call unless `SPONSORS_ENABLED=true`),
  `src/sponsors/ledger.py` (a local per-sponsor tally in millicents with a
  7500-bp default share, plus HMAC-SHA256 *claims* that the operator sends by
  choice). New surfaces: `GET/POST /sponsors/*`, the `sponsor_line` and
  `sponsor_ledger` MCP tools, and `kollektiv sponsors
  status|catalog|line|ledger|claim|verify|enable|disable|forget`. No prompt, no
  code, no project and no identity is ever an input; nothing leaves the machine
  unless a human sends a claim. 48 tests in `tests/test_sponsors.py`.
  This is off by default and unrelated to running Kollektiv.

### Changed

- **Dead code removed, and the type gate widened to the tests.** `vulture` at
  100 % confidence plus a hand review of every 60 % hit deleted an unused
  `absolute` parameter from `GitHubClient._request`, the write-only
  `AgentPool._dispatch_counter` and the unused `JSONColumn = SAJSON` alias;
  `signature_raw` became `_signature_raw` to say out loud that the unverified
  decode path ignores it. Deliberately *kept* as live-but-dynamic: FastAPI route
  handlers, MCP tool functions, pydantic settings fields. `examples/` is now a
  package so `mypy` can check the tests too — the gate is
  `mypy src config examples scripts tests` (71 files) and the 24 type errors it
  found in six test files are fixed.

### Security

- **Path validation for every identifier that becomes a path** — new
  `src/utils/paths.py` (`safe_path_segment`, `safe_relative_path`) validates
  project ids in the state manager and the artifact uploader, plus the project id
  *and* file path of `GET /projects/{id}/files/{path}/url`. Traversal segments,
  absolute paths, separators, control characters and over-long values raise
  `ValueError`, and a new API-level handler answers those with `400` instead of a
  `500`. 39 tests in `tests/test_path_safety.py`.
- **No internal messages reach clients** — `GET /health`, unexpected failures in
  the project event stream and the presigned-URL route now log with
  `exc_info=True` and answer with a generic message (plus the exception class
  name where that is useful), so a stack trace or an internal path cannot leak
  through an error response.
- **CodeQL baseline documented** in `SECURITY.md`: the first run reports 43
  findings, what was fixed is listed, and the rest — a custom validator that
  CodeQL cannot prove, the CLI printing a freshly generated `SECRET_KEY` to the
  operator's own terminal, the example shim running the command the operator
  configured — is written down with the reason to type when dismissing it.

### Fixed

- **`pip install kollektiv` installed a broken package.** `src/connectors` was
  missing from `[tool.setuptools] packages`, so the wheel failed at import with
  `ModuleNotFoundError` — invisible in CI because the test job uses an editable
  install (`pip install -e .`) which reads the source tree directly. The wheel
  smoke step in CI had also swallowed the failure with `|| true`.
- **The dashboard was missing from the wheel and the image.** `web/` is now
  packaged (`packages` + `package-data` for the wheel, `MANIFEST.in` for the
  sdist) so `create_app()` finds it at `<site-packages>/web` after a real
  install, and the Dockerfile copies it to `/app/web` for the same reason. Both
  were verified by installing the built wheel in a clean environment and serving
  `/ui/`, `/ui/assets/*` and `/health` from it.
- **Deprecated packaging metadata** — `project.license` is now the PEP 639 SPDX
  string (`license = "MIT"` + `license-files`), the license classifier is gone
  and the build requirement moved to `setuptools>=77`. The build is warning-free.

### Added

- **Deployment smoke test** — `scripts/smoke.py` runs ten checks against any
  deployment (health, the bundled dashboard, the four read surfaces the UI uses,
  project planning, the state document, the resume briefing, one SSE frame and a
  `kollektiv check`), prints a table or `--json`, and exits `0`/`1`/`2` so a
  supervisor can tell "not deployed" from "deployed but broken". 16 tests cover
  the failure directions (missing dashboard, empty plan, wrong payload shape,
  unreachable host, token forwarding).
- **`docs/deploy-checklist.md`** — the order of operations for going live:
  accounts to create with **which ones have a CLI** (`wrangler`, `neon`, `clerk`,
  `resend-cli`, `cloudflared`, `gh`, `ollama`), which hosts genuinely still have a
  free tier in 2026 and which fit an always-on webhook/cron process, the first
  real project, the smoke test, and the known limits.
- **Packaging tests** (`tests/test_packaging.py`) — every source package must be
  declared, the dashboard must ship, the three console scripts must exist, the
  metadata must stay modern, and the CI smoke step must not swallow failures.

### Changed

- **The README is a front page, and `docs/` is the manual.** At 1 300 lines the
  README had become the manual: it now carries the pitch, a feature table, the
  quickstart and links (in the shape large projects like OpenHands use), and the
  depth moved into pages that each link back — `docs/architecture.md`,
  `configuration.md`, `connectors.md`, `deployment.md`, `agent-runtimes.md`,
  `api.md`, `operations.md`, `extending.md`, `development.md`, `faq.md`,
  `roadmap.md`, `privacy.md`, `legal.md`, `why-kollektiv.md`, plus a
  `docs/README.md` hub. Nothing was dropped: the moved sections are byte-for-byte
  the same text, with every relative link and anchor rewritten.
  `tests/test_docs.py` keeps it that way: the README has a line budget, every
  local link and `file.md#anchor` must resolve to a real file or heading, every
  page must be listed in the hub, and the long-form sections must not creep back
  into the README.
- **Badges and links follow the split** — the release workflow's beta banner now
  points at `docs/operations.md`, the pull-request template and `CONTRIBUTING.md`
  ask for the *matching page* in `docs/` rather than a bigger README, and
  `CLAUDE.md` documents the docs map so the split survives future edits.
- **Beta releases are published as normal GitHub releases.** Tags stay
  `vX.Y.Z-beta.N` and the release notes open with a beta warning, but the release
  is no longer marked as a GitHub *pre-release* — pre-releases are skipped by the
  sidebar's "Latest" widget, which made every release hard to find. The beta note
  now links to the release policy with an absolute URL, so it works from forks
  too.
- `README.md`, `CLAUDE.md` and `CONTRIBUTING.md` document the new dashboard file
  split, the SSE endpoint, the path validation, the CI gates
  (`mypy src config examples`) and the current test count; the duplicated
  "Health and observability" heading in the README is gone.

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
