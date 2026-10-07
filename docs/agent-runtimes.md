# Agent runtimes

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

## Agent runtimes

**In plain terms.** Kollektiv is the *manager*: it plans, splits the work,
tracks the shared state and pushes to GitHub. A "runtime" is whoever *writes the
code* for one subtask. Any program that accepts a prompt over HTTP and returns
text can be that writer, so the list below is a menu, not a dependency — the
default is an Arena account, and everything else is optional.

| Question | Answer |
| --- | --- |
| What is a "worker"? | One credential entry in `ARENA_ACCOUNTS` pointing at an endpoint + model. The pool runs several in parallel. |
| What is a "runtime"? | The program behind that endpoint: an LLM API, a local model, or a whole CLI agent wrapped in a shim. |
| Default | **Arena accounts** (`kollektiv login` stores the session token encrypted). |
| Optional | Groq, DeepSeek, OpenRouter, Together, local Ollama/vLLM, or a CLI agent (Aider, OpenHands, …) — see the table. |

```bash
kollektiv login                            # Arena, default: paste your session token (encrypted)
kollektiv login --provider groq            # optional: a key instead of an account
kollektiv login --provider ollama          # optional: a local model, no key at all
kollektiv accounts                         # what is stored (tokens masked)
kollektiv connectors --probe               # can everything actually be reached?
```

### Arena accounts are the default

Arena accounts are what Kollektiv was built around, and the flow is deliberately
account-first: `kollektiv login` prompts for the session token from your
signed-in session, encrypts it with `SECRET_KEY` and stores it in the database
(never in `.env`, never in git). `ARENA_ACCOUNTS` then only names the account:

```jsonc
// .env — the token lives in the encrypted store, not here
ARENA_ACCOUNTS='[{"name":"default","base_url":"https://arena.ai","provider":"arena"}]'
```

Kollektiv does not scrape, automate logins, or bypass any tier: a session token
that you supply is used against the endpoint you are authorised to use, rate
limits are respected, and a throttled account is cooled down rather than
hammered. The other providers exist so the project stays usable if an account is
unavailable — not to work around anyone's terms.

### The full menu (optional)

| Runtime | Licence | Why you would plug it in |
| --- | --- | --- |
| **[OpenHands](https://github.com/All-Hands-AI/OpenHands)** | MIT | self-hostable autonomous agent, sandboxed runs, strongest headless/CI story |
| **[Aider](https://github.com/Aider-AI/aider)** | Apache-2.0 | git-native edits; excellent with cheap or local models |
| **[OpenCode](https://github.com/sst/opencode)** | MIT | provider-agnostic terminal agent, 75+ providers including local |
| **[Goose](https://github.com/block/goose)** | Apache-2.0 | MCP-heavy automation (code *and* non-code tasks) |
| **[Cline](https://github.com/cline/cline)** / **[Kilo Code](https://github.com/kilo-org)** | Apache-2.0 / MIT | autonomous edits in the editor; Kilo runs parallel agents |
| **[Qwen Code](https://github.com/QwenLM/qwen-code)** | Apache-2.0 | open fork of the Gemini CLI line, pairs with open-weight models |
| **[Codex CLI](https://github.com/openai/codex)** | Apache-2.0 | sandboxed CLI agent; local models via `--oss` |
| **[Freebuff](https://freebuff.com)** | Apache-2.0 | **free without an API key**, ads fund the models; its file edits become artifacts through `examples/freebuff_shim.py` — one **supervised** session at a time (see the note below) |
| **Local models** (Ollama, llama.cpp, LM Studio, vLLM) | — | zero per-token cost; expose the OpenAI-compatible endpoint as a worker |

#### Wiring one in

The pool speaks OpenAI-compatible HTTP (`base_url` + `model` +
`session_token`), so hosted and local endpoints work as-is:

```jsonc
// ARENA_ACCOUNTS — one entry per worker
[
  {"name": "local-ollama", "session_token": "ollama",
   "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5-coder:32b"},
  {"name": "groq-worker-1", "session_token": "gsk_…",
   "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"}
]
```

For a CLI agent (Aider, OpenHands, Codex CLI), put it behind a tiny HTTP shim
that accepts `{"prompt": …}` and returns `{"text": …}` — a ~40-line FastAPI app —
and register that URL as a worker. Kollektiv keeps planning, dependency
ordering, collection, review, state and sync; the runtime only has to write the
code for one subtask. `examples/aider_shim.py` is a working template, and
`examples/freebuff_shim.py` does the same for [Freebuff](https://freebuff.com) —
the free, ad-supported coding agent — with two extras: it strips terminal ads
and ANSI noise, and it returns **the files the agent changed** as fenced
blocks (Kollektiv's worker contract) instead of the chat transcript.

> **Freebuff is supervised by design.** Its free tier is funded by ads and its
> terms (as reported by reviewers) expect an operator to start a session and
> stay present while the agent works. So it is a perfectly good *free* single
> worker for a project you are watching — and the wrong choice for an
> unattended fleet. Kollektiv supports both patterns; this one is the
> deliberate exception.

**The 3-example minimisation.** Three workers on free endpoints (Groq, a local
Ollama model, a cheap DeepSeek key) cost nothing to start and are enough to see
the whole pipeline work end to end; add accounts as you hit rate limits.

---
