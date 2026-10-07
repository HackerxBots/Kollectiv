# Configuration

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

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

### 5. Sponsor line (`SPONSOR_*`, optional)

The only advertising surface Kollektiv has, off unless you turn it on; the full
design and the reasoning are in [Monetization](monetization.md).

| Key | Default | Purpose |
| --- | --- | --- |
| `SPONSORS_ENABLED` | `false` | Master switch. `kollektiv sponsors enable` writes it for you. |
| `SPONSOR_CATALOG_PATH` | — | JSON file with the sponsor entries (`docs/sponsors.example.json`) |
| `SPONSOR_CATALOG_URL` | — | Or an HTTPS endpoint returning the same JSON (fetched lazily) |
| `SPONSOR_CATALOG_PUBLIC_KEY` | — | Ed25519 public key (base64); set it to refuse unsigned catalogues |
| `SPONSOR_SHARE_BP` | `7500` | Your share of the gross in basis points (75%) |
| `SPONSOR_CPM_CENTS` | `100` | Fallback rate per 1000 lines when an entry omits one |
| `SPONSOR_MIN_PAYOUT_CENTS` | `1000` | A claim is only offered above this |
| `SPONSOR_CATEGORIES` | — | Self-declared interests — the only targeting that exists |
| `SPONSOR_MIN_INTERVAL_SECONDS` | `90` | Attention budget: at most one line per interval |
| `SPONSOR_REQUEST_TIMEOUT` | `15.0` | Catalogue HTTP timeout |

### 6. Connectors (`TELEGRAM_*`, `DISCORD_*`, `SLACK_*`, `LINEAR_*`, `WA_*`)

All optional, all inert until filled in — see [Connectors](connectors.md) for
what each one can do and which actions are dangerous.

| Key | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | — | Default destination chat id (per-call `chat_id` still wins) |
| `TELEGRAM_BASE_URL` | `https://api.telegram.org` | Self-hosted Bot API server |
| `DISCORD_BOT_TOKEN` | — | Bot token (Developer Portal → Bot) |
| `DISCORD_DEFAULT_CHANNEL` | — | Default channel id for `send_message` |
| `DISCORD_WEBHOOK_URL` | — | Incoming webhook; works without a bot token |
| `DISCORD_API_VERSION` | `v10` | Pinned Discord API version |
| `SLACK_BOT_TOKEN` | — | `xoxb-…` with `chat:write` |
| `SLACK_DEFAULT_CHANNEL` | — | Default channel (`#general` or an id) |
| `SLACK_WEBHOOK_URL` | — | Incoming webhook; works without a bot token |
| `LINEAR_API_KEY` | — | Personal API key (`lin_api_…`) |
| `LINEAR_TEAM_ID` | — | Default team for `create_issue` |
| `WA_BACKEND` | inferred | `cloud` (official) or `bridge` (unofficial) |
| `WA_DEFAULT_TO` | — | Default recipient number (digits, country code first) |
| `WA_CLOUD_TOKEN`, `WA_PHONE_NUMBER_ID` | — | Cloud API credentials |
| `WA_GRAPH_VERSION` | `v21.0` | Pinned Graph API version |
| `WA_BRIDGE_URL`, `WA_BRIDGE_TOKEN` | — | The local linked-device bridge |
| `WA_ALLOW_UNOFFICIAL` | `false` | **Required** for the bridge route: automating a personal number breaks Meta's terms and can get it banned |
| `<SERVICE>_REQUEST_TIMEOUT` | `30.0` | Per-service HTTP timeout |

### 7. MCP gateway (`GATEWAY_*`, optional)

One MCP URL, per-client tokens, per-client policy and a local audit log; the
full walkthrough is in [MCP gateway](gateway.md). Off unless you turn it on —
`kollektiv-mcp` and the HTTP API stay first-class either way.

| Key | Default | Purpose |
| --- | --- | --- |
| `GATEWAY_ENABLED` | `false` | Master switch (or just run `kollektiv gateway serve`) |
| `GATEWAY_HOST` / `GATEWAY_PORT` | `0.0.0.0` / `8010` | Bind address |
| `GATEWAY_MCP_PATH` | `/mcp` | Where the MCP endpoint is mounted |
| `GATEWAY_REQUIRE_TOKENS` | `true` | Refuse anonymous calls; `false` is development only |
| `GATEWAY_POLICY_PATH` | — | JSON file overriding per-client policies (keep it in git) |
| `GATEWAY_DEFAULT_CLIENT` | `dashboard` | The name `kollektiv gateway init` uses |
| `GATEWAY_AUDIT_LIMIT` | `200` | Default rows for `GET /audit` |
| `GATEWAY_ALLOWED_HOSTS` | — | Host-header allowlist; empty disables the MCP SDK's DNS-rebinding check (right for a bearer-token endpoint) |
| `GATEWAY_ALLOWED_ORIGINS` | — | Origin allowlist; defaults to the hosts above |
| `GATEWAY_MAX_BODY_BYTES` | `4194304` | Largest MCP request body |

### 8. Budget (`BUDGET_*`, `PROJECT_CONFIG_PATH`, optional)

Estimate a run before it happens, refuse to exceed a cap, and keep a local tally
of tokens and dollars. The full page is [Budgets and `.kollektiv.yml`](budget.md);
a project can also carry its own `budget.max_usd` in `.kollektiv.yml`.

| Key | Default | Purpose |
| --- | --- | --- |
| `BUDGET_ENABLED` | `true` | Estimation and the ledger; set `false` to ignore caps entirely |
| `BUDGET_MAX_USD` | `0` | Refuse a project whose estimate is above this (0 = no cap) |
| `BUDGET_DAILY_MAX_USD` | `0` | Refuse any run once today's recorded spend reaches this |
| `BUDGET_WARN_AT` | `0.8` | Warn from this fraction of a cap |
| `BUDGET_PRICE_IN_PER_MTOK` | `0.30` | Brain input price used by the estimate |
| `BUDGET_PRICE_OUT_PER_MTOK` | `1.20` | Brain output price |
| `BUDGET_WORKER_PRICE_IN_PER_MTOK` | `0` | Worker input price (free by default) |
| `BUDGET_WORKER_PRICE_OUT_PER_MTOK` | `0` | Worker output price |
| `BUDGET_PROMPT_OVERHEAD_TOKENS` | `900` | Per-task prompt overhead in the estimate |
| `BUDGET_OUTPUT_TOKENS_PER_TASK` | `700` | Expected answer size per task |
| `PROJECT_CONFIG_PATH` | — | Explicit `.kollektiv.yml` path (otherwise auto-found) |

**Prices are yours, not ours.** The defaults are one cheap model's published
rates; the estimate is only as honest as the numbers you put in, so check your
provider's current pricing page and override them.

### 9. Runtime

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
