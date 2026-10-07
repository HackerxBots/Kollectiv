# Architecture

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

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
examples/freebuff_shim.py      run the free, ad-supported Freebuff agent as a worker
                              (strips ads, returns its edits as artifact blocks)
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
tests/                         354 hermetic tests (no network, no credentials)
```

---

---

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
