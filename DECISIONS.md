# Decisions

The backlog as agreed between the maintainer and the project owner, item by
item: **accepted**, **rejected**, **debating**, or **done**. Accepted items that
are not done yet have a matching GitHub issue; nothing here is a promise without
an owner.

Legend: ✅ accepted · ❌ rejected · 🟡 debating (needs an explanation first) ·
🚀 shipped in this branch.

| # | Suggestion | Verdict | Notes |
| --- | --- | --- | --- |
| 1 | Rename `ARENA_*` → `WORKER_*` | ❌ | Rejected: the variables keep the `ARENA_` prefix. The provider behind each worker is chosen per entry (`provider`), not by the name. |
| 2 | `kollektiv login --provider …` presets | 🚀 | Shipped: bring your own key. Keys are stored encrypted per worker; DeepSeek, Groq, OpenRouter, OpenAI, Gemini, Mistral, Together, Ollama and LM Studio are presets, and `custom` takes any OpenAI-compatible URL. |
| 3 | Role-based model routing (cheap planner, stronger reviewer) | ✅ | Issue #2. |
| 4 | Per-run budget caps (tokens/cost) with abort + alert | 🟡 | Explained below; waiting on a decision. |
| 5 | Per-project `.kollektiv.yml` (agents, models, protected paths) | 🟡 | Explained below; waiting on a decision. |
| 6 | Plan-only "dry run" with a cost estimate | 🟡 | Explained below; waiting on a decision. |
| 7 | `kollektiv connect google` device-flow helper | ✅ | Issue #3. |
| 8 | Drive upload with resumable sessions | ✅ | Issue #4 (the `TODO(rule 9)` in `src/connectors/google_workspace.py`). |
| 9 | Slack/Discord message formatters | ✅ | Issue #5. |
| 10 | Per-action allowlist so agents cannot call `gmail_send` by accident | ✅ | Issue #6. |
| 11 | Connector audit log (`/connectors/calls`) | ✅ | Issue #7. |
| 12 | GitHub write actions (branch/PR/commit) behind confirm + protected paths | ✅ | Issue #8. |
| 13 | Calendar events auto-created from plan waves | ❌ | Rejected for now (low value while runs are short). |
| 14 | MCP resources: expose `PROJECT_STATE.md` as a resource | ✅ | Issue #9. |
| 15 | Ollama/LM Studio compose profile (fully offline) | ✅ | Issue #10. |
| 16 | `POST /workers/probe` — one-token ping per endpoint | ✅ | Issue #11 (connector probes shipped; worker probes next). |
| 17 | Per-provider token bucket (shared rate limits) | ✅ | Issue #12. |
| 18 | Prompt caching for the shared context block | ✅ | Issue #13. |
| 19 | Dedicated reviewer agent that approves merges | ✅ | Issue #14. |
| 20 | Container/WASM sandbox for generated code before commit | ✅ | Issue #15 (the biggest safety win). |
| 21 | Alembic migrations for the Postgres/Neon path | ✅ | Issue #16. |
| 22 | `/metrics` (Prometheus) + Grafana dashboard JSON | ✅ | Issue #17. |
| 23 | Nightly backup task (SQLite + state → R2) with restore command | ✅ | Issue #18. |
| 24 | Leader election for the cron (Postgres advisory lock) | ✅ | Issue #19. |
| 25 | Error digests to `EVENT_WEBHOOKS` / Sentry-compatible DSN | ✅ | Issue #20. |
| 26 | Free-tier watchdog (warn near R2/Resend/Neon limits) | ✅ | Issue #21. |
| 27 | You build the frontend; the API contract stays frozen | 🟡 | Waiting on a decision (the prompt in the chat covers it). |
| 28 | SSE live log stream (`/projects/{id}/events/stream`) | 🟡 | Waiting on a decision. |
| 29 | Diff viewer + per-file approve/reject in the UI | 🟡 | Waiting on a decision. |
| 30 | PWA/offline + command palette | 🟡 | Waiting on a decision (explained below). |
| 31 | `CONTRIBUTING.md`, issue/PR templates, CODE_OF_CONDUCT, SECURITY.md | ✅ | Issue #22. |
| 32 | ~10 "good first issue" entries from the roadmap | ✅ | Done: issues #2–#26 are the initial set (25 issues). |
| 33 | mkdocs-material docs site on Pages | ✅ | Issue #23. |
| 34 | Dependabot + CodeQL + OpenSSF Scorecard | ✅ | Issue #24. |
| 35 | `kollektiv demo` (fake agents, real pipeline) | ✅ | Issue #25. |
| 36 | Compatibility shim: import `kollektiv` alongside `src` | ✅ | Issue #26. |
| 37 | Freebuff (free, ad-funded coding agent) as a worker | 🚀 | `examples/freebuff_shim.py`: strips ads/ANSI, converts its edits into the fenced-block contract, retries only git reads. Caveat shipped in the README: its terms expect a supervised session, so it is one worker you watch — not an unattended fleet. |
| 38 | Use Freebuff to work on Kollektiv itself | ✅ | Free dev agent for contributors who cannot pay for one; it is just a CLI in your terminal, so nothing in the repository needs to change. |
| 39 | Ad-supported "free" tools on the critical path | ❌ | Ruled out. No advertising, sponsor lines or paid tiers in the product. The only support is donations and sponsorship ([support.md](docs/support.md)). |

## Non-negotiables (not up for a vote)

- **Never merge the PR without an explicit "merge it".**
- **No telemetry, ever** — no analytics, identifiers or usage pings, in the API,
  CLI or dashboard.
- **Free on the critical path** — no paid service may become required to run
  Kollektiv, and the free path stays the default.
- **Arena stays the default worker provider**; everything else is optional.
- **Credential handling**: encrypted at rest, masked in logs, never in `.env`
  when `kollektiv login` can hold it.

## How to change this file

Suggestions get a row here the moment they are decided (including rejections, so
they are not re-litigated), and an issue when they are accepted but not done.
Update the table in the same PR as the change.
