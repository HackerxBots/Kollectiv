# Kollektiv

**A multi-agent collaborative dev team orchestrator.**

Kollektiv turns a project brief into a working repository by coordinating a
team of AI worker agents. A cheap LLM plans and reviews the work, the workers
write the code in parallel, every artifact is archived to shared cloud storage,
and GitHub is the real-time source of truth for what has actually landed.

```
                    ┌──────────────────────────┐
   brief ──────────►│  Brain (DeepSeek / Groq) │  plans, reviews, summarises
                    └────────────┬─────────────┘
                                 │ subtasks + context
                    ┌────────────▼─────────────┐
                    │  Dispatcher  (waves)     │  dependency-aware scheduling
                    └───────┬──────────┬───────┘
                            │          │
             ┌──────────────▼──┐   ┌───▼──────────────┐
             │ Worker agents   │   │ Worker agents    │   as many accounts as
             │ (account pool)  │   │ (account pool)   │   you have configured
             └───────┬─────────┘   └───────┬──────────┘
                     │  markdown w/ file paths
             ┌───────▼──────────────────────────────┐
             │  Collector  (parse, merge, conflicts)│
             └───────┬──────────────────────┬───────┘
                     │                      │
          ┌──────────▼─────────┐   ┌────────▼────────────┐
          │ TeraBox (pooled)   │   │ GitHub (commits,    │
          │ PROJECT_STATE.md   │   │ PRs, webhooks)      │
          └────────────────────┘   └─────────────────────┘
```

- **Brain** – any OpenAI-compatible endpoint (`deepseek-chat` by default,
  Groq as automatic fallback). Splits briefs into subtasks, reviews worker
  output and compresses shared state into injectable context.
- **Worker agents** – a pool of accounts/prompts running in parallel. Point each
  one at an HTTP endpoint that accepts a prompt and returns text (a hosted chat
  endpoint, a self-hosted vLLM/Ollama, or any OpenAI-compatible API key).
  Each account is one worker; more accounts means more parallelism.
- **TeraBox** – pooled object storage. Several free accounts act as one drive,
  routed by free space, and hold `PROJECT_STATE.md`, the shared memory every
  agent reads before it starts and writes to when it finishes.
- **GitHub** – the real-time sync layer. Push and pull-request webhooks,
  commit diffs, PR reviews and file archiving all flow through here.

Everything is async, every external call is retried with exponential backoff,
and every credential is stored encrypted (Fernet) in SQLite.

---

## Quick start

```bash
git clone https://github.com/HackerxBots/Kollektiv.git
cd Kollektiv

python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env        # then fill in your keys (see Configuration)
kollektiv secret            # paste the output into SECRET_KEY
kollektiv check             # shows exactly what is still missing
kollektiv serve-api         # http://localhost:8000/docs
```

Run your first project from the CLI:

```bash
kollektiv run "Build a URL shortener: FastAPI service, SQLite storage, CLI and pytest tests" \
    --name shortener --agents 3
```

…or over HTTP:

```bash
curl -s localhost:8000/projects -H 'content-type: application/json' -d '{
  "name": "shortener",
  "description": "Build a URL shortener with FastAPI, SQLite and tests",
  "n_agents": 3
}'

curl -s -X POST localhost:8000/projects/<project_id>/run
curl -s localhost:8000/projects/<project_id>/status | jq
```

With Docker:

```bash
docker compose up --build      # API on :8000, MCP server on :8001
```

---

## Configuration

All settings live in `.env` (see `.env.example`). The four that matter:

### 1. Worker agents (`ARENA_ACCOUNTS`)

A JSON list; each entry becomes a worker. Two supported shapes:

```jsonc
// (a) OpenAI-compatible endpoint with its own key
[{"name": "w1", "session_token": "sk-...", "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"}]

// (b) bespoke chat endpoint (e.g. a bridge you run yourself)
[{"email": "w1@example.com", "session_token": "...", "base_url": "https://my-bridge.internal", "api_style": "custom"}]
```

`ARENA_CHAT_PATH` / `ARENA_LOGIN_PATH` exist for endpoints with custom routes;
the client auto-detects the OpenAI shape when `base_url` ends in `/v1`, and
otherwise uses the custom envelope (`{"prompt", "agent_mode", "stream"}`).

> **Use endpoints you are allowed to use.** Kollektiv does not scrape services,
> bypass paywalls or evade rate limits. If a provider's terms forbid automated
> use, point the pool at an API key, a self-hosted model or your own endpoint
> instead. Rate limits are respected: a 429 puts that worker in cooldown and the
> task is routed to another account.

