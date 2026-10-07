<a name="readme-top"></a>

<div align="center">
  <img src="web/assets/favicon.svg" alt="Kollektiv" width="96">
  <h1 align="center" style="border-bottom: none">Kollektiv</h1>
  <p align="center">
    <strong>A multi-agent collaborative dev team orchestrator — free to run, self-hosted, open source (MIT).</strong>
  </p>
  <p align="center">
    Give it a brief and a few free LLM endpoints. It plans the work, splits it into subtasks,
    runs them in parallel across pooled agents, reviews the output, keeps a shared state
    document in pooled cloud storage, and syncs everything through GitHub.
  </p>
</div>

<div align="center">
  <a href="https://github.com/HackerxBots/Kollektiv/actions/workflows/ci.yml"><img src="https://img.shields.io/badge/status-beta-blue?style=for-the-badge" alt="Project status beta"></a>
  <a href="https://github.com/HackerxBots/Kollektiv/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/HackerxBots/Kollektiv/ci.yml?branch=main&style=for-the-badge" alt="CI status"></a>
  <a href="https://github.com/HackerxBots/Kollektiv/releases"><img src="https://img.shields.io/github/v/release/HackerxBots/Kollektiv?include_prereleases&style=for-the-badge" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue?style=for-the-badge" alt="License MIT"></a>
  <a href="tests/"><img src="https://img.shields.io/badge/tests-303%20passing-brightgreen?style=for-the-badge" alt="Tests"></a>
  <a href="docs/README.md"><img src="https://img.shields.io/badge/Documentation-000?logo=googledocs&logoColor=FFE165&style=for-the-badge" alt="Documentation"></a>
</div>

<div align="center">
  <a href="#quickstart">Quickstart</a> |
  <a href="docs/README.md">Docs</a> |
  <a href="docs/deployment.md#self-hosting-checklist">Self-Hosting</a> |
  <a href="docs/configuration.md">Configuration</a> |
  <a href="docs/faq.md">FAQ</a> |
  <a href="CONTRIBUTING.md">Contributing</a>
</div>

<p align="center">
<em>The whole system, in eight lines:</em>
</p>

```
brief ─► brain (DeepSeek/Groq/local) ─► planner ─► N parallel worker agents ─► collector
                                                          │                        │
                                    Cloudflare R2 (one pooled drive) ◄── sync ── GitHub
                          optional free tiers: Neon (db) · Clerk (auth) · Resend (email) · Pages (UI)
                          connectors: GitHub · Google · Notion · Webhooks · any REST API
```

<hr>

Kollektiv turns a project brief into a working dev team made of things you
already have. One process plans the work and splits it into subtasks with
dependencies; a pool of LLM endpoints (several accounts on one provider, or many
providers) executes those subtasks in parallel waves; the results are collected,
reviewed, merged and pushed through GitHub. The plan, the task status and the
history live in a single shared `PROJECT_STATE.md` on pooled free storage, so
any session — including the next one — can pick the project up exactly where it
stopped.

It is a **coordinator, not a worker**: point it at Arena accounts, Groq, a local
Ollama model, or a shim around a CLI agent like Aider or Freebuff, and it will
keep them all busy, route around the slow or throttled ones, and keep the state
that makes the effort compound.

