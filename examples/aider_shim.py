"""Wrap any CLI coding agent as a Kollektiv worker endpoint.

Kollektiv workers are HTTP endpoints that accept a prompt and return text (the
OpenAI-compatible shape, or the simple ``{"prompt": …}`` / ``{"text": …}`` shape
used here). This shim runs a CLI agent — Aider, OpenHands, Codex CLI, OpenCode,
Qwen Code, … — in a scratch directory and returns whatever it printed, so a
whole agent can be one worker in the pool.

Run it:

```bash
pip install fastapi uvicorn
WORKER_COMMAND='aider --yes --no-auto-commits --no-stream --message {prompt}' \\
WORKER_TOKEN=local-dev-token \\
    uvicorn examples.aider_shim:app --host 127.0.0.1 --port 8099
```

then register it in ``.env``:

```jsonc
ARENA_ACCOUNTS='[{"name":"aider","session_token":"local-dev-token",
                  "base_url":"http://127.0.0.1:8099/v1","model":"aider"}]'
```

The shim speaks two dialects:

* ``POST /v1/chat/completions`` — the OpenAI shape the pool prefers.
* ``POST /prompt`` — the minimal ``{"prompt": …}`` → ``{"text": …}`` shape.

Security: it executes a command with your prompt as an argument, so it is meant
for ``127.0.0.1`` or a private network, guarded by ``WORKER_TOKEN``. Prompts are
passed as a single argv element (never through a shell) unless
``WORKER_SHELL=1`` is set, which is only for trusted, local experiments.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

#: Command template. ``{prompt}`` is replaced by the prompt (quoted for argv).
COMMAND = os.environ.get("WORKER_COMMAND", "aider --yes --no-auto-commits --message {prompt}")
#: Optional bearer token clients must present (empty disables the check).
TOKEN = os.environ.get("WORKER_TOKEN", "")
#: Working directory for the command; a scratch dir keeps repos untouched.
WORKDIR = os.environ.get("WORKER_WORKDIR", "") or None
#: Seconds before a run is killed.
TIMEOUT = float(os.environ.get("WORKER_TIMEOUT", "900"))
#: Set to 1 to run through the shell (only for commands that need pipes).
USE_SHELL = os.environ.get("WORKER_SHELL", "") == "1"

app = FastAPI(title="Kollektiv CLI worker shim", version="0.1.0")


class ChatMessage(BaseModel):
    """One OpenAI-style chat message."""

    role: str = "user"
    content: str = ""


class ChatRequest(BaseModel):
    """The subset of the OpenAI chat completion request the pool sends."""

    model: str = "shim"
    messages: List[ChatMessage] = Field(default_factory=list)
    stream: bool = False


class PromptRequest(BaseModel):
    """The minimal dialect."""

    prompt: str


def _check_auth(authorization: Optional[str]) -> None:
    """Reject callers that do not present ``WORKER_TOKEN`` (when configured)."""
    if not TOKEN:
        return
    expected = f"Bearer {TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid worker token")


def _prompt_from(body: ChatRequest) -> str:
    """Flatten chat messages into one prompt for a CLI agent."""
    return "\n\n".join(message.content for message in body.messages if message.content)


async def _run(command: str, prompt: str) -> Dict[str, Any]:
    """Execute the CLI agent with ``prompt`` and capture its output."""
    argv = shlex.split(command.replace("{prompt}", prompt))
    started = time.perf_counter()
    try:
        if USE_SHELL:
            process = await asyncio.create_subprocess_shell(
                command.replace("{prompt}", shlex.quote(prompt)),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=WORKDIR,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=WORKDIR,
            )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"WORKER_COMMAND is not executable: {exc}",
        ) from exc

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"the agent did not finish within {TIMEOUT:.0f}s",
        ) from exc

    text = (stdout or b"").decode("utf-8", "replace")
    elapsed = time.perf_counter() - started
    return {
        "text": text,
        "exit_code": process.returncode,
        "seconds": round(elapsed, 2),
    }


@app.get("/health")
async def health() -> Dict[str, Any]:
    """Report the shim's configuration (no secrets)."""
    return {
        "status": "ok",
        "command": COMMAND.split()[0],
        "workdir": WORKDIR or os.getcwd(),
        "timeout": TIMEOUT,
        "auth": bool(TOKEN),
    }


@app.post("/prompt")
async def prompt(body: PromptRequest, authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """Return the agent's raw output for one prompt."""
    _check_auth(authorization)
    if not body.prompt.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="prompt is empty")
    return await _run(COMMAND, body.prompt)


@app.post("/v1/chat/completions")
async def chat_completions(
    body: ChatRequest, authorization: Optional[str] = Header(default=None)
) -> Dict[str, Any]:
    """OpenAI-compatible endpoint so the pool can use this shim as a worker."""
    _check_auth(authorization)
    prompt = _prompt_from(body)
    if not prompt.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no message content")
    result = await _run(COMMAND, prompt)
    return {
        "id": f"chatcmpl-shim-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result["text"]},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


if __name__ == "__main__":  # pragma: no cover - manual entry point
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8099)
