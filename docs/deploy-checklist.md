# Deploy checklist

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

Everything here is *the order you do it in*, with the exact command or click. It
assumes you have read [Deployment](deployment.md) (the free stack in detail) and
[Configuration](configuration.md) (every setting).

Three honest premises, because they shape the whole list:

1. **Kollektiv is an always-on process.** It holds the background sync schedule
   and receives GitHub webhooks. A host that sleeps after 15 minutes will miss
   both (see [Which host](#2-which-host-honestly)).
2. **Nothing here needs a credit card except the host you choose.** The code
   path is free end to end; free *compute* in 2026 is the scarce part.
3. **A deployment is "done" when `scripts/smoke.py` passes**, not when the
   container starts. That is step 6 and it is not optional.

---

## 0. What the code already gives you

| | |
| --- | --- |
| Artifacts | `pip install kollektiv` (wheel with the dashboard inside) or `docker compose up --build` |
| Health | `GET /health` (always 200, reports degradation), `kollektiv check --json` |
| Smoke test | `python scripts/smoke.py --base-url …` — 10 checks, exit 0/1/2 |
| Migrations | none: the schema is created idempotently at boot (`kollektiv init-db`), so a fresh database just works. Alembic is tracked as a roadmap item, and is only needed once you have data you cannot re-create |
| Rollback | re-deploy the previous image/tag; the SQLite volume and R2 bucket are untouched by a rollback (no migration to undo) |

## 1. Accounts and keys to gather

Create only what you want; every row is optional and each one unlocks exactly one
capability — the app upgrades in place when it appears. The **CLI** column is the
answer to "can I do this from a terminal instead of a dashboard": nine of twelve
have one, and the three that do not are single `curl`s.

| # | Capability | Free tier | CLI | Command / where |
| --- | --- | --- | --- | --- |
| 1 | **Arena worker accounts** | free accounts | *none* (Arena has no public CLI) | `kollektiv login` stores the session token encrypted; repeat per account |
| 2 | **Brain** (planner/reviewer) | DeepSeek pay-as-you-go ≈ cents; Groq free tier | *no official CLI* | one key in `BRAIN_API_KEY`; any OpenAI-compatible endpoint via `BRAIN_BASE_URL` |
| 3 | **Shared storage** | Cloudflare R2: 10 GB, no egress | ✅ `wrangler` | `npx wrangler r2 bucket create kollektiv` → `wrangler r2 bucket …` for keys; or paste `R2_*` from the dashboard |
| 3b | _alternative_ | TeraBox free space | *none* | `kollektiv login --provider terabox`-style token flow (`TERABOX_ACCOUNTS`) |
| 4 | **Database (server deploys)** | Neon: branches + free compute | ✅ `neon` | `npm i -g neon@latest && neon auth && neon link && neon env pull` → copy `DATABASE_URL` (SQLite is the default and needs nothing) |
| 5 | **Auth (public deploys)** | Clerk free tier | ✅ `clerk` | `npm i -g clerk && clerk auth login`; `clerk config` reads/pushes instance settings; `clerk doctor` verifies |
| 6 | **Email (optional)** | Resend free tier (3 000/mo) | ✅ `resend-cli` | `npm i -g resend-cli && resend login && resend doctor`; then `RESEND_API_KEY` + `NOTIFY_EMAILS` |
| 7 | **Dashboard hosting** | Cloudflare Pages / GitHub Pages | ✅ `wrangler pages deploy` | `npx wrangler pages deploy web --project-name kollektiv`, or let `.github/workflows/pages.yml` do it. Three values switch the automation on: variable **`CF_PAGES_PROJECT`**, secrets **`CLOUDFLARE_API_TOKEN`** + **`CLOUDFLARE_ACCOUNT_ID`** (repo → Settings → Secrets and variables → Actions). Then `main` publishes the production URL and **every other branch publishes its own preview**, with the link commented on the pull request |
| 8 | **A public URL for webhooks** (no server) | Cloudflare Tunnel | ✅ `cloudflared` | `cloudflared tunnel --url http://localhost:8000` → use the printed `https://…trycloudflare.com` as the GitHub webhook URL |
| 9 | **Repo automation** | GitHub itself | ✅ `gh` | `gh auth login`, `gh run watch`, `gh release view` |
| 10 | **Local models** (fully offline) | Ollama | ✅ `ollama` | `ollama pull qwen2.5-coder:7b` → worker `base_url: http://127.0.0.1:11434/v1` |
| 11 | **CLI coding agents as workers** | free (see [Agent runtimes](agent-runtimes.md)) | ✅ `freebuff`, `aider`, `opencode`, `codex` | put one behind `examples/*_shim.py` and register its URL as a worker |
| 12 | **Docker host** | see below | ✅ `docker` / `docker compose` | `docker compose up --build` |

Anything without a CLI is still scriptable — every endpoint in
[API, MCP and CLI](api.md) has a `curl` and every setting has an env var, which
is all a CI job needs.

**Row 7, what you should see.** Until `CF_PAGES_PROJECT` exists, the *Deploy
dashboard* run finishes with both jobs `skipped` — that is the gate doing its
job, not a broken workflow (the `Deploy dashboard` run appears whenever `web/**`
or the workflow itself changes). After the three values are in place, each push
deploys `web/` and the run prints the environment URL it published:

```text
Cloudflare Pages: https://<branch>.<project>.pages.dev     # preview, every branch
Cloudflare Pages: https://<project>.pages.dev              # production (main)
```

A preview URL per branch, no build minutes to manage, and no server to keep
awake: that one link is the whole hosting story for the dashboard. People who
want to *use* Kollektiv still run the orchestrator themselves — the published
page points at their own API (`?api=https://…`, stored locally).

## 2. Which host (honestly)

Free compute changed in 2025–2026: Heroku's free tier is gone, Fly.io removed its
allowances, Railway is credit-based, and Koyeb narrowed its free offer. What is
actually left, and how it fits Kollektiv's always-on requirement:

| Host | Free? | Fits Kollektiv because | Watch out for |
| --- | --- | --- | --- |
| **Oracle Cloud Always Free** | forever, but **the allowance was halved on 2026-06-15** | still the best free fit: a real VM, `docker compose`, always on | the ARM pool is now **2 OCPUs / 12 GB** for new Always Free tenancies (PAYG accounts keep 4/24); two x86 micro VMs at 1 GB each remain — those are **amd64**, so build the image for `linux/amd64` on them, not ARM; signup wants payment details; idle instances can be reclaimed — keep it busy; you run the OS updates |
| **A machine you own** (spare laptop, mini PC, Raspberry Pi) | yes | zero cost, always on, local by default; expose it with `cloudflared` | your uptime and your backups |
| **Render free web service** | 750 h/mo | one-click Docker deploys | **sleeps after ~15 min idle** → set `CRON_ENABLED=false` and expect cold webhooks; a cron ping or an external pinger helps |
| **Google Cloud Run** | generous meter | scale-to-zero API | not a scheduler: the background loop only runs while an instance is alive |
| **A $4–6 VPS** | no, but cheap | the boring answer that just works | the one line item in the whole project that is not free |
| ~~Fly.io / Railway / Koyeb compute~~ | — | — | free allowances removed or trial-only in 2026; don't plan on them |

Rule of thumb: **webhooks + cron want an always-on box** (Oracle, your own
machine, or a small VPS. A 2 OCPU / 12 GB ARM VM runs the API, the MCP server and
Postgres with room to spare — the memory number is not the constraint here).
Everything else — dashboard, storage, database, auth — is happy on free tiers
because it is reachable on demand.

**Short of a box? Checkpointing does it.** Kollektiv keeps project state in
storage and writes a handoff briefing on demand, so a laptop that is awake a few
hours a day works: `kollektiv run` in the evening, `kollektiv resume` the next
morning. What you lose without an always-on host is *event-driven* work — GitHub
webhooks arriving at 3am — not the orchestrator itself. Two free tiers cover the
rest: Cloudflare Pages for the dashboard, and the GitHub Actions cron already in
`.github/workflows/` for scheduled syncs.

## 3. Before you deploy (once)

- [ ] Local secrets generated: `kollektiv keys` (one command writes `SECRET_KEY`,
      `SESSION_TOKEN` and a `GATEWAY_ADMIN_TOKEN` to `.env`; `kollektiv secret`
      alone still prints just the Fernet key). Back the file up somewhere you can
      restore — `SECRET_KEY` decrypts every stored token, so losing it means
      re-authenticating every account.
- [ ] `GITHUB_TOKEN` + `GITHUB_REPO` set (a fine-grained token with
      `contents: write` and `pull_requests: write` on that repository).
- [ ] `ARENA_ACCOUNTS` filled (or another worker, or none: the heuristic planner
      still works, it just has nothing to dispatch to).
- [ ] Workers are healthy: `kollektiv accounts` lists them, `GET /agents/status?probe=true`
      pings each endpoint.
- [ ] Storage decided: `R2_*` (server deploys) or local workspace (fine to start).
- [ ] `CORS_ORIGINS` includes your dashboard origin if the API and the UI are on
      different hosts.
- [ ] If the API is public: `AUTH_REQUIRED=true` **and** Clerk keys, or keep it
      behind a tunnel/VPN. `/health`, `/docs` and `/webhooks/*` stay public by
      design; `/webhooks/*` verifies its own signatures.
- [ ] `GITHUB_WEBHOOK_SECRET` + the webhook itself: `https://your-host/webhooks/github`,
      events *pushes, pull requests, issues* (TLS is required by GitHub).

## 4. Deploy

```bash
# Docker (any host that runs compose)
cp .env.example .env          # fill it in
docker compose up --build     # API on :8000, dashboard at /ui, MCP on :8001

# or from the package, on a box you control
pip install kollektiv
kollektiv bootstrap           # schema + workspace + the free-tier checklist
kollektiv serve-api           # uvicorn src.api.routes:app --port 8000
```

Behind a tunnel instead of a public IP:

```bash
cloudflared tunnel --url http://localhost:8000
```

Put that `https://…trycloudflare.com` URL in the GitHub webhook and in
`CORS_ORIGINS` if you also host the dashboard on Pages.

**Not a server person?** Two routes skip the box for the *dashboard*: the
Cloudflare Pages link (step 5 above) and the native desktop app
(`desktop/README.md`, installers from **Actions → Desktop installers**). Both
still need an API somewhere for real work — your own machine with `kollektiv
serve-api` is a perfectly good "somewhere", and `cloudflared tunnel --url
http://localhost:8000` gives it a public URL when you want one.

## 5. First real project

A deployment is not proven by `/health` — it is proven by a project that runs:

```bash
kollektiv check                                       # what is configured / degraded
kollektiv run "Build a URL shortener: FastAPI + SQLite + pytest" \
    --name shortener --agents 3
kollektiv status --project-id prj_…                   # tasks, waves, progress
kollektiv resume --project-id prj_…                   # the handoff briefing
```

Watch it live at `https://your-host/ui` (the project drawer streams over SSE).
If a worker is the problem, `GET /agents/status?probe=true` says which one and
why; if it is the brain, `/health` names the missing key.

## 6. Prove it, every time you deploy

```bash
python scripts/smoke.py --base-url https://your-host --token "$CLERK_JWT"
```

Ten checks: health, the bundled dashboard, the four read surfaces the UI needs,
project planning, the state document, the resume briefing and one SSE frame.
Exit `0` all green, `1` a named check failed, `2` nothing is listening. Wire it
into your own cron or your platform's post-deploy hook — `--json` is meant for a
machine.

## 7. Known limits (not bugs, decisions)

| Limit | Why it is fine for now | Tracked as |
| --- | --- | --- |
| No Alembic migrations | The schema is created idempotently at boot; nothing to migrate until you have data you cannot re-create | roadmap item 1 in [Operations](operations.md) |
| Agent output is not sandboxed | Kollektiv writes artifacts, it never executes generated code | issue #15 |
| One process holds the cron schedule | Scale it by running one scheduler instance; the loop is idempotent | `CRON_ENABLED` in [Configuration](configuration.md) |
| Workers are only as good as the model behind them | The orchestrator coordinates; quality comes from the endpoint you point it at | [Agent runtimes](agent-runtimes.md) |
| Hermetic tests, no live-key integration test in CI | CI must not need your credentials; the smoke test is the live one | `scripts/smoke.py` |

## 8. After it is live

- **Tag the release once** the branch is merged: `v0.4.0-beta.1` →
  `.github/workflows/release.yml` publishes the wheel and sdist (see
  [Operations](operations.md#releases-versioning-and-the-readme)).
- **Turn on the repo switches**: [Repository settings](repo-settings.md) — a
  ruleset requiring the CI checks on `main` is what makes "no merging without
  review" mechanical, and both the security policy and Code scanning only light
  up once those files are on the default branch.
- **Watch the first week** through `GET /health`, the `events` table and
  `kollektiv logs`-style tailing: the storage watermark and the worker failure
  counters are the two numbers that move first.
