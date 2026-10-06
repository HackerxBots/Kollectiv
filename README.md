# Kollektiv

**A multi-agent collaborative dev team orchestrator.**

Kollektiv turns a project brief into a working repository by coordinating a
team of AI worker agents. A cheap LLM plans and reviews the work, the workers
write the code in parallel, every artifact is archived to shared cloud storage,
and GitHub is the real-time source of truth for what has actually landed.

```
                        ┌───────────────────────────────┐
   project brief ──────►│  Brain (DeepSeek / Groq / …)  │  plans, reviews,
                        └───────────────┬───────────────┘  summarises
                                        │ subtasks + context
                        ┌───────────────▼───────────────┐
                        │  Planner → execution waves    │  dependency graph
                        └───────────────┬───────────────┘
                                        │
                        ┌───────────────▼───────────────┐
                        │  Dispatcher (bounded parallel)│
                        └───┬───────────┬───────────┬───┘
                            │           │           │
                   ┌────────▼──┐  ┌─────▼─────┐  ┌──▼────────┐
                   │ worker #1 │  │ worker #2 │  │ worker #N │  (AgentPool)
                   └────────┬──┘  └─────┬─────┘  └──┬────────┘
                            │           │           │  markdown + file blocks
                        ┌───▼───────────▼───────────▼───┐
                        │  Collector (parse, path-safe, │  conflict detection
                        │  merge, missing dependencies) │
                        └───┬───────────────────────┬───┘
                            │                       │
              ┌─────────────▼──────────┐   ┌────────▼────────────────┐
              │  TeraBox (pooled)      │   │  GitHub (commits, PRs,  │
              │  PROJECT_STATE.md,     │   │  webhooks, actions)     │
              │  artifacts, quota      │   └────────┬────────────────┘
              └─────────────┬──────────┘            │
                            └────────► SyncEngine ◄─┘
                            cron every CRON_INTERVAL_MINUTES + webhooks
```

- **Brain** – any OpenAI-compatible endpoint (`deepseek-chat` by default, Groq
  as the fallback). Splits briefs into subtasks, reviews worker output and
  compresses shared state into injectable context. Works without a key too:
  a deterministic planner/reviewer keeps the pipeline usable offline.
- **Worker agents** – a pool of accounts/endpoints that run in parallel. Each
  account is one worker; more accounts means more concurrency and resilience
  when one endpoint rate-limits.
- **TeraBox** – shared storage. Multiple accounts act as one drive, routed by
  free space, holding `PROJECT_STATE.md` and every artifact the team produces.
- **GitHub** – the real-time sync layer: pushes, pull requests, commit diffs,
  PR comments and file archiving all flow through it.

Everything is async, every external call is retried with exponential backoff,
and every credential is encrypted (Fernet) before it touches disk.

---

## Table of contents

