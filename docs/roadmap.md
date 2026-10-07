# Performance and roadmap

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

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
12. ~~**Metering per provider** in `/health`~~ — **shipped** as the budget
    ledger plus `GET /budget`, `GET /projects/{id}/estimate` and
    `kollektiv budget` (per-project and per-day totals, brain tokens measured
    from the provider's `usage` block). Per-provider latency is still open.

---

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
- [x] Cost accounting and caps (`.kollektiv.yml`, `BUDGET_MAX_USD`, dry-run estimate)
- [ ] Latency accounting per provider in `/health`
- [ ] A Tauri 2 desktop build (the API as a sidecar) — recipe and honest numbers in
      [Performance](performance.md#browser-or-desktop-app); waiting for a user who
      wants one-click installs and a maintainer willing to sign three platforms
- [x] Installable PWA shell (service worker, install prompt, offline dashboard)
- [ ] A browser-only *demo* mode against a hosted API — the engine itself cannot
      run in a browser (128 MB, 10 ms CPU, no threads, no filesystem)
- [ ] Signed Python wheels + SBOM attached to each release

---
