# Bring your own key (BYOK)

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

Kollektiv is free and open source. Every model it uses is reached with **a key
you already have**, the same way [OpenCode](https://opencode.ai) works: the
software costs nothing, and the provider you chose bills you for the tokens you
use. There is no Kollectiv account, no subscription, and no middleman holding
your credentials.

## The short version

| Question | Answer |
| --- | --- |
| Is it free? | Yes. MIT licence, no paid edition, no hosted tier that is required. |
| Who gets paid for model calls? | The provider you chose (DeepSeek, Groq, OpenAI, a local machine, ...). Kollectiv takes nothing. |
| Do I need internet? | For hosted providers, yes. For a local model (Ollama, LM Studio) the model needs none. See [What needs the internet](#what-needs-the-internet). |
| Where do keys live? | Encrypted with `SECRET_KEY` in the database, or only in an environment variable you name. Never in `.env`, never in git, never printed in full. |
| Can I use a free tier? | Yes, where a provider offers one. Free tiers change, so check the provider's pricing page. |
| Can I mix providers? | Yes. Each worker has its own provider, model and key. The brain can use a different provider again. |

## How OpenCode does it, and how we do the same

OpenCode is the reference point because it is the closest free tool people
already use. Its model is:

- **The client is free and MIT licensed.** It runs on your machine.
- **You bring your own key** for any of its 75+ providers, or connect a local
  model through Ollama or LM Studio.
- **An optional hosted gateway (Zen)** sells pay-as-you-go tokens with no
  subscription. You never have to use it.

Kollectiv follows the same split:

| | OpenCode | Kollectiv |
| --- | --- | --- |
| The software | Free, MIT | Free, MIT |
| Model access | Your key, any provider | Your key, any provider in [the table below](#providers) |
| Local models | Ollama, LM Studio | Ollama and LM Studio are first-class presets |
| Paid hosted gateway | Optional (Zen) | **None.** There is no Kollectiv-hosted model service. |
| Where the key is kept | `~/.local/share/opencode/auth.json` or an env var | Encrypted in your database, or an env var you name |

We do not sell model access, we do not resell anyone's subscription, and we do
not log in to web chat interfaces on anyone's behalf. Workers talk to
**documented APIs** only (see [the rules](#rules)).

## What needs the internet

Kollectiv runs on your machine, so the question is exactly which calls leave it.

| Part | Needs internet? | Notes |
| --- | --- | --- |
| The web dashboard, the API, the MCP server | No | Served from `127.0.0.1` (or your LAN). |
| Local database and workspace | No | SQLite and a folder by default. |
| A worker on **Ollama** or **LM Studio** | No | The model runs on your machine. |
| A worker on a **hosted provider** (DeepSeek, Groq, ...) | Yes | Only the prompt for that worker's task goes to the provider you named. |
| The orchestrator **brain** (planning, review) | Only if `BRAIN_PROVIDER` is hosted | Heuristic mode works offline. |
| GitHub sync, connectors (Notion, Slack, ...) | Yes, when you enable them | Each is optional and off until you set its credentials. |

So a fully offline setup is possible: local models for the workers, `BRAIN`
unset (heuristic mode), no GitHub token. You lose the cloud models, not the
product.

## Providers

These are the presets in [`src/agents/providers.py`](../src/agents/providers.py).
The same table feeds `kollektiv login`, the workers and the brain, so they cannot
disagree. Each one can be overridden per worker with `model` and `base_url`.

| Provider | Kind | Key from | Default model |
| --- | --- | --- | --- |
| `deepseek` | hosted | platform.deepseek.com (`DEEPSEEK_API_KEY`) | `deepseek-chat` |
| `groq` | hosted, free tier | console.groq.com (`GROQ_API_KEY`) | `llama-3.3-70b-versatile` |
| `openrouter` | hosted, includes `:free` models | openrouter.ai/keys (`OPENROUTER_API_KEY`) | `deepseek/deepseek-chat` |
| `openai` | hosted | platform.openai.com (`OPENAI_API_KEY`) | `gpt-4o-mini` |
| `gemini` | hosted, OpenAI-compatible endpoint | aistudio.google.com (`GEMINI_API_KEY`) | `gemini-2.5-flash` |
| `mistral` | hosted | console.mistral.ai (`MISTRAL_API_KEY`) | `mistral-small-latest` |
| `together` | hosted | api.together.xyz (`TOGETHER_API_KEY`) | `deepseek-ai/DeepSeek-V3` |
| `ollama` | **local** | none; run `ollama pull <model>` first | `qwen2.5-coder:7b` |
| `lmstudio` | **local** | none; start LM Studio's local server | `local-model` |
| `custom` | any OpenAI-compatible endpoint | you | you set `base_url` and `model` |

Model names change faster than endpoints. If a provider answers "model not
found", change the `model` on that worker; you do not need a code change.

## Set it up

```bash
kollektiv login --provider deepseek --name Vega        # prompts for the key (hidden)
kollektiv login --provider ollama   --name Terra       # local: nothing to type
kollektiv login --provider groq     --name Atlas --key-env GROQ_API_KEY   # key stays in your env
kollektiv accounts                                      # workers + masked keys
kollektiv check --json | jq .subsystems.agents          # are they ready?
```

`kollektiv login` does two things: it stores the key encrypted with
`SECRET_KEY`, and it writes a worker entry into `ARENA_ACCOUNTS` in your `.env`:

```jsonc
// .env — no key in here, only the names
ARENA_ACCOUNTS='[{"name":"Vega","provider":"deepseek","account_id":"vega"},{"name":"Terra","provider":"ollama","account_id":"terra"}]'
```

Want more than four workers? Run `kollektiv login` again with a new `--name`.
A project can use up to 64 agents, and names are optional: an unnamed worker is
given a name from the curated list.

### Keys from the environment

If you already keep keys in your shell or a secrets manager, pass
`--key-env NAME`. Kollectiv then records only the variable's **name**, reads the
value when it needs it, and stores nothing:

```jsonc
ARENA_ACCOUNTS='[{"name":"Atlas","provider":"groq","account_id":"atlas","api_key_env":"GROQ_API_KEY"}]'
```

Lookup order, first hit wins: the variable named by `api_key_env` (or the
provider's default variable), then the encrypted store.

### Removing and rotating

```bash
kollektiv logout --provider deepseek --account vega    # deletes the key and the worker entry
kollektiv login  --provider deepseek --name Vega       # rotate: run login again, same name
```

After a rotation you do not need a restart. The first task that gets a `401`
from the old key forgets it, and the next task reads the new key from the store
or the environment. Restarting the API also works.

## Rules

1. **Documented APIs only.** Workers call OpenAI-compatible `/chat/completions`
   endpoints. Kollectiv does not log in to web chat UIs, replay browser sessions,
   or scrape an interface. The earlier Arena session-token and browser-bridge
   code was removed for exactly this reason.
2. **Keys go to one place: the provider you named.** A key is sent only in the
   `Authorization` header to that provider's base URL. It is never written to
   logs, to project state, to a commit or to the dashboard.
3. **Rate limits are respected.** A 429 puts that worker in cooldown for the
   `Retry-After` window, and the pool gives its task to another worker.
4. **Nothing is pooled across people.** Your keys serve your workers. Kollectiv
   does not share, resell or aggregate a provider account between users.
5. **You pay your provider directly.** The [budget ledger](budget.md) estimates
   spend per worker and can stop a run at a cap you set.

## Docker and local models

When Kollectiv runs in a container and Ollama runs on the host, the worker's
`base_url` must point at the host, not at the container's own `127.0.0.1`:

```jsonc
ARENA_ACCOUNTS='[{"name":"Terra","provider":"ollama","base_url":"http://host.docker.internal:11434/v1"}]'
```

On Linux, add `--add-host=host.docker.internal:host-gateway` to the container.

## Where to go next

- [Agent runtimes](agent-runtimes.md) — the free menu of CLI agents and how to put one behind a worker shim.
- [Configuration](configuration.md) — every `ARENA_*`, `BRAIN_*` and provider variable.
- [Budget](budget.md) — spend caps for paid providers.
- [Privacy](privacy.md) — what leaves your machine, and what never does.
