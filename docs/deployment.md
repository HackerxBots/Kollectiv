# Deployment and the free stack

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

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
| 5 | **Cloudflare Pages** | unlimited static sites | *Workers & Pages* → *Create* → connect this repo → build output `web` | *(dashboard only — see [web/README.md](../web/README.md))* |
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
[Configuration](configuration.md#configuration).

---

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
| Dashboard | Cloudflare Pages | static `web/` (see [web/README.md](../web/README.md)); the API also serves it at `/ui`, and `/` redirects there |

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

With Cloudflare configured, **branch previews come for free**: `main` deploys to
the project's production URL and every other branch (and pull request) deploys to
`https://<branch>.<project>.pages.dev`, with the link posted as a sticky comment
on the PR. One project and one shareable link is enough — the previews are just
that link, per branch. GitHub Pages has a single site per repository, so the
fallback stays `main`-only on purpose.

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
