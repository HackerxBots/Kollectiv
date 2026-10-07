# Kollektiv documentation

Start with the [README](../README.md) for the pitch and the 60-second quickstart,
then come here for depth. Every page is self-contained and links back.

| Page | What is in it |
| --- | --- |
| [Why Kollektiv](why-kollektiv.md) | The idea, what it is and is not, and the five reasons people run it. |
| [Quickstart](../README.md#quickstart) | Install, first project, dashboard. (In the README, so a newcomer reads one page.) |
| [Architecture](architecture.md) | How a run works, the shared `PROJECT_STATE.md`, session continuity (handoff), and the full project layout. |
| [Configuration](configuration.md) | Every setting: workers, storage, GitHub, brain, runtime, with examples. |
| [Deployment and the free stack](deployment.md) | Docker Compose, bare metal, the free-hosted path (R2 + Neon + Clerk + Resend + Pages) and the self-hosting checklist. |
| [Deploy checklist](deploy-checklist.md) | The order of operations: which accounts to create (and which have a CLI), which host actually fits in 2026, the first real project, and `scripts/smoke.py`. |
| [Agent runtimes](agent-runtimes.md) | Arena accounts as the default, plus the full free menu (Groq, Ollama, CLI agents) and how to wire one in. |
| [Connectors](connectors.md) | GitHub, Google, Notion, webhooks, declarative REST, calling them from MCP, and the safety rules. |
| [API, MCP and CLI](api.md) | HTTP endpoints, the MCP tool server, and every CLI command. |
| [Operations](operations.md) | Releases and versioning, health and observability, background sync, storage hygiene, scaling, and the maintainer repository checklist. |
| [Extending Kollektiv](extending.md) | New worker shapes, brain providers, storage backends and tools. |
| [Development](development.md) | Dev setup, design decisions and testing notes. |
| [Troubleshooting and FAQ](faq.md) | Symptom → cause → fix, and the answers to the questions people actually ask. |
| [Performance and roadmap](roadmap.md) | The honest bottlenecks and what ships next. |
| [Privacy](privacy.md) | No telemetry, no accounts, no data collection — and how to verify it. |
| [Legal and responsible use](legal.md) | What Kollektiv deliberately does not do. |
| [Repository settings](repo-settings.md) | Maintainer pass: description, topics, security toggles, rulesets. |
| [Frontend prompt](ui-prompt.md) | The prompt that (re)builds `web/`, the dashboard in this repository. |

**Conventions.** Docs are written for the person who has 10 minutes, not 2 hours:
the first paragraph of a page says what it is for, tables beat prose, and every
command is copy-pasteable. If a page and the code disagree, the code is right —
[open an issue](https://github.com/HackerxBots/Kollektiv/issues) so the page is
fixed.

Screenshots and diagrams are text (code blocks) rather than images: they diff,
they are searchable, and they keep the repository free of binary blobs.
