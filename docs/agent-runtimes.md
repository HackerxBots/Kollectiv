# Agent runtimes

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

## Agent runtimes

> **Short version:** a worker is a model you reach with your own key. Kollectiv
> is the manager; the runtime writes the code for one subtask. Everything here is
> bring-your-own-key — see [BYOK](byok.md) for the providers, setup and rules.

**In plain terms.** Kollectiv plans, splits the work, tracks the shared state and
pushes to GitHub. A "runtime" is whoever *writes the code* for one subtask. Any
program that accepts a prompt over HTTP and returns text can be that writer, so
the list below is a menu, not a dependency.

| Question | Answer |
| --- | --- |
| What is a "worker"? | One entry in `ARENA_ACCOUNTS`: a provider, a model, and a key you stored with `kollektiv login`. The pool runs several in parallel. |
| What is a "runtime"? | The program behind that worker: a hosted API, a local model, or a whole CLI agent behind a small HTTP shim. |
| Default | `kollektiv login` defaults to your own Arena.ai account (the original plan). Free alternatives: a free-tier key (Groq) or a **local model** (Ollama, LM Studio), which needs no key and no internet. |
| Optional | Groq, DeepSeek, OpenRouter, Gemini, Mistral, Together, or a CLI agent (Aider, OpenHands, ...) — see the menu. |

```bash
kollektiv login --provider deepseek --name Vega    # prompts for the key, stores it encrypted
kollektiv login --provider ollama   --name Terra   # a local model: no key at all
kollektiv accounts                                  # workers and masked keys
kollektiv check --json | jq .subsystems.agents      # are they ready?
```

### Arena.ai session workers (your own account)

`provider: arena` runs a worker on **your own Arena.ai account**. This is the
original plan: many sessions, each on an account its owner logged in to, working
on one project together.

```bash
kollektiv login --provider arena --name Nova
#   e-mail and password are sent once to ARENA_LOGIN_PATH and not stored, or
kollektiv login --provider arena --name Nova --token <session token from your browser>
kollektiv accounts        # Nova  arena  nova  ready | needs_login | session_expired
```

What Kollectiv does, and what it does not:

* The session is stored encrypted with `SECRET_KEY`, together with its expiry.
  When it runs out, the worker reports `session_expired` and the pool routes
  around it until you log in again.
* A worker posts `{prompt, model, agent_mode}` to `ARENA_CHAT_PATH` on
  `ARENA_BASE_URL`, with its session as a bearer token.
* **The paths are placeholders.** `/api/auth/login` and `/api/chat` are the
  defaults the project started with. Set them to the endpoint you actually use.
  Kollectiv does not verify them against the service.
* **Verification challenges are yours to complete.** Kollectiv does not open a
  browser, does not solve or pass a challenge on your behalf, and does not hand
  work back to a script after one. If the service asks you to verify, do it in
  your own browser, then paste the new session token with `--token`.
* **Rate limits are respected.** A 429 puts that worker in cooldown, and the pool
  gives its task to another worker. Kollectiv does not spread work across extra
  accounts to get past a limit.
* **Terms are your responsibility.** Check that Arena's terms allow what you run
  before you run it, and especially before you run many accounts at once.

The health panel (`GET /agents/health`, and the state column in `kollektiv
accounts`) shows each worker's state, its share of the work so far, and who needs
attention. It reads local state only and never shows a key or a session.

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

Every API worker is OpenAI-compatible HTTP (`provider` + `model` + a key), so hosted
and local endpoints need no special code. `kollektiv login` writes these entries
for you; this is what they look like in `.env` (no keys in the file):

```jsonc
// ARENA_ACCOUNTS — one entry per worker
[
  {"name": "Terra", "provider": "ollama", "account_id": "terra",
   "model": "qwen2.5-coder:14b"},
  {"name": "Atlas", "provider": "groq", "account_id": "atlas",
   "api_key_env": "GROQ_API_KEY"}
]
```

For a CLI agent (Aider, OpenHands, Codex CLI), put it behind a tiny HTTP shim
that speaks `/chat/completions` (a ~40-line FastAPI app), then register that URL with
`provider: custom` and its `base_url`. Kollektiv keeps planning, dependency
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