1. [Quick start](#quick-start)
2. [Configuration](#configuration)
3. [How a run works](#how-a-run-works)
4. [The shared state document](#the-shared-state-document)
5. [Interfaces](#interfaces) — HTTP API, MCP, CLI
6. [Deployment](#deployment)
7. [Operations](#operations)
8. [Extending Kollektiv](#extending-kollektiv)
9. [Project layout](#project-layout)
10. [Development](#development)
11. [Troubleshooting](#troubleshooting)
12. [FAQ](#faq)
13. [Roadmap](#roadmap)
14. [Legal & responsible use](#legal--responsible-use)

---

## Quick start

```bash
git clone https://github.com/HackerxBots/Kollektiv.git
cd Kollektiv

python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env        # fill in your keys (see Configuration)
kollektiv secret            # paste the output into SECRET_KEY
kollektiv check             # shows exactly what is missing or degraded
kollektiv serve-api         # http://localhost:8000/docs
```

Your first project:

```bash
kollektiv run "Build a URL shortener: FastAPI service, SQLite storage, CLI and pytest tests" \
    --name shortener --agents 3
kollektiv status --project-id prj_…      # the shared state document
```

Or over HTTP:

```bash
curl -s localhost:8000/projects -H 'content-type: application/json' -d '{
  "name": "shortener",
  "description": "Build a URL shortener with FastAPI, SQLite and tests",
  "n_agents": 3
}'

curl -s -X POST localhost:8000/projects/<project_id>/run
curl -s localhost:8000/projects/<project_id>/status | jq '.tasks[] | {id, title, status}'
```

With Docker:

```bash
docker compose up --build      # API on :8000, MCP server on :8001
```

> **No credentials yet?** Kollektiv still boots: `/health` and
> `kollektiv check` list every degraded subsystem, the brain falls back to
> heuristics, storage falls back to the local workspace and GitHub sync
> reports itself as unconfigured. You can exercise the whole pipeline offline.

---

## Configuration

All settings come from `.env` (see `.env.example`, 51 keys with comments).
`kollektiv check --json` prints the resolved configuration plus warnings;
secrets are always redacted.

### 1. Worker agents (`ARENA_ACCOUNTS`)

A JSON list; each entry becomes one worker in the pool. Two request shapes are
supported and auto-detected:

```jsonc
// (a) OpenAI-compatible endpoint holding its own key
[
  {"name": "fast", "base_url": "https://api.groq.com/openai/v1",
   "model": "llama-3.3-70b-versatile", "session_token": "gsk_…"},
  {"name": "reasoner", "base_url": "https://api.deepseek.com/v1",
   "model": "deepseek-reasoner", "session_token": "sk_…"}
]

// (b) a custom chat endpoint (your own bridge, a self-hosted model, …)
[{"email": "w1@example.com", "session_token": "…",
  "base_url": "https://my-bridge.internal", "api_style": "custom"}]
```

The client picks the OpenAI shape when `base_url` ends in `/v1`, otherwise it
posts the custom envelope (`{"prompt", "agent_mode", "stream"}`) to
`ARENA_CHAT_PATH`. Related keys: `ARENA_LOGIN_PATH`, `ARENA_TIMEOUT`,
`ARENA_MAX_CONCURRENCY`, `ARENA_RATE_LIMIT_COOLDOWN`,
`ARENA_SESSION_LIFETIME_MINUTES`.

> **Use endpoints you are allowed to use.** Kollektiv does not scrape services,
> bypass paywalls or evade rate limits. Point the pool at API keys,
> self-hosted models or your own endpoints. When a provider answers `429`, that
> worker goes into cooldown and the task is routed to another account — the
> orchestrator never sleeps through a rate limit.

### 2. Shared storage (`TERABOX_ACCOUNTS`)

```json
[
  {"email": "box1@example.com", "password": "",
   "access_token": "…", "refresh_token": "…"},
  {"email": "box2@example.com", "refresh_token": "…"}
]
```

Tokens come from the TeraBox Open Platform OAuth flow (access tokens last ~2
days; the client refreshes them proactively, and `TokenStore` keeps them
encrypted in SQLite). Without user tokens, set `TERABOX_APP_ID` /
`TERABOX_APP_KEY` for the client-credentials grant.

Uploads are sharded (`precreate` → `shard` → `merge`, default 4 MiB blocks) and
downloads are streamed to disk. The pool routes each write to the account with
the most free space and remembers which account holds what, so reads and
deletes go straight to the owner. If TeraBox is unreachable the state document
falls back to a local cache and the run continues in degraded mode.

Related keys: `TERABOX_BASE_URL`, `TERABOX_REMOTE_ROOT`, `TERABOX_CHUNK_SIZE`,
`TERABOX_MAX_FILE_SIZE`.

### 3. GitHub (`GITHUB_TOKEN`, `GITHUB_REPO`, `GITHUB_WEBHOOK_SECRET`)

A fine-grained PAT with `contents`, `pull_requests` and `issues` write scope on
the target repository. `GITHUB_REPO` is `owner/repo`; the shipped
`owner/repo` placeholder is treated as *unconfigured* so nothing hammers the
API with doomed requests.

```bash
# Repository → Settings → Webhooks → Add webhook
#   Payload URL:  https://your-host/webhooks/github
#   Content type: application/json
#   Secret:       the same value as GITHUB_WEBHOOK_SECRET
#   Events:       pushes, pull requests, issues
```

Signatures are verified with HMAC-SHA256 (`X-Hub-Signature-256`) using a
constant-time comparison; unsigned or mismatched deliveries get `401` and are
logged. **Every request is verified before it is parsed**, including pings.

Related keys: `GITHUB_API_URL`, `GITHUB_DEFAULT_BRANCH`,
`GITHUB_PUSH_AGENT_OUTPUT`, `GITHUB_REQUEST_TIMEOUT`.

### 4. Brain (`BRAIN_*`)

```bash
BRAIN_PROVIDER=deepseek                 # deepseek | groq | openai | …
BRAIN_API_KEY=sk-…
BRAIN_MODEL=deepseek-chat
BRAIN_BASE_URL=https://api.deepseek.com/v1

BRAIN_FALLBACK_PROVIDER=groq            # used when the primary errors
BRAIN_FALLBACK_API_KEY=gsk_…
BRAIN_FALLBACK_MODEL=llama-3.3-70b-versatile
```

`BRAIN_TEMPERATURE`, `BRAIN_MAX_TOKENS` and `BRAIN_TIMEOUT` tune generation.
Without `BRAIN_API_KEY` the heuristic planner, reviewer and summariser take
over, which is what makes the test suite and offline runs possible.

### 5. Runtime

| Key | Default | Purpose |
| --- | --- | --- |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | HTTP API bind address |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8001` | MCP bind address |
| `MCP_TRANSPORT` | `sse` | `stdio`, `sse` or `streamable-http` |
| `CRON_INTERVAL_MINUTES` | `15` | Sync pass cadence |
| `CRON_ENABLED` | `true` | Turn the scheduler off for one-shot runs |
| `DATABASE_URL` | `sqlite:///./data/kollektiv.db` | SQLite (or any SQLAlchemy URL) |
| `WORKSPACE_DIR` | `./data/workspace` | Where collected files are written |
| `SECRET_KEY` | — | Fernet key for `TokenStore` (`kollektiv secret`) |
| `LOG_LEVEL` | `INFO` | `DEBUG`…`CRITICAL` |
| `ENVIRONMENT` | `development` | `development` / `production` |
| `CORS_ORIGINS` | `*` in development | Comma-separated allowed origins |
| `HTTP_SSL_VERIFY`, `SSL_CA_BUNDLE` | `true`, — | Corporate proxies / custom CAs |
| `AUTO_INIT_DB` | `true` | Create tables on startup |
| `MAX_RETRIES`, `RETRY_BASE_DELAY` | `3`, `1.0` | Retry policy for external calls |

---

## How a run works

1. **Plan** — the brain splits the brief into `n_agents`+ subtasks, each with an
   id, title, description, priority and dependencies. The planner turns that
   into execution *waves*: independent tasks run together, dependants wait.
   Dangling dependency ids are dropped, cycles are broken by removing the
   offending edge, and the critical path is reported.
2. **Dispatch** — for every wave the dispatcher builds per-agent context
   (mission, current state, this task, files that already exist, constraints,
   tactical advice from the brain) and asks the pool for a free worker. Tasks
   run with bounded concurrency; a worker that fails or rate-limits hands the
   task to the next one.
3. **Collect** — the collector extracts fenced code blocks tagged with file
   paths, writes them into the project workspace (path-traversal safe), flags
   placeholders, and merges everything into one artifact set with
   `file_count`, per-file producers, conflicts and missing dependencies.
4. **Review** — the brain scores each output. Below the threshold the task is
   retried once with the reviewer's feedback appended to the prompt;
   dependants of a failed task are marked `blocked` instead of being dispatched
   on top of a missing foundation.
5. **Record** — outcomes update SQLite, `PROJECT_STATE.md` (on TeraBox, with a
   local fallback) and the append-only event history.
6. **Sync** — every `CRON_INTERVAL_MINUTES`, and on every webhook, the sync
   engine pulls commits and PRs, summarises them with the brain, archives
   artifacts to TeraBox and pushes the refreshed context to the workers.

### Worker output contract

Agents are asked to answer with fenced blocks whose info string is the file
path:

````markdown
```python path=src/app.py
print("complete file contents")
```
````

Accepted forms: ` ```path=src/app.py ``, ` ```file=src/app.py `,
` ```src/app.py ` and ` ```python path=src/app.py ` (with or without extra
attributes). Blocks tagged with a language only are treated as snippets and are
**never** written to disk. Files that would land outside the project workspace
are rejected and reported. The prompt that asks for this lives in
`AgentPool.build_prompt()`; the parser lives in `Collector.collect_result()`.

### Failure semantics

| Situation | What happens |
| --- | --- |
| Worker returns unusable output | Review scores it low → one retry with feedback → `failed`, dependants `blocked` |
| Worker hits a rate limit | Worker cools down, task is reassigned to another account |
| Every worker fails a task | Task marked `failed`, run continues with the rest |
| TeraBox is down | State falls back to `data/workspace/<project>/PROJECT_STATE.md`; uploads are logged and skipped |
| GitHub is unconfigured | Sync reports `configured: false` and returns no commits instead of erroring |
| Brain is unconfigured | Heuristic plan/review/summary, everything else unchanged |

---

## The shared state document

`PROJECT_STATE.md` is the team's memory: every worker reads it before starting
a task, and every result is appended to it. Kollektiv both **renders** it (from
a state dict) and **parses** it, so a human can edit it by hand without
breaking the orchestrator.

```markdown
# Project State — shortener

- Updated: 2026-10-06T20:31:19+00:00
- Description: Build a URL shortener with FastAPI, SQLite and tests
- Last commit: abc1234
- Status: running

## Tasks

| id | title | status | assigned_agent | score |
| --- | --- | --- | --- | --- |
| t1 | Requirements and file layout | completed | a1b2c3d4e5f60718 | 0.85 |
| t2 | Core implementation | completed | 9f8e7d6c5b4a3210 | 0.90 |

## Agents

| account_id | status | tasks_done |
| --- | --- | --- |
| a1b2c3d4e5f60718 | idle | 2 |

## Files

- `src/app.py` (412 bytes) [a1b2c3d4e5f60718]

## History

- 2026-10-06T20:31:20+00:00 | planner | plan_created | 2 task(s) in 2 wave(s)
- 2026-10-06T20:31:41+00:00 | a1b2c3d4e5f60718 | task_completed | t1 scored 0.85
```

`StateManager.read_state()` returns `{project_name, description, status, tasks,
agents, files, last_commit, history, …}`; unknown sections written by humans
are preserved under `metadata.unparsed_sections` rather than dropped, and parse
problems are collected in `last_parse_errors`. `get_latest_context(agent_id)`
renders the prompt-injectable digest that each worker receives.

---

## Interfaces

### HTTP API

Run it with `kollektiv serve-api`, `kollektiv-api`, or
`uvicorn src.api.routes:app --port 8000`. Interactive docs at `/docs`.

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| `GET` | `/health` | — | `{status, subsystems, warnings}` — never fails when degraded |
| `POST` | `/projects` | `{name, description, n_agents}` | `201 {project_id, name, plan}` |
| `GET` | `/projects` | — | `{count, projects: [{project_id, name, status, …}]}` |
| `POST` | `/projects/{id}/run` | `?background=&max_concurrency=` | `{status, tasks_dispatched, completed, failed, artifact}` |
| `POST` | `/projects/{id}/replan` | `?dispatch=&max_new_tasks=` | `{revision, new_tasks, plan, results}` |
| `GET` | `/projects/{id}/status` | `?force=` | The state document as JSON |
| `GET` | `/projects/{id}/files` | — | `[{path, size, source, …}]` |
| `POST` | `/projects/{id}/upload` | `{file_path}` or multipart | `{path, size, url}` |
| `GET` | `/agents/status` | `?probe=true` | `{count, available, agents:[…]}` |
| `GET` | `/storage/status` | — | `{used_gb, free_gb, total_gb, per_account}` |
| `POST` | `/sync` | — | `{commits, prs, archived, errors, state_updated}` |
| `POST` | `/webhooks/github` | GitHub payload + HMAC header | `200`/`202`, or `401` when unsigned |

Error handling is uniform: `404` for unknown projects, `400` for configuration
problems (no agents configured, empty description) and `502` when an upstream
system fails. Everything is logged with the project id and the failing
subsystem.

### MCP server

`kollektiv serve-mcp`, `kollektiv-mcp`, or `python -m src.api.mcp_server`.
Supports `mcp` SDK v1 (`FastMCP`) and v2 (`MCPServer`).

| Tool | Arguments | Result |
| --- | --- | --- |
| `list_projects` | — | Projects with status and task counts |
| `get_project_status` | `project_id` | The shared state document |
| `create_project` | `name`, `description`, `n_agents` | New project + plan |
| `run_project` | `project_id`, `max_concurrency` | Run summary |
| `replan_project` | `project_id`, `dispatch` | Corrective plan |
| `list_files` | `project_id` | Stored artifacts |
| `upload_file` | `project_id`, `file_path` | TeraBox location + URL |
| `get_agent_pool_status` | `probe` | Worker pool snapshot |
| `get_storage_status` | — | Pooled quota |
| `trigger_sync` | — | Sync pass result |

```bash
python -m src.api.mcp_server --transport stdio                 # local clients
python -m src.api.mcp_server --transport sse --port 8001       # remote clients
python -m src.api.mcp_server --transport streamable-http       # 2025+ clients
```

Example client configuration (Claude Desktop style):

```json
{
  "mcpServers": {
    "kollektiv": {
      "command": "python",
      "args": ["-m", "src.api.mcp_server", "--transport", "stdio"],
      "env": {"DATABASE_URL": "sqlite:////absolute/path/data/kollektiv.db"}
    }
  }
}
```

### CLI

```
kollektiv check [--json] [--live]     validate configuration; --live probes the APIs
kollektiv init-db                     create the SQLite schema
kollektiv projects                    list projects
kollektiv plan "<brief>" --agents 3   plan without running
kollektiv run "<brief>"|--project-id  plan (if needed) and execute
       [--name] [--agents] [--max-concurrency] [--export-state PATH]
kollektiv status --project-id prj_…   print the shared state document
kollektiv sync                        one sync pass (GitHub → TeraBox → agents)
kollektiv secret                      print a fresh Fernet key for SECRET_KEY
kollektiv serve-api [--host] [--port] [--reload]
kollektiv serve-mcp [--transport] [--host] [--port]
```

`--log-level DEBUG` is available globally, and `kollektiv check --live` is the
fastest way to prove that tokens, buckets and the brain actually work.

---

## Deployment

### Docker Compose (recommended)

```bash
cp .env.example .env      # fill it in
docker compose up --build
docker compose logs -f kollektiv-api
```

Two services share one named volume:

| Service | Image | Port | Entrypoint |
| --- | --- | --- | --- |
| `kollektiv-api` | `kollektiv:latest` | `API_PORT` → 8000 | `uvicorn src.api.routes:app` |
| `kollektiv-mcp` | `kollektiv:latest` | `MCP_PORT` → 8001 | `python -m src.api.mcp_server --transport sse` |

The MCP service waits for the API healthcheck, so the database schema and the
workspace exist before it starts. SQLite and `data/workspace` live on the shared
volume (`kollektiv-data`) — back it up as a unit.

### Bare metal / VM

```bash
git clone … && cd Kollektiv
python -m venv /opt/kollektiv && /opt/kollektiv/bin/pip install .
install -m 600 .env /etc/kollektiv.env
```

`/etc/systemd/system/kollektiv-api.service`:

```ini
[Unit]
Description=Kollektiv API
After=network-online.target

[Service]
User=kollektiv
WorkingDirectory=/opt/kollektiv
EnvironmentFile=/etc/kollektiv.env
ExecStart=/opt/kollektiv/bin/uvicorn src.api.routes:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then a second unit for `kollektiv-mcp` with
`ExecStart=/opt/kollektiv/bin/python -m src.api.mcp_server --transport sse`.
Put nginx/Caddy in front for TLS — GitHub only delivers webhooks over HTTPS.

### Behind a proxy or a corporate CA

Set `SSL_CA_BUNDLE=/path/to/ca.pem` (or `HTTP_SSL_VERIFY=false` for a local
lab, never in production). Both the GitHub, TeraBox and brain clients honour
these settings through `src/utils/net.py`.

---

## Operations

### Health and observability

- `GET /health` — always `200`; lists subsystems, counts and configuration
  warnings so a supervisor can distinguish "running degraded" from "down".
- `kollektiv check --json` — the same information from a shell/cron.
- Structured logs (`LOG_LEVEL=DEBUG` shows request payloads with secrets
  redacted) plus an append-only `events` table: every dispatch, review, sync
  and webhook lands there with project id, agent id and result.
- `GET /agents/status?probe=true` actively pings each worker endpoint instead of
  reporting the cached snapshot.

### Background sync

The cron pass runs inside the API process (APScheduler), every
`CRON_INTERVAL_MINUTES`. Set `CRON_ENABLED=false` when you scale the API
horizontally so only one replica performs the sync, or run the CLI `sync`
command from an external scheduler (cron, Kubernetes CronJob).

### Storage hygiene

- `data/kollektiv.db` — projects, tasks, plans, events, encrypted tokens.
- `data/workspace/<project_id>/` — collected files for the local project; the
  TeraBox copy is authoritative, this is the working cache.
- Delete a project's artifacts with `GET /projects/{id}/files` plus the pool's
  `delete_file`, or remove the remote folder `/Kollektiv/<project_id>`.

### Scaling

| Symptom | Knob |
| --- | --- |
| Tasks queue behind each other | add `ARENA_ACCOUNTS`, or raise `ARENA_MAX_CONCURRENCY` |
| Storage fills up | add `TERABOX_ACCOUNTS` (the pool routes by free space) |
| Brain is slow/expensive | keep the cheap model for planning, set a larger `BRAIN_MODEL` only for reviews |
| Sync takes long | raise `CRON_INTERVAL_MINUTES` |

SQLite handles a single API process comfortably. For multiple replicas, move
`DATABASE_URL` to PostgreSQL (SQLModel is portable) and disable the in-process
scheduler.

---

## Extending Kollektiv

**A new worker shape.** Subclass `ArenaClient` and override `_build_request()`
(or point `base_url` at an OpenAI-compatible gateway) — the pool needs nothing
else. Anything that accepts a prompt and returns text can be a worker.

**A new brain provider.** Any OpenAI-compatible endpoint works by setting
`BRAIN_BASE_URL`/`BRAIN_MODEL`. For a different protocol, implement `complete()`
on a subclass of `OrchestratorBrain` and pass it to `Orchestrator(brain=…)`.

**A new storage backend.** `TeraBoxPoolManager` exposes
`upload_file/download_file/list_all_files/get_total_quota`; implement the same
four methods (S3, WebDAV, a local NAS) and pass it as `pool=`.

**A new tool.** Add a function decorated with `@server.tool()` inside
`create_server()` in `src/api/mcp_server.py` — the SDK generates the schema from
the type hints.

---

## Project layout

```
config/settings.py             pydantic-settings configuration + account parsing
src/utils/logger.py            structured logging (JSON optional), secret masking
src/utils/errors.py            typed errors: permanent vs transient (retryable)
src/utils/retry.py             exponential backoff with retry_on/exclude filters
src/utils/crypto.py            Fernet helpers, token/email redaction
src/utils/token_store.py       encrypted tokens in SQLite
src/utils/net.py               shared httpx client, TLS/CA configuration
src/db/models.py               SQLModel tables + session_scope()/init_db()
src/storage/terabox_client.py  OAuth, sharded uploads, streaming downloads
src/storage/pool_manager.py    multi-account routing by free space
src/storage/state_manager.py   PROJECT_STATE.md read/write/parse + agent context
src/agents/arena_client.py     one worker account (OpenAI + custom shapes)
src/agents/agent_pool.py       scheduling, failover, statistics, prompts
src/agents/session_manager.py  periodic session maintenance (APScheduler)
src/github/github_client.py    commits, diffs, trees, PRs, comments, branches
src/github/webhook_handler.py  HMAC-verified GitHub webhook endpoints
src/orchestrator/brain.py      planning, review, summarisation (+ fallbacks)
src/orchestrator/planner.py    dependency graph, waves, critical path, replanning
src/orchestrator/dispatcher.py wave execution, retries, blocked-task handling
src/orchestrator/collector.py  parse agent markdown, merge, conflict detection
src/orchestrator/sync_engine.pyGitHub ⇄ TeraBox ⇄ agents synchronisation
src/orchestrator/app.py        the Orchestrator that wires everything together
src/api/routes.py              FastAPI application + webhook router
src/api/mcp_server.py          MCP tool server (SDK v1 and v2)
src/api/cli.py                 the `kollektiv` command line interface
tests/                         113 hermetic tests (no network, no credentials)
```

---

## Development

```bash
pip install -e ".[dev]"

pytest -q                 # 113 tests, ~4 s, fully mocked
pytest tests/test_api.py -q
ruff check .              # lint (clean)
mypy src config           # types (clean)
```

### Design decisions

- **Typed errors, two retry classes.** Transport failures and 5xx responses are
  retried with exponential backoff; 4xx/API-level errors fail fast, and *rate
  limits are never retried inside a client* — the pool and the brain own
  failover so no request sleeps through a cooldown.
- **Graceful degradation everywhere.** Missing brain key → heuristics; missing
  TeraBox → local fallback file; missing GitHub → sync reports it and moves on.
  `/health` and `kollektiv check` always describe what is degraded.
- **Encrypted credentials.** Tokens are Fernet-encrypted before they touch
  SQLite and are never logged (emails and tokens are masked in every message).
- **Provider-agnostic workers.** The pool speaks plain HTTP with two request
  shapes, so it works with hosted APIs, local models or your own bridge — no
  vendor lock-in and no automation of services that forbid it.
- **Project-local task ids.** Plan ids (`t1`, `t2`, …) are unique per project;
  the `tasks` table is keyed by `(id, project_id)` so many projects coexist.
- **One engine per orchestrator.** Passing a `Settings` object to an
  `Orchestrator` rebinds the database engine to that configuration, which is
  what lets tests run dozens of isolated in-memory orchestrators.

### Testing notes

- Every test runs against mocked HTTP transports and in-memory SQLite, so no
  credentials or network access are required.
- Retry backoff collapses to milliseconds in tests via
  `KOLLEKTIV_RETRY_BASE_DELAY` / `KOLLEKTIV_RETRY_MAX_DELAY`; the same variables
  tune (or effectively disable) waiting in production.
- `tests/test_state.py` covers the markdown round trip and the storage-outage
  fallback; `tests/test_api.py` covers the routes through
  `httpx.ASGITransport` and calls the real MCP tools through the SDK.

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `kollektiv check` reports heuristic mode | `BRAIN_API_KEY` unset — the pipeline still runs, but without LLM planning/review |
| "no agent became available" | every account is busy or cooling down; add accounts or lower `ARENA_MAX_CONCURRENCY` |
| `AuthenticationError` from TeraBox | access token expired and refresh failed — check `refresh_token`, or set `TERABOX_APP_ID`/`TERABOX_APP_KEY` |
| Uploads fail with `errno -6` / `-9` | the account cannot write at that path; the pool retries the next account, check `TERABOX_REMOTE_ROOT` |
| Webhooks return `401` | `GITHUB_WEBHOOK_SECRET` does not match the GitHub hook configuration |
| Webhooks return `202` but nothing happens | the orchestrator is mid-sync (a single lock serialises sync passes); check the logs for `sync` |
| TLS verification errors behind a proxy | set `SSL_CA_BUNDLE=/path/to/ca.pem` (`HTTP_SSL_VERIFY=false` only for local debugging) |
| Files are never written to disk | workers are not tagging fenced blocks with paths — see the output contract |
| Tasks fail with "review score" | the reviewer rejected the output; `GET /projects/{id}/status` shows the feedback per task |
| `database is locked` | more than one process is writing SQLite; disable the in-process cron in all but one, or move to PostgreSQL |

---

## FAQ

**Do I need a paid LLM?** No. The brain is optional and the worker pool is
plain HTTP. The intended setup is a cheap planning model plus free/cheap worker
endpoints.

**How many agents should I configure?** Start with 3: enough to parallelise
independent tasks, few enough that your endpoints stay inside their rate limits.
`planner` will happily produce more tasks than agents — they queue in waves.

**Can it work on an existing repository?** Yes: set `GITHUB_REPO` and
`GITHUB_DEFAULT_BRANCH`, and the sync engine summarises recent commits so the
first plan starts from reality. Push changed files with
`GITHUB_PUSH_AGENT_OUTPUT=true`.

**Does it merge pull requests?** No. It reads PRs, reviews them and comments
with a summary; a human merges. That is deliberate — the orchestrator never
writes to a protected branch on its own.

**Where does the state live if TeraBox is down?** In
`WORKSPACE_DIR/<project_id>/PROJECT_STATE.md`, and it is flushed to TeraBox as
soon as the next upload succeeds.

**How do I run it completely offline?** `BRAIN_API_KEY=` empty,
`ARENA_ACCOUNTS=[]` — you get the heuristic planner and a pool with no workers
(`run` will tell you so). Tests cover exactly this mode.

---

## Roadmap

- [ ] PostgreSQL-backed multi-replica deployment guide and migrations
- [ ] Worker-side sandboxing (run collected code in a container before upload)
- [ ] Plan templates and reusable skill packs per task type
- [ ] Web UI for the state document and event stream
- [ ] Additional storage backends (S3, WebDAV) behind the pool interface
- [ ] Cost/latency accounting per provider in `/health`

---

## Legal & responsible use

Kollektiv is a coordination framework. It intentionally ships **no** code to
scrape services, bypass paywalls, defeat CAPTCHAs or evade rate limits: rate
limits are respected, and a throttled endpoint is routed around rather than
hammered. Bring your own API keys, endpoints or self-hosted models, and follow
the terms of service of every system you connect — including GitHub's API
limits and TeraBox's storage policies. You are responsible for the code your
agents produce and for reviewing it before it ships.

## License

MIT — see [LICENSE](LICENSE).
