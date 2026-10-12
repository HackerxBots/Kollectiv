# Why Kollektiv

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

### What it is, and what it is not

| Kollektiv **is** | Kollektiv **is not** |
| --- | --- |
| An orchestrator that plans, dispatches, collects and reviews work across many agents | An in-editor autocomplete or a single-agent CLI |
| Provider-agnostic: any OpenAI-compatible endpoint, including local models | A wrapper around one vendor's subscription |
| Self-hostable end to end, with free-tier defaults for every dependency | A hosted service you cannot audit |
| Honest about state: every subsystem reports its own health | Silent about what is degraded |

---

---

## Why people run it

- **Free by construction.** Every dependency is optional and every default is
  the zero-cost path: SQLite, local workspace, open API, log-only notifications.
  As keys appear, the same code upgrades in place (R2, Neon, Clerk, Resend,
  Pages) — see [Run it for free](deployment.md#run-it-for-free).
- **Many accounts, one drive.** R2 buckets or TeraBox accounts are pooled into a
  single logical drive, routed by free space and health, so several free
  accounts add up to one large shared volume.
- **Many agents, one team.** Worker endpoints are pooled and scheduled in
  dependency order; a throttled or failing worker is cooled down and routed
  around instead of stalling the run.
- **Your tools, not ours.** Connectors expose Gmail, Calendar, Drive, Notion,
  GitHub, outbound webhooks and *any* JSON API as callable tools for the agents
  (and for your MCP client) — see [Connect your services](connectors.md#connect-your-services).
- **Real source of truth.** GitHub holds the commits; a shared
  `PROJECT_STATE.md` holds the plan, task status and history; `/health` holds
  the truth about what is configured.

---