### 2. Shared storage (`TERABOX_ACCOUNTS`)

```json
[{"email": "box1@example.com", "password": "", "access_token": "…", "refresh_token": "…"}]
```

Tokens come from the TeraBox Open Platform OAuth flow (access tokens last ~2
days; the client refreshes them proactively). Without user tokens, set
`TERABOX_APP_ID`/`TERABOX_APP_KEY` for the client-credentials grant. Uploads
are sharded (4 MiB blocks, `precreate` → `shard` → `merge`) and downloads are
streamed. If TeraBox is unreachable, the state document falls back to a local
cache so the run continues in degraded mode.

### 3. GitHub (`GITHUB_TOKEN`, `GITHUB_REPO`, `GITHUB_WEBHOOK_SECRET`)

A fine-grained PAT with `contents`, `pull_requests` and `issues` scope.
Webhooks are verified with HMAC-SHA256 (`X-Hub-Signature-256`).

```bash
# point a webhook at:  https://your-host/webhooks/github
# events: push, pull_request, issues   content type: application/json
```

### 4. Brain (`BRAIN_*`)

```bash
BRAIN_PROVIDER=deepseek          # deepseek | groq | openai | openrouter | together | ollama
BRAIN_API_KEY=sk-...
BRAIN_MODEL=deepseek-chat
BRAIN_FALLBACK_PROVIDER=groq     # used when the primary provider errors
BRAIN_FALLBACK_API_KEY=...
```

Without a brain key the orchestrator still works: it falls back to a
deterministic planner, reviewer and summariser, so the whole pipeline stays
testable and usable offline.

---

## How a run works

1. **Plan** – the brain splits the brief into N subtasks, each with an id,
   priority, deliverable and dependencies. The planner turns that into
   execution *waves* (independent tasks run together) and validates the graph,
   dropping dangling ids and breaking cycles.
2. **Dispatch** – for each wave the dispatcher builds per-agent context (mission,
   current state, the task, existing files, constraints, tactical advice),
   sends it to the pool and collects the raw markdown answer.
3. **Collect** – the collector extracts fenced code blocks tagged with file
   paths, writes them into the workspace (traversal-safe), detects placeholders
   and errors, and merges results — reporting file conflicts when two agents
   produced different content for the same path.
4. **Review** – the brain scores each output; below 0.6 the task is retried once
   with the reviewer's feedback appended. Dependants of a failed task are marked
   `blocked` instead of being dispatched with missing foundations.
5. **Record** – every outcome updates SQLite, `PROJECT_STATE.md` on TeraBox and
   the event history.
6. **Sync** – every `CRON_INTERVAL_MINUTES` (and on every webhook) the sync
   engine pulls commits, summarises diffs and PRs with the brain, archives
   artifacts to TeraBox and pushes the refreshed context to the workers.

### Output contract

Workers are asked to answer with fenced blocks whose info string is the file
path:

````markdown
```python path=src/app.py
print("complete file contents")
```
````

The collector also accepts ```` ```file=src/app.py ````, ```` ```src/app.py ````,
and ```` ```yaml path=config/settings.yaml ````. Blocks tagged with only a
language are treated as snippets and never written to disk.

---

## Interfaces

### HTTP API (`kollektiv serve-api`, default port 8000)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | subsystem health + configuration warnings |
| `POST` | `/projects` | create a project and plan it (`{name, description, n_agents}`) |
| `GET` | `/projects` | list projects |
| `POST` | `/projects/{id}/run` | dispatch the plan (`?background=true` to stream on) |
| `GET` | `/projects/{id}/status` | `PROJECT_STATE.md` as JSON |
| `GET` | `/projects/{id}/files` | files stored for the project |
| `POST` | `/projects/{id}/upload` | archive a local file to TeraBox |
| `GET` | `/agents/status` | worker pool status (`?probe=true` for liveness checks) |
| `GET` | `/storage/status` | pooled TeraBox quota |
| `POST` | `/sync` | run the sync pass now |
| `POST` | `/webhooks/github` | GitHub webhook receiver |

Interactive docs: `/docs`.

### MCP server (`python -m src.api.mcp_server`, default port 8001)

Exposes Kollektiv to any MCP-capable model:

`list_projects`, `get_project_status`, `create_project`, `run_project`,
`list_files`, `upload_file`, `get_agent_pool_status`, `get_storage_status`,
`trigger_sync`.

```bash
python -m src.api.mcp_server --transport stdio            # for a local MCP client
python -m src.api.mcp_server --transport sse --port 8001  # for remote clients
```

Works with both `mcp` SDK generations (v1 `FastMCP` and v2 `MCPServer`).

### CLI (`kollektiv …`)

```
check       validate configuration and readiness (--live to call the APIs)
init-db     create the SQLite schema
plan        plan a project without running it
run         plan and execute a project
status      print a project's shared state
projects    list projects
sync        run the sync pass once
secret      print a fresh SECRET_KEY
serve-api   run the FastAPI app
serve-mcp   run the MCP server
```

---

## Project layout

```
config/settings.py            pydantic-settings configuration + account parsing
src/utils/                    logging, typed errors, retries, Fernet crypto,
                              encrypted token store, HTTP/TLS helpers
