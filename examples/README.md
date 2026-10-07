# Examples

Small, runnable extras that are not part of the installed package.

| File | What it does |
| --- | --- |
| `aider_shim.py` | Wraps any CLI coding agent (Aider, OpenHands, Codex CLI, OpenCode, Qwen Code, …) behind an HTTP endpoint so the pool can use it as a worker. Speaks both `POST /prompt` and the OpenAI `POST /v1/chat/completions` shape; `WORKER_TOKEN` guards it, `WORKER_COMMAND` chooses the agent. |
| `freebuff_shim.py` | The same idea specialised for [Freebuff](https://freebuff.com), the free ad-supported agent: strips ads and ANSI noise from the terminal text, then converts `git status` into Kollektiv's fenced-block contract, so the worker's answer is *the files it changed*. Retries only the git reads (never the agent). **One supervised session at a time** — see the module docstring. |

```bash
pip install fastapi uvicorn
WORKER_COMMAND='aider --yes --no-auto-commits --message {prompt}' WORKER_TOKEN=dev \
    uvicorn examples.aider_shim:app --port 8099
```

```jsonc
// .env
ARENA_ACCOUNTS='[{"name":"aider","session_token":"dev",
                  "base_url":"http://127.0.0.1:8099/v1","model":"aider"}]'
```

The Freebuff shim takes its prompt on stdin (or in argv), edits the working
directory and reports the changes:

```bash
FREEBUFF_COMMAND='freebuff' WORKER_WORKDIR=/srv/kollectiv/workspace \
WORKER_PROMPT_VIA=stdin WORKER_TOKEN=dev \
    python examples/freebuff_shim.py            # http://127.0.0.1:8099
```

`freebuff --help` is the source of truth for the non-interactive invocation of
your installed version — the shim deliberately hard-codes no flags it cannot
verify (see the `TODO(rule 9)` in the file).

Ideas for the next examples (PRs welcome): a Groq/LM-Studio profile, a
docker-compose for a fully offline run with Ollama, and an n8n/Activepieces
workflow that consumes `EVENT_WEBHOOKS`.
