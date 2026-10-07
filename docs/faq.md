# Troubleshooting and FAQ

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

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
yourself (see [Self-hosting checklist](deployment.md#self-hosting-checklist)).

**Can I use something other than Arena for the workers?** Yes, and most people
do: any OpenAI-compatible endpoint works as a worker, so Groq, Together,
OpenRouter, DeepSeek, a local Ollama/vLLM server, or a shim around a CLI agent
(OpenHands, Aider, OpenCode, Goose, Cline/Kilo, Qwen Code, Codex CLI, Freebuff) all plug
in through `ARENA_ACCOUNTS` — see [Agent runtimes](agent-runtimes.md#agent-runtimes). The same is
true for connectors: the four built-ins and `CUSTOM_CONNECTORS` cover most of
what "linking services" means, and Activepieces/n8n/Zapier can be reached
through `EVENT_WEBHOOKS` or their own REST APIs.

**Do I need any keys to start?** No. With an empty `.env` you get the heuristic
planner, a pool with zero workers, local-workspace storage, an open API and no
emails. Everything reports itself in `/health`, `kollektiv check` and
`kollektiv connectors`, so you can add one credential at a time.

**Is any specific agent required — Freebuff, Aider, Arena?** No agent is
required to *build or run* Kollektiv: it is a plain Python package (FastAPI,
SQLModel, httpx, one optional `mcp`), so no particular coding agent appears
anywhere in the install or the test suite, and the planner has a built-in
heuristic mode that works with an empty worker pool. A *worker* is only needed
to generate code: pick one or more that cost you nothing — Arena accounts are
the documented default, and Groq, a local Ollama model or a Freebuff/Aider shim
are interchangeable alternatives. Freebuff is a convenience for "no API key, no
card" situations, never a dependency.

**Is every piece really free?** Yes, and there is no paid component on the
critical path: Cloudflare R2 (10 GB, no egress), Neon, Clerk, Resend and Pages
all have usable free tiers, and Kollektiv runs with none of them.

**How do I run it completely offline?** `BRAIN_API_KEY=` empty,
`ARENA_ACCOUNTS=[]` — you get the heuristic planner and a pool with no workers
(`run` will tell you so). Tests cover exactly this mode.

---