src/db/models.py              SQLModel tables (projects, tasks, files, events…)
src/storage/terabox_client.py OAuth, sharded uploads, streaming downloads
src/storage/pool_manager.py   multi-account routing by free space
src/storage/state_manager.py  PROJECT_STATE.md read/write/parse + agent context
src/agents/arena_client.py    one worker account (OpenAI + custom shapes)
src/agents/agent_pool.py      scheduling, failover, statistics
src/agents/session_manager.py periodic session maintenance (APScheduler)
src/github/github_client.py   commits, diffs, trees, PRs, comments
src/github/webhook_handler.py verified GitHub webhook endpoints
src/orchestrator/brain.py     planning, review, summarisation (LLM + fallbacks)
src/orchestrator/planner.py   dependency graph, waves, replanning
src/orchestrator/dispatcher.pywave execution, retries, blocked-task handling
src/orchestrator/collector.py  parse agent markdown, merge, conflict detection
src/orchestrator/sync_engine.pyGitHub ⇄ TeraBox ⇄ agents synchronisation
src/orchestrator/app.py       the Orchestrator that wires everything together
src/api/routes.py             FastAPI application
src/api/mcp_server.py         MCP tool server
src/api/cli.py                command line interface
tests/                        109 hermetic tests (no network, no credentials)
```

---

## Development

```bash
pip install -e ".[dev]"

pytest -q                 # 109 tests, ~4s, fully mocked
ruff check .              # lint
mypy src config           # types
```

Testing notes:

- Every test runs against an in-memory SQLite database and mocked HTTP
  transports, so no credentials or network access are required.
- Retry backoff collapses to milliseconds in tests via
  `KOLLEKTIV_RETRY_BASE_DELAY` / `KOLLEKTIV_RETRY_MAX_DELAY`; set the same
  variables in production to tune (or disable) waiting globally.

### Design decisions

- **Typed errors, two retry classes.** Transport failures and 5xx responses are
  retried with exponential backoff; 4xx/API-level errors fail fast, and rate
  limits never make a worker sleep — the pool simply routes to another account.
- **Graceful degradation everywhere.** Missing brain key → heuristics. Missing
  TeraBox → local cache. Missing GitHub token → sync reports it and continues.
  `GET /health` and `kollektiv check` always tell you what is degraded.
- **Encrypted credentials.** Tokens are Fernet-encrypted before they touch
  SQLite and are never written to logs (emails and secrets are masked).
- **Provider-agnostic workers.** The pool speaks plain HTTP with two request
  shapes, so it works with hosted APIs, local models or your own bridge — no
  vendor lock-in and no automation of services that forbid it.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `kollektiv check` says the brain is in heuristic mode | set `BRAIN_API_KEY` (DeepSeek/Groq/…) |
| All tasks fail with "no agent became available" | every account is rate limited; add accounts or lower `ARENA_MAX_CONCURRENCY` |
| Uploads fail with `errno -6` | TeraBox OAuth tokens expired; refresh them or set `TERABOX_APP_ID`/`TERABOX_APP_KEY` |
| Webhooks return 401 | `GITHUB_WEBHOOK_SECRET` does not match the GitHub hook configuration |
| TLS verification errors behind a proxy | set `SSL_CA_BUNDLE=/path/to/ca.pem` (or `HTTP_SSL_VERIFY=false` only for local debugging) |
| Workers ignore the file-path contract | tighten `WORKER_SYSTEM_PROMPT` in `src/agents/agent_pool.py` and re-run; the collector only writes path-tagged blocks |

## Legal & responsible use

Kollektiv is a coordination framework: it does not include, and will not
accept, code that scrapes services, bypasses paywalls or evades rate limits.
Bring your own API keys, endpoints or self-hosted models, and follow the terms
of every service you connect. Respect GitHub's API limits and TeraBox's storage
policies.

## License

MIT — see [LICENSE](LICENSE).
