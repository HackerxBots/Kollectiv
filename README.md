# Kollektiv

[![CI](https://github.com/HackerxBots/Kollektiv/actions/workflows/ci.yml/badge.svg)](https://github.com/HackerxBots/Kollektiv/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/HackerxBots/Kollektiv?include_prereleases&label=release)](https://github.com/HackerxBots/Kollektiv/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Tests: 262](https://img.shields.io/badge/tests-262%20passing-brightgreen.svg)](tests/)

**A multi-agent collaborative dev team orchestrator — free to run, self-hosted, open source (MIT).**

Give Kollektiv a project brief and a few LLM endpoints. It plans the work,
splits it into subtasks, runs them in parallel across pooled agents, reviews the
output, keeps a shared state document in cloud storage, syncs everything through
GitHub, and can reach out to the services you already use (Gmail, Calendar,
Drive, Notion, Slack, your own APIs) through **connectors**.

```
brief ─► brain (DeepSeek/Groq/local) ─► planner ─► N parallel worker agents ─► collector
                                                          │                        │
                                    Cloudflare R2 (one pooled drive) ◄── sync ── GitHub
                          optional free tiers: Neon (db) · Clerk (auth) · Resend (email) · Pages (UI)
                          connectors: GitHub · Google · Notion · Webhooks · any REST API
```

> **In one line:** a self-hosted dev team that runs on free tiers, talks to the
> tools you already use, and lets you swap every component for your own.

<!-- The block above is the "peak" description: it is updated with every release
     (see the release checklist in CLAUDE.md). -->

### What it is, and what it is not

| Kollektiv **is** | Kollektiv **is not** |
| --- | --- |
| An orchestrator that plans, dispatches, collects and reviews work across many agents | An in-editor autocomplete or a single-agent CLI |
| Provider-agnostic: any OpenAI-compatible endpoint, including local models | A wrapper around one vendor's subscription |
| Self-hostable end to end, with free-tier defaults for every dependency | A hosted service you cannot audit |
| Honest about state: every subsystem reports its own health | Silent about what is degraded |

---

## Why people run it

- **Free by construction.** Every dependency is optional and every default is
  the zero-cost path: SQLite, local workspace, open API, log-only notifications.
  As keys appear, the same code upgrades in place (R2, Neon, Clerk, Resend,
  Pages) — see [Run it for free](#run-it-for-free).
- **Many accounts, one drive.** R2 buckets or TeraBox accounts are pooled into a
  single logical drive, routed by free space and health, so several free
  accounts add up to one large shared volume.
- **Many agents, one team.** Worker endpoints are pooled and scheduled in
  dependency order; a throttled or failing worker is cooled down and routed
  around instead of stalling the run.
- **Your tools, not ours.** Connectors expose Gmail, Calendar, Drive, Notion,
  GitHub, outbound webhooks and *any* JSON API as callable tools for the agents
  (and for your MCP client) — see [Connect your services](#connect-your-services).
- **Real source of truth.** GitHub holds the commits; a shared
  `PROJECT_STATE.md` holds the plan, task status and history; `/health` holds
  the truth about what is configured.

---

## Table of contents

1. [Run it for free](#run-it-for-free)
2. [Quick start](#quick-start)
3. [Connect your services](#connect-your-services) — connectors
4. [Agent runtimes](#agent-runtimes) — free coding agents & local models
5. [Configuration](#configuration)
6. [How a run works](#how-a-run-works)
7. [The shared state document](#the-shared-state-document)
8. [Interfaces](#interfaces) — HTTP API, MCP, CLI
9. [Deployment](#deployment) — Docker, bare metal, free hosting, self-hosting
10. [Operations](#operations) — health, releases, scaling, the drive pool
11. [Extending Kollektiv](#extending-kollektiv)
12. [Project layout](#project-layout)
13. [Development](#development)
14. [Performance & next iteration](#performance--next-iteration)
15. [Troubleshooting](#troubleshooting)
16. [FAQ](#faq)
17. [Roadmap](#roadmap)
18. [Privacy: no telemetry, no accounts, no data collection](#privacy-no-telemetry-no-accounts-no-data-collection)
19. [Contributing](#contributing)
20. [Legal & responsible use](#legal--responsible-use)

---

## Run it for free

Everything Kollektiv needs has a free tier, and every piece is optional: it
degrades instead of failing. `kollektiv bootstrap` prepares the install and
prints this same checklist with your current status:

```bash
pip install -e ".[dev]"
kollektiv bootstrap          # creates the schema/workspace, prints the checklist
kollektiv check              # what is configured, what is missing, and why
kollektiv serve-api          # http://localhost:8000/docs
```

| # | Tool | Free tier | Steps | `.env` keys |
| - | ---- | --------- | ----- | ----------- |
| 1 | **Cloudflare R2** | 10 GB stored, unlimited reads, **no egress fees** | R2 → *Create bucket* (`kollektiv`) → *Manage API tokens* → *Object Read & Write* | `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT` (`https://<account-id>.r2.cloudflarestorage.com`) |
| 2 | **Neon** | serverless Postgres, 0.5 GB + autosuspend | *Create project* → copy the **pooled** connection string | `DATABASE_URL` (`postgresql://…-pooler.…neon.tech/kollektiv?sslmode=require`) |
| 3 | **Clerk** | 10k monthly active users | *Create application* → copy the keys; add a webhook endpoint | `CLERK_SECRET_KEY`, `CLERK_PUBLISHABLE_KEY`, `CLERK_WEBHOOK_SECRET`, `AUTH_REQUIRED=true` |
| 4 | **Resend** | 3k emails/month | *API Keys* → create; verify a sender | `RESEND_API_KEY`, `RESEND_FROM`, `NOTIFY_EMAILS` |
| 5 | **Cloudflare Pages** | unlimited static sites | *Workers & Pages* → *Create* → connect this repo → build output `web` | *(dashboard only — see [web/README.md](web/README.md))* |
| + | **Groq / DeepSeek** | free credits / cheap tokens | create a key, keep it OpenAI-compatible | `BRAIN_API_KEY`, `BRAIN_PROVIDER`, `ARENA_ACCOUNTS` |

No account for any of them? Kollektiv still runs: the brain falls back to the
deterministic heuristic planner, storage falls back to the local workspace and
the API runs without auth. Add the keys later — nothing has to be migrated.

### One storage layer, two providers (and the 9Drive trick)

It is easy to read "R2 or 9Drive" as two storage options. It is really one
layer with two *providers*, plus a pooling pattern that applies to both:

| Piece | What it is |
| --- | --- |
| **Provider: Cloudflare R2** | a bucket you own, S3 API, 10 GB free, no egress fees |
| **Provider: TeraBox** | free consumer cloud drive; several free accounts can be pooled |
| **Pattern: pooling ("9Drive style")** | many accounts/buckets presented as **one logical drive**, routed by free space, health and cooldown |

```bash
STORAGE_BACKEND=auto   # R2 -> TeraBox -> local workspace
STORAGE_BACKEND=r2     # force R2 (single key or R2_ACCOUNTS pool)
STORAGE_BACKEND=terabox
```

So: **one** place to configure, **one** API (`upload_file`, `download_file`,
`list_all_files`, `get_total_quota`, `get_file_url`), and the pool is what makes
several free accounts add up to one large drive. Switch providers without
touching a line of agent code.

The storage layer pools accounts and presents them as **one drive**:

- every account/bucket gets a weight from its free space and error rate;
- uploads go to the healthiest account, downloads and listings are routed to
  whichever account holds (or can serve) the object;
- keys are namespaced per account (`R2_PREFIX`), so one account can host many
  projects and two accounts never collide;
- a failing account is cooled down and the pool routes around it;
- `GET /storage/status` (and the dashboard) shows per-account usage,
  health and cooldown until the next retry.

```jsonc
// R2_ACCOUNTS — several free Cloudflare accounts pooled into one drive
[
  {"name": "primary",  "bucket": "kollektiv",   "access_key_id": "…", "secret_access_key": "…",
   "endpoint": "https://<account-a>.r2.cloudflarestorage.com"},
  {"name": "overflow", "bucket": "kollektiv-2", "access_key_id": "…", "secret_access_key": "…",
   "endpoint": "https://<account-b>.r2.cloudflarestorage.com", "weight": 2}
]
```

TeraBox works the same way through `TERABOX_ACCOUNTS` (also 9Drive style: one
logical drive, many free accounts, routed by free space) — see
[Configuration](#configuration).

---

## Quick start

```bash
# Option A — install the package (the API/MCP entrypoints above need this layout)
pip install kollektiv && kollektiv bootstrap && kollektiv serve-api

# Option B — work from a clone (recommended for the dashboard and docs)
git clone https://github.com/HackerxBots/Kollektiv.git
cd Kollektiv

python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env        # fill in your keys (see Configuration)
kollektiv secret            # paste the output into SECRET_KEY
kollektiv check             # shows exactly what is missing or degraded
kollektiv serve-api         # dashboard at http://localhost:8000/ui, docs at /docs
```

Your first project:

```bash
kollektiv run "Build a URL shortener: FastAPI service, SQLite storage, CLI and pytest tests" \
    --name shortener --agents 3
kollektiv status --project-id prj_…      # the shared state document
```

The dashboard (projects, tasks, agents, storage quota, health):

```bash
open http://localhost:8000/ui        # or point Cloudflare Pages at web/
```

It is a plain static site in `web/`: `index.html` plus `assets/styles.css`,
`assets/app.js` and `assets/favicon.svg` — no build step, no framework, no CDN,
no web fonts, no analytics. The project drawer streams live state over Server-Sent
Events (`GET /projects/{id}/events/stream`), the command palette (⌘K / Ctrl-K)
drives every action, dangerous connector calls ask for confirmation, everything
has hover, focus-visible and reduced-motion states, and the API serves the same
files at `/ui` so a single-origin deployment needs no CORS at all.
`docs/ui-prompt.md` records the prompt that rebuilds it; `web/README.md` covers
the Cloudflare Pages deployment.

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

## Connect your services

Connectors are small, typed adapters that expose a service as **actions**. The
brain sees them as tools, `GET /connectors` lists them, the MCP server exposes
them to Claude/Cursor/etc., and the CLI can call them by hand:

```bash
kollektiv connectors                 # what exists, what is configured, which actions
kollektiv call github recent_commits --params '{"limit": 5}'
kollektiv call notion search --params '{"query": "roadmap"}'
kollektiv call webhook notify --params '{"text": "deploy finished"}' --confirm
```

```bash
curl -s localhost:8000/connectors | jq '.configured, .connectors[] | {name, detail}'
curl -s -X POST localhost:8000/connectors/github/call \
  -H 'content-type: application/json' \
  -d '{"action": "open_pull_requests"}'
```

| Connector | Env | Actions | Notes |
| --- | --- | --- | --- |
| **GitHub** | `GITHUB_TOKEN`, `GITHUB_REPO` | `recent_commits`, `commit_diff`, `open_pull_requests`, `pull_request_diff`, `file`, `repo_tree`, `open_issues`, `comment_on_pull_request`* | the repository the team works on |
| **Google Workspace** | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | `gmail_search`, `gmail_read`, `gmail_send`*, `calendar_events`, `calendar_create_event`*, `drive_search`, `drive_export` | one OAuth token covers Gmail + Calendar + Drive; the refreshed access token is cached in memory and in the encrypted store |
| **Notion** | `NOTION_TOKEN` | `search`, `get_page`, `query_database`, `create_page`*, `append_text`* | share each page/database with the integration |
| **Webhooks** | `EVENT_WEBHOOKS` | `notify`, `list_targets` | run summaries are broadcast after every orchestrated run; point it at Slack, Discord, n8n, Activepieces, Zapier or your own service |
| **Any REST API** | `CUSTOM_CONNECTORS` | whatever you declare | one JSON entry per service, one action per endpoint — no code |

`*` = **dangerous**: it changes something outside Kollektiv, so it requires an
explicit confirmation (`--confirm`, `"confirm": true` or `confirm=True` in MCP).

### Declaring your own connector (no code)

```jsonc
// CUSTOM_CONNECTORS in .env
[{
  "name": "slack", "category": "chat",
  "description": "Post to Slack",
  "base_url": "https://slack.com/api",
  "auth": "bearer", "token": "xoxb-…",
  "actions": [
    {"name": "post_message", "method": "POST", "path": "/chat.postMessage",
     "description": "Send a message", "dangerous": true,
     "params": {"channel": "Channel id", "text": "Message body"}},
    {"name": "channel_history", "method": "GET", "path": "/conversations.history",
     "params": {"channel": "Channel id", "limit": "Max messages"}}
  ]
}]
```

`{placeholders}` in `path` are filled from the parameters; the rest become the
query string (GET/DELETE) or the JSON body (POST/PUT/PATCH). `auth` is one of
`bearer`, `header` (`header_name`, default `Authorization`), `query`
(`query_name`, default `api_key`) or `none`.

### Calling connectors from an MCP client

Add Kollektiv to your MCP client (`claude_desktop_config.json`, Cursor, …) and
every connector action becomes a tool next to the project tools:

```json
{
  "mcpServers": {
    "kollektiv": {
      "command": "python",
      "args": ["-m", "src.api.mcp_server"],
      "env": { "KOLLEKTIV_ENV_FILE": "/etc/kollektiv.env" }
    }
  }
}
```

Tools: `list_projects`, `get_project_status`, `create_project`, `run_project`,
`replan_project`, `list_files`, `upload_file`, `get_agent_pool_status`,
`get_storage_status`, `trigger_sync`, **`list_connectors`**, **`call_connector`**,
plus OpenAI built-in web search when run with `--with-search`.

### Making connectors reliable (design for "no support tickets")

The catalog above is deliberately shaped so a wrong call fails *early, cheaply
and legibly*:

| Guarantee | How it works | What a user sees |
| --- | --- | --- |
| **Known actions only** | every connector publishes its action list; unknown names are rejected | `Connector 'webhook' has no action 'notifyy' (available: notify, list_targets)` |
| **Known parameters only** | parameters are validated against the action's declared params before any request | `webhook.notify does not accept tex; accepted parameters: text, url, event` |
| **Safe by default** | read actions never mutate; anything that sends/creates/comments is `dangerous` and needs `confirm` | the call refuses to run with an explicit message |
| **Reachability, on demand** | `kollektiv connectors --probe`, `POST /connectors/{name}/probe` run the cheapest read action and time it | `[ok] github 0.42s reachable` / `[FAIL] notion not configured (NOTION_TOKEN)` |
| **Secrets never leak** | tokens are encrypted at rest, masked in logs, `/health` and `redacted()` | `***redacted***` in every dump |
| **Failures are typed** | transport/5xx/429 retry with backoff and honour rate limits; 4xx fail fast with the service's own message | a 401 says "rejected the token", a 403 says which scope is missing |
| **One service cannot break a run** | every action is isolated; failures are logged, reported and skipped | the run finishes, `/health` shows the degradation |
| **Response caps** | bodies are truncated to a sane size before they reach an agent's context | no accidental 20 MB diff in a prompt |

The probe is the first thing to run when a connector looks broken — it separates
"the credential is wrong" from "the service is down" from "the action name is a
typo" without reading a log.

### Credentials and safety

- Connector secrets are masked in logs, in `/health` and in `redacted()`.
- Tokens can live in the encrypted token store (`service="google"`,
  `account="default"`), which takes precedence over `.env` — refreshed OAuth
  tokens are written back automatically.
- Every connector is optional and every failure is isolated: an unreachable
  service returns an error for that action, logs it, and leaves the run alone.
- Read actions are safe by default; anything that sends, creates or comments is
  flagged `dangerous` and gated behind `confirm`.

---

## Agent runtimes

**In plain terms.** Kollektiv is the *manager*: it plans, splits the work,
tracks the shared state and pushes to GitHub. A "runtime" is whoever *writes the
code* for one subtask. Any program that accepts a prompt over HTTP and returns
text can be that writer, so the list below is a menu, not a dependency — the
default is an Arena account, and everything else is optional.

| Question | Answer |
| --- | --- |
| What is a "worker"? | One credential entry in `ARENA_ACCOUNTS` pointing at an endpoint + model. The pool runs several in parallel. |
| What is a "runtime"? | The program behind that endpoint: an LLM API, a local model, or a whole CLI agent wrapped in a shim. |
| Default | **Arena accounts** (`kollektiv login` stores the session token encrypted). |
| Optional | Groq, DeepSeek, OpenRouter, Together, local Ollama/vLLM, or a CLI agent (Aider, OpenHands, …) — see the table. |

```bash
kollektiv login                            # Arena, default: paste your session token (encrypted)
kollektiv login --provider groq            # optional: a key instead of an account
kollektiv login --provider ollama          # optional: a local model, no key at all
kollektiv accounts                         # what is stored (tokens masked)
kollektiv connectors --probe               # can everything actually be reached?
```

### Arena accounts are the default

Arena accounts are what Kollektiv was built around, and the flow is deliberately
account-first: `kollektiv login` prompts for the session token from your
signed-in session, encrypts it with `SECRET_KEY` and stores it in the database
(never in `.env`, never in git). `ARENA_ACCOUNTS` then only names the account:

```jsonc
// .env — the token lives in the encrypted store, not here
ARENA_ACCOUNTS='[{"name":"default","base_url":"https://arena.ai","provider":"arena"}]'
```

Kollektiv does not scrape, automate logins, or bypass any tier: a session token
that you supply is used against the endpoint you are authorised to use, rate
limits are respected, and a throttled account is cooled down rather than
hammered. The other providers exist so the project stays usable if an account is
unavailable — not to work around anyone's terms.

### Session continuity: never lose the good part

The annoying part of agent work is that a session ends mid-run and the next one
starts from nothing. Kollektiv keeps the plan, task status, files and history in
`PROJECT_STATE.md`, and turns that into a **resume briefing**:

```bash
kollektiv resume --project-id prj_abc123    # briefing, and refreshes HANDOFF.md
curl -s "localhost:8000/projects/prj_abc123/handoff?markdown=true"
```

```
## Next actions
1. t3 — Add tests (failed — retry or replan; assigned: w3)
2. t2 — Implement storage (already in progress; assigned: w2)

## Blockers
- task t3 (Add tests) failed: timeout
```

Paste that into a fresh Arena chat (or hand it to a colleague, or an MCP client)
and the work continues instead of restarting. `HANDOFF.md` is rewritten after
every run, so it is always current; `GET /projects/{id}/handoff` returns the same
data as JSON and the MCP tool `get_handoff` exposes it to any MCP client.

### The full menu (optional)

| Runtime | Licence | Why you would plug it in |
| --- | --- | --- |
| **[OpenHands](https://github.com/All-Hands-AI/OpenHands)** | MIT | self-hostable autonomous agent, sandboxed runs, strongest headless/CI story |
| **[Aider](https://github.com/Aider-AI/aider)** | Apache-2.0 | git-native edits; excellent with cheap or local models |
| **[OpenCode](https://github.com/sst/opencode)** | MIT | provider-agnostic terminal agent, 75+ providers including local |
| **[Goose](https://github.com/block/goose)** | Apache-2.0 | MCP-heavy automation (code *and* non-code tasks) |
| **[Cline](https://github.com/cline/cline)** / **[Kilo Code](https://github.com/kilo-org)** | Apache-2.0 / MIT | autonomous edits in the editor; Kilo runs parallel agents |
| **[Qwen Code](https://github.com/QwenLM/qwen-code)** | Apache-2.0 | open fork of the Gemini CLI line, pairs with open-weight models |
| **[Codex CLI](https://github.com/openai/codex)** | Apache-2.0 | sandboxed CLI agent; local models via `--oss` |
| **Local models** (Ollama, llama.cpp, LM Studio, vLLM) | — | zero per-token cost; expose the OpenAI-compatible endpoint as a worker |

#### Wiring one in

The pool speaks OpenAI-compatible HTTP (`base_url` + `model` +
`session_token`), so hosted and local endpoints work as-is:

```jsonc
// ARENA_ACCOUNTS — one entry per worker
[
  {"name": "local-ollama", "session_token": "ollama",
   "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5-coder:32b"},
  {"name": "groq-worker-1", "session_token": "gsk_…",
   "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"}
]
```

For a CLI agent (Aider, OpenHands, Codex CLI), put it behind a tiny HTTP shim
that accepts `{"prompt": …}` and returns `{"text": …}` — a ~40-line FastAPI app —
and register that URL as a worker. Kollektiv keeps planning, dependency
ordering, collection, review, state and sync; the runtime only has to write the
code for one subtask. `examples/aider_shim.py` is a working template.

**The 3-example minimisation.** Three workers on free endpoints (Groq, a local
Ollama model, a cheap DeepSeek key) cost nothing to start and are enough to see
the whole pipeline work end to end; add accounts as you hit rate limits.

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

### Free-hosted stack (no server of your own)

The whole thing runs on free tiers with no VM:

| Piece | Where it runs | Notes |
| --- | --- | --- |
| API + scheduler | any small always-on box, a free Oracle/AWS micro instance, or `docker compose` on a laptop | it needs a long-lived process for the cron and the webhooks |
| Database | Neon (serverless Postgres) | set `DATABASE_URL` to the **pooled** string; SQLite stays the default |
| Shared drive | Cloudflare R2 (or pooled TeraBox) | `STORAGE_BACKEND=auto` picks R2 as soon as the keys are present |
| Auth | Clerk | `AUTH_REQUIRED=true` protects every route except `/health`, `/docs` and `/webhooks/*` |
| Email | Resend | run summaries/alerts; `NOTIFY_ON_FAILURE_ONLY=true` keeps the quota for failures |
| Dashboard | Cloudflare Pages | static `web/` (see [web/README.md](web/README.md)); the API also serves it at `/ui`, and `/` redirects there |

Bootstrap a fresh box end to end:

```bash
git clone https://github.com/HackerxBots/Kollectiv.git && cd Kollektiv
python -m venv .venv && .venv/bin/pip install -e ".[postgres]"
.venv/bin/kollektiv bootstrap      # prints the exact keys still missing
.venv/bin/kollektiv serve-api      # uvicorn src.api.routes:app --port 8000
```

Cloudflare Pages deployment is automatic once you set the repository variable
`CF_PAGES_PROJECT` (and the `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID`
secrets); without them the workflow publishes the same folder to GitHub Pages.

### Self-hosting checklist

Kollektiv is designed to be self-hosted; nothing phones home and no feature is
reserved for a hosted edition. A single small VM (1 vCPU / 1 GB) is enough,
because the heavy lifting happens at the LLM endpoints, not here.

| Piece | Minimum | Notes |
| --- | --- | --- |
| Python | 3.11+ | `pip install -e ".[postgres]"` adds the Neon/Postgres driver |
| Process | 1 API instance | the in-process cron needs exactly one scheduler in a multi-replica setup |
| Disk | ~200 MB + workspace | artifacts also live in the shared drive |
| Database | SQLite, or Postgres/Neon for replicas | `AUTO_INIT_DB=true` creates the schema on boot |
| TLS | required for GitHub webhooks | nginx/Caddy in front; set `CLERK_AUTHORIZED_PARTIES` to your origin |
| Backups | `data/` (SQLite + workspace), the drive, the repo | the repo is already a backup of the code |

Security defaults worth knowing:

- `AUTH_REQUIRED=false` is the default so a fresh clone works locally. Turn it
  on (`AUTH_REQUIRED=true` + Clerk keys) before exposing the API; `/health`,
  `/docs` and `/webhooks/*` stay public by design (they verify their own
  signatures).
- Connector secrets are encrypted at rest and masked in logs, `/health` and
  `redacted()`; dangerous actions need an explicit `confirm`.
- `SECRET_KEY` protects the token store — back it up with the database, or
  stored tokens become unreadable.
- Nothing about TeraBox or any other service is scraped or automated: bring
  endpoints and tokens you are authorised to use.

### Behind a proxy or a corporate CA

Set `SSL_CA_BUNDLE=/path/to/ca.pem` (or `HTTP_SSL_VERIFY=false` for a local
lab, never in production). Both the GitHub, TeraBox and brain clients honour
these settings through `src/utils/net.py`.

---

## Operations

### Releases, versioning and the README

Kollektiv ships small and often, and documents every step. **Everything is a
beta tag for now:** the interfaces still move, so every tag is `v0.3.0-beta.1`,
`v0.3.0-beta.2`, … and the release notes open with a beta warning. The release
itself is published as a *normal* GitHub release (not a pre-release) so it shows
up in the sidebar's "Latest" widget instead of hiding — the tag is the honest
signal, and the README is updated in the same PR as the change.

- **Versioning** — `0.x` while the API/config still evolves. Inside a minor
  line: `-beta.N` increments per batch of changes, `MAJOR`/`MINOR` bumps when
  behaviour changes, `PATCH` (`v0.3.1-beta.1`) for fixes only.
- **Changelog first.** `CHANGELOG.md` is updated in the same PR; the release
  workflow refuses to publish a tag whose version has no changelog section.
- **Beta tags, findable releases:** the workflow keeps `vX.Y.Z-beta.N` tagging
  and a beta banner in the notes, attaches the sdist + wheel, and publishes the
  release so it is listed (pre-releases are skipped by "Latest").
- **The README is part of the release.** The peak block (release name, test
  count) and the tool/table sections change with it; the checklist in
  `CLAUDE.md` keeps that honest.

Cutting a release:

```bash
# 1. bump version in pyproject.toml and src/__init__.py
# 2. move the CHANGELOG "Unreleased" entries under the new version
# 3. update the README peak block
git tag v0.3.0-beta.1 && git push origin v0.3.0-beta.1   # workflow publishes the release
```

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
  copy in the shared drive (R2/TeraBox) is authoritative, this is the cache.
- Delete a project's artifacts with `GET /projects/{id}/files` plus the pool's
  `delete_file`, or remove the remote folder `/Kollektiv/<project_id>`.

### Scaling

| Symptom | Knob |
| --- | --- |
| Tasks queue behind each other | add `ARENA_ACCOUNTS`, or raise `ARENA_MAX_CONCURRENCY` |
| Storage fills up | add `R2_ACCOUNTS` buckets or `TERABOX_ACCOUNTS` (the pool routes by free space) |
| Brain is slow/expensive | keep the cheap model for planning, set a larger `BRAIN_MODEL` only for reviews |
| Sync takes long | raise `CRON_INTERVAL_MINUTES` |

SQLite handles a single API process comfortably. For multiple replicas, point
`DATABASE_URL` at Neon (the `[postgres]` extra installs the driver) and disable
the in-process scheduler so the cron runs in exactly one place.

---

## Extending Kollektiv

**A new worker shape.** Subclass `ArenaClient` and override `_build_request()`
(or point `base_url` at an OpenAI-compatible gateway) — the pool needs nothing
else. Anything that accepts a prompt and returns text can be a worker.

**A new brain provider.** Any OpenAI-compatible endpoint works by setting
`BRAIN_BASE_URL`/`BRAIN_MODEL`. For a different protocol, implement `complete()`
on a subclass of `OrchestratorBrain` and pass it to `Orchestrator(brain=…)`.

**A new storage backend.** `R2Storage`/`TeraBoxPoolManager` expose
`upload_file/download_file/list_all_files/get_total_quota` (plus
`get_file_url`/`get_status`); implement the same methods — WebDAV, a NAS, B2 —
and pass it as `pool=` or add a branch to `build_storage()`.

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
src/storage/pool_manager.py    multi-account routing by free space (9Drive style)
src/storage/r2_client.py       Cloudflare R2 (S3) client on the sigv4 signer
src/storage/r2_pool.py         R2 buckets/accounts pooled into one drive
src/storage/factory.py         build_storage(): auto picks R2 → TeraBox → local
src/utils/sigv4.py             dependency-free AWS SigV4 signing + presigning
src/utils/resend_client.py     Resend email notifications (summaries, alerts)
src/api/auth.py                Clerk JWT verification, auth middleware, Svix
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
src/connectors/base.py         connector + action registry (one tool catalogue)
src/connectors/github.py       repository actions (commits, PRs, issues, files)
src/connectors/google_workspace.py  Gmail + Calendar + Drive over one OAuth token
src/connectors/notion.py       Notion pages and databases
src/connectors/webhook.py      outbound events (Slack/Discord/n8n/Zapier/webhooks)
src/connectors/rest.py         declarative REST connectors (CUSTOM_CONNECTORS)
src/api/cli.py                 the `kollektiv` command line interface
examples/aider_shim.py         wrap any CLI coding agent as a worker endpoint
web/index.html                 dashboard markup — four views, hash routes
web/assets/styles.css          design tokens, components, hover/focus states
web/assets/app.js              API client, renderers, command palette, SSE consumer
web/assets/favicon.svg         logo (inline SVG, no icon font)
docs/ui-prompt.md              prompt that (re)builds the dashboard
SECURITY.md                    private vulnerability reporting + threat model
CONTRIBUTING.md                gates, non-negotiables, connector + release recipes
CODE_OF_CONDUCT.md             Contributor Covenant 2.1
.github/ISSUE_TEMPLATE/         bug, feature and question forms
.github/workflows/codeql.yml   CodeQL scanning (PRs + weekly)
tests/                         262 hermetic tests (no network, no credentials)
```

---

## Development

```bash
pip install -e ".[dev]"

pytest -q                 # 262 tests, ~10 s, fully mocked
pytest tests/test_api.py -q
ruff check .              # lint (clean)
mypy src config examples  # types (clean)
pytest --cov=src          # optional coverage (pip install pytest-cov)
```

`tests/test_r2.py` pins the SigV4 vectors and, when `botocore` is installed
(it is part of the `dev` extra), cross-checks the hand-rolled signer against
`botocore.auth.S3SigV4Auth` — so the free-tier storage client provably matches
the reference implementation.

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
- **Free tier first, paid never required.** Cloudflare R2, Neon, Clerk, Resend
  and Pages are all optional: `STORAGE_BACKEND=auto`, SQLite, open routes and
  log-only notifications are the defaults, so a fresh clone runs end to end
  with zero accounts and upgrades in place when keys appear.
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

## Performance & next iteration

The orchestrator is deliberately simple: one process, one database, one drive.
That is plenty for tens of projects, and the bottlenecks are known — this is the
plan for the next iterations (each item is sized so it can ship on its own).

**Now (v0.3.0-beta.x)**

| Item | Change | Why |
| --- | --- | --- |
| Dashboard connectors panel | list connectors, run safe actions from the UI | the UI can now exercise the whole tool catalogue |
| Connector registry | one catalogue for brain, API, MCP and CLI | adding a service is a JSON entry, not a code path |
| `bind_engine()` | one place that binds the database | CLI, orchestrator and API agree on `DATABASE_URL` |
| Event broadcast | run summaries pushed to `EVENT_WEBHOOKS` | pipe results anywhere without polling |

**Next (performance and scale)**

1. **Alembic migrations** for the Postgres/Neon path (today the schema is
   created idempotently at boot).
2. **Broadcast state updates.** The dashboard already consumes
   `GET /projects/{id}/events/stream`, but each connected client polls the
   database; a per-project broadcast channel (Postgres `LISTEN/NOTIFY`, or
   Redis when available) removes that last hop.
3. **Composite indexes + partial indexes** on `tasks(project_id, status)` and
   `events(project_id, created_at)` for large event streams.
4. **Streamed agent output.** Long worker responses are buffered whole; reading
   the OpenAI-compatible stream would cut time-to-first-artifact and memory.
5. **Concurrency budget per provider.** Today `ARENA_MAX_CONCURRENCY` is per
   account; a shared token-bucket per provider avoids 429 storms with many
   accounts on one endpoint.
6. **Content-addressed artifacts.** Hash files before upload so repeated runs
   skip identical uploads (a big win against 10 GB free tiers).
7. **Speculative planning.** Warm the planner for the next wave while the
   current one runs, so the brain is never the critical path.
8. **Cached repo tree.** The GitHub tree is fetched per wave; cache it with an
   ETag for the duration of a run.
9. **Local embedding index** over `PROJECT_STATE.md` and the repo, so context
   injection stops sending the whole document with every prompt.
10. **Optional local brain.** Ship an Ollama profile so a fully offline run is
    one command (`docker compose --profile local-llm up`).
11. **Worker sandboxing** (also on the roadmap): run collected code in a
    container before it is committed — the one gap that keeps Kollektiv from
    being a fully autonomous pipeline.
12. **Metering per provider** in `/health`: calls, tokens, latency and cost, so
    a free-tier budget can be seen at a glance.

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

**Where does the state live if the shared drive is down?** In
`WORKSPACE_DIR/<project_id>/PROJECT_STATE.md`, and it is flushed to R2/TeraBox
as soon as the next upload succeeds. With no storage configured at all the
workspace is the only copy — `/health` says so.

**Is this self-hostable?** Yes — it is the primary deployment: `pip install`
(plus `docker compose up --build`), one small VM, SQLite or Neon, and an
optional free dashboard on Pages. Nothing calls home, no feature is gated and
every hosted free tier in the docs can be replaced by something you run
yourself (see [Self-hosting checklist](#self-hosting-checklist)).

**Can I use something other than Arena for the workers?** Yes, and most people
do: any OpenAI-compatible endpoint works as a worker, so Groq, Together,
OpenRouter, DeepSeek, a local Ollama/vLLM server, or a shim around a CLI agent
(OpenHands, Aider, OpenCode, Goose, Cline/Kilo, Qwen Code, Codex CLI) all plug
in through `ARENA_ACCOUNTS` — see [Agent runtimes](#agent-runtimes). The same is
true for connectors: the four built-ins and `CUSTOM_CONNECTORS` cover most of
what "linking services" means, and Activepieces/n8n/Zapier can be reached
through `EVENT_WEBHOOKS` or their own REST APIs.

**Do I need any keys to start?** No. With an empty `.env` you get the heuristic
planner, a pool with zero workers, local-workspace storage, an open API and no
emails. Everything reports itself in `/health`, `kollektiv check` and
`kollektiv connectors`, so you can add one credential at a time.

**Is every piece really free?** Yes, and there is no paid component on the
critical path: Cloudflare R2 (10 GB, no egress), Neon, Clerk, Resend and Pages
all have usable free tiers, and Kollektiv runs with none of them.

**How do I run it completely offline?** `BRAIN_API_KEY=` empty,
`ARENA_ACCOUNTS=[]` — you get the heuristic planner and a pool with no workers
(`run` will tell you so). Tests cover exactly this mode.

---

## Roadmap

Shipped in v0.2.0 ("free stack"): Cloudflare R2 storage pool, Neon-ready
Postgres, Clerk auth, Resend notifications, the static Pages dashboard, the
hand-rolled SigV4 signer and `kollektiv bootstrap`.

- [ ] Alembic migrations for the Postgres/Neon path
- [ ] Worker-side sandboxing (run collected code in a container before upload)
- [ ] Plan templates and reusable skill packs per task type
- [ ] Dashboard actions for retrying a single task and viewing diffs
- [ ] Additional storage backends (WebDAV, Backblaze B2) behind the pool
- [ ] Cost/latency accounting per provider in `/health`
- [ ] Signed Python wheels + SBOM attached to each release

---

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
- **Self-hosted by default** (see [Self-hosting checklist](#self-hosting-checklist)):
  the free hosted tiers in this README (Cloudflare, Neon, Clerk, Resend) are
  conveniences you can replace with something you run, one at a time.
- **Auditable in one command.** `rg "httpx|requests" src/` lists every place a
  request can be made; the connector catalogue (`GET /connectors`) shows every
  service currently configured.

There is no paid tier, no data resale and no business model that needs your
data. That is a promise the licence (MIT) lets anyone verify and fork.

---

## Contributing

Issues and pull requests are welcome. The short version:

1. `pytest -q`, `ruff check .`, `mypy src config` must all be green.
2. New behaviour comes with tests; they must stay hermetic (no network, no real
   credentials) — see `tests/conftest.py` for the fakes.
3. Update `README.md`, `CHANGELOG.md` and (if config changed) `.env.example` in
   the same PR. The release workflow only publishes versions the changelog
   documents.
4. Commit prefixes: `feat:`, `fix:`, `test:`, `docs:`, `chore:`.

Good first issues are tagged in the tracker; the [roadmap](#roadmap) and
[Performance & next iteration](#performance--next-iteration) sections are
accurate lists of what is next.

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
