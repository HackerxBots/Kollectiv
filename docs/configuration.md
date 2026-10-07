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
