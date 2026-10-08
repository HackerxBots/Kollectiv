# Budgets and `.kollektiv.yml`

Free is the default: local models, free-tier keys and a cheap brain mean a project can
run for nothing. But "free" stops being true the moment you point workers at a
paid endpoint, or the brain at a provider with a real invoice — and nobody wants
to find that out from a statement. So Kollektiv answers three questions up
front: **what will this cost**, **may it run**, and **what did it cost**.

[← Back to the README](../README.md) · [Kollektiv documentation](README.md)

## `.kollektiv.yml` — settings that live with the project

```yaml
# kollektiv init-config writes exactly this, with comments.
project:
  n_agents: 3
  max_concurrency: 3

budget:
  max_usd: 2.50      # refuse a run whose estimate is above this (0 = no cap)
  warn_at: 0.8       # warn from 80% of the cap

brain:
  provider: deepseek
  model: ""
  temperature: 0.3

storage:
  backend: local     # local | r2 | terabox
```

* Lookup: `PROJECT_CONFIG_PATH`, else `.kollektiv.yml` / `.kollektiv.yaml` /
  `.kollektiv.json` in the working directory, then in each parent directory.
* The CLI, the API, the MCP server and the gateway all read the same file, so
  "how this project runs" has one definition.
* YAML is parsed with PyYAML when you have it, and with Kollektiv's own strict
  subset reader when you do not (mappings, lists, scalars, comments, quotes).
  Anything outside that subset — anchors, tags, block scalars, flow mappings —
  is **refused with a line number** instead of guessed at.
* The file always wins over the matching environment variable *for that
  project*. `BUDGET_MAX_USD` is the deployment-wide default; `budget.max_usd`
  is this project's own statement of intent, and the more specific one wins.
* A broken file never stops a run: the problem is logged, listed in
  `ProjectConfig.problems`, and the defaults are used. "The run failed because
  of a typo in a config file" is a worse outcome than "the file was ignored,
  loudly".

## Estimating before you spend

```bash
kollektiv estimate --project-id prj_…        # tokens and dollars, per run
kollektiv run "Add pagination to the API" --dry-run   # plan, estimate, dispatch nothing
kollektiv budget                             # the local ledger: today and all time
```

The estimate is arithmetic on the plan and **your** prices:

| Input | Where it comes from |
| --- | --- |
| Task count, description length | the plan |
| Prompt overhead (+900 tokens/task) | `BUDGET_PROMPT_OVERHEAD_TOKENS` |
| Expected answer size (700 tokens/task) | `BUDGET_OUTPUT_TOKENS_PER_TASK` |
| Brain calls (plan + one review per task + summary) | the orchestrator's own flow |
| Prices per million tokens | `BUDGET_PRICE_*` (defaults: a cheap DeepSeek-class model) |
| Already-spent money | the local ledger |

The response says `estimate_only: true` and never presents itself as a quote.
Worker calls default to **$0** because local models and free-tier keys cost
nothing; point workers at a paid endpoint and set
`BUDGET_WORKER_PRICE_IN_PER_MTOK` / `..._OUT_PER_MTOK` to make the estimate
honest again.

## Caps: refuse, don't surprise

```bash
kollektiv run "…"                    # refused (exit 3) when over budget
kollektiv run "…" --allow-over-budget # the operator's explicit override
```

Two caps, both off by default (`0` = uncapped):

| Cap | Scope | Where |
| --- | --- | --- |
| `budget.max_usd` | one project | `.kollektiv.yml` |
| `BUDGET_MAX_USD` | every project in this deployment | environment |
| `BUDGET_DAILY_MAX_USD` | everything today | environment |

The check happens **before a single task is dispatched**, and the refusal is
specific: it names the estimate, the spend, the cap and the file the cap came
from, then tells you the three ways to proceed. Over the API that refusal is
`402 Payment Required` with the same numbers in the body; in the gateway and the
MCP server it is the same sentence in the tool result. `warn_at` produces a log
line and a `warn` verdict instead — a warning that blocks would be a cap.

## The ledger

Every finished run appends to `budget_ledger`, in this deployment's own
database, keyed by project and day:

```
project_id | day | runs | tasks | brain_calls | tokens in/out | usd | estimated
```

* **Brain tokens are real** when the provider returns a `usage` block (every
  OpenAI-compatible API does), measured as a delta around the run.
* **Worker tokens are estimates**: worker endpoints are chat calls that do not
  report usage, and the row is flagged `estimated: true` so nobody reads a
  number as a measurement it is not.
* The ledger stores tokens and dollars only — never prompts, files, briefs or
  identifiers. It is local and never uploaded.
* `GET /budget`, the `budget_report` MCP tool and `kollektiv budget` all read it.
  `/health` deliberately does **not**: health must answer instantly and never
  query.

## What it deliberately does not do

* **No prediction dressed as a promise.** The estimate is arithmetic on a plan,
  documented term by term, and labelled. A model that generates 10× the tokens
  we guessed costs 10× the estimate.
* **No hard enforcement mid-run.** Once a run starts, its tasks finish; the cap
  applies to the *next* run. Killing a task halfway to save cents would throw
  away the work that was already paid for.
* **No vendor price table.** Kollektiv does not ship a list of provider prices
  that goes stale and misleads. You give the numbers; we do the maths.
* **No central accounting.** No account, no upload, no analytics: the ledger is
  a table in your database, and `kollektiv budget --json` is the export.

## Related

* [Configuration](configuration.md) — every `BUDGET_*` variable
* [API, MCP and CLI](api.md) — `GET /budget`, `GET /projects/{id}/estimate`
* [Bring your own key](byok.md) — which keys the workers use and how they are stored