| | |
| --- | --- |
| [**Free by construction**](docs/deployment.md#run-it-for-free) | Every default is the zero-cost path (SQLite, local workspace, no auth, log-only notifications); each free tier upgrades the same code in place. |
| [**Many accounts, one drive**](docs/deployment.md#one-storage-layer-two-providers-and-the-9drive-trick) | Cloudflare R2 buckets or TeraBox accounts are pooled into one logical volume, routed by free space and health. |
| [**Many agents, one team**](docs/agent-runtimes.md) | Worker endpoints are pooled and scheduled in dependency order; a failing worker is cooled down and routed around, not retried into the ground. |
| [**Your tools, not ours**](docs/connectors.md) | Gmail, Calendar, Drive, Notion, GitHub, outbound webhooks and *any* JSON API become callable tools for the agents and your MCP client. |
| [**Nothing lost between sessions**](docs/architecture.md#session-continuity-never-lose-the-good-part) | `kollektiv resume`, `GET /projects/{id}/handoff` and `HANDOFF.md`: what is done, what is next, which blockers, and the exact next commands. |
| [**No telemetry, ever**](docs/privacy.md) | No analytics, no accounts, no identifiers, nothing to opt out of. The only traffic is to endpoints you configure. |

> **Is Kollektiv another coding agent?** No. It does not autocomplete, and it does
> not run one smart model — it organises the free agents you already have into a
> team, and keeps the shared memory that a team needs. See
> [Why Kollektiv](docs/why-kollektiv.md).

## Quickstart

**Option 1 — install the package** (fastest; Python 3.11+):

```bash
pip install kollektiv
kollektiv bootstrap          # schema, workspace, free-tier checklist
kollektiv check              # exactly what is configured or degraded
kollektiv serve-api          # dashboard at http://localhost:8000/ui, docs at /docs
```

**Option 2 — clone** (recommended for the dashboard, docs and hacking on it):

```bash
git clone https://github.com/HackerxBots/Kollektiv.git
cd Kollectiv
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env && kollektiv secret              # put the output in SECRET_KEY
kollektiv serve-api
```

**Give it something to build.** With no keys at all you get the heuristic
planner; add workers when you have them:

```bash
kollektiv run "Build a URL shortener: FastAPI service, SQLite storage, CLI and pytest tests" \
    --name shortener --agents 3
kollektiv status --project-id prj_…     # the shared state document
kollektiv resume --project-id prj_…     # continue it in a new session
python scripts/smoke.py                 # 10 checks: is this deployment actually working?
```

**Dashboards and clients.** `http://localhost:8000/ui` serves the bundled static
dashboard (four views, live SSE updates, ⌘K palette — see [`web/`](web/README.md));
deploying it on Cloudflare Pages is a three-click job. The same API is exposed to
MCP clients with `python -m src.api.mcp_server`.

<details>
<summary>Where do the keys go? (one line each)</summary>

| Capability | Variable(s) | Free source |
| --- | --- | --- |
| Worker agents | `ARENA_ACCOUNTS` (JSON list) | Arena accounts are the default; any OpenAI-compatible endpoint works |
| Brain (planner/reviewer) | `BRAIN_API_KEY` (+ optional `BRAIN_BASE_URL`) | DeepSeek by default, Groq as a fallback; local models work |
| Shared storage | `R2_*` or `TERABOX_ACCOUNTS` | Cloudflare R2 (10 GB, no egress) or TeraBox |
| Database | `DATABASE_URL` | SQLite by default; Neon Postgres free tier for a server |
| Auth | `CLERK_*` + `AUTH_REQUIRED` | Clerk free tier (optional; the API is open by default on localhost) |
| Email | `RESEND_API_KEY` + `NOTIFY_EMAILS` | Resend free tier (optional) |
| GitHub sync | `GITHUB_TOKEN`, `GITHUB_REPO` | your own repository |

Full reference: [Configuration](docs/configuration.md) and `.env.example`.
</details>

## Documentation

- [Why Kollektiv](docs/why-kollektiv.md) — the idea, and what it is not
- [Architecture](docs/architecture.md) — a run end to end, the shared state, the layout
- [Configuration](docs/configuration.md) — every setting, with examples
- [Deploy checklist](docs/deploy-checklist.md) — accounts, CLIs, hosts that actually fit, and `scripts/smoke.py`
- [Deployment and the free stack](docs/deployment.md) — Compose, bare metal, free hosting, self-hosting checklist
- [Agent runtimes](docs/agent-runtimes.md) — Arena by default, and the whole free menu
- [Connectors](docs/connectors.md) — Google, Notion, GitHub, webhooks, any REST API
- [API, MCP and CLI](docs/api.md) — endpoints, tools and commands
- [Operations](docs/operations.md) — releases, health, sync, scaling, maintainer settings
- [Extending Kollektiv](docs/extending.md) · [Development](docs/development.md) · [Troubleshooting and FAQ](docs/faq.md)
- [Performance and roadmap](docs/roadmap.md) · [Privacy](docs/privacy.md) · [Legal](docs/legal.md)

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md)
(three gates: `pytest -q`, `ruff check .`, `mypy src config examples scripts`), the
[code of conduct](CODE_OF_CONDUCT.md), and the [security policy](SECURITY.md) for
vulnerability reports. `docs/` and `CHANGELOG.md` ship with the change.

## License

MIT — see [LICENSE](LICENSE). Kollektiv ships **no** code to scrape services,
bypass paywalls or evade rate limits; you are responsible for what your agents
produce and for the terms of every service you connect
([Legal & responsible use](docs/legal.md)).

<div align="center">
  <sub>Free to run · self-hosted · no telemetry · <a href="#readme-top">back to top</a></sub>
</div>
