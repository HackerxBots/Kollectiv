# Examples

Small, runnable extras that are not part of the installed package.

| File | What it does |
| --- | --- |
| `aider_shim.py` | Wraps any CLI coding agent (Aider, OpenHands, Codex CLI, OpenCode, Qwen Code, …) behind an HTTP endpoint so the pool can use it as a worker. Speaks both `POST /prompt` and the OpenAI `POST /v1/chat/completions` shape; `WORKER_TOKEN` guards it, `WORKER_COMMAND` chooses the agent. |

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

Ideas for the next examples (PRs welcome): a Groq/LM-Studio profile, a
docker-compose for a fully offline run with Ollama, and an n8n/Activepieces
workflow that consumes `EVENT_WEBHOOKS`.
