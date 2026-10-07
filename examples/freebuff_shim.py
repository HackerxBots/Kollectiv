"""Run the Freebuff CLI as a Kollektiv worker — supervised, ad-filtered, diffed.

[Freebuff](https://freebuff.com) is a **free, ad-supported coding agent** (the
Apache-2.0 Codebuff multi-agent framework, CLI + desktop + web). It has no HTTP
API and no API key, so it cannot be an ``ARENA_ACCOUNTS`` entry on its own; it
becomes a worker through this shim, exactly like Aider or OpenCode.

Why this shim is more than a wrapper:

* **Its edits become Kollektiv artifacts.** Freebuff changes files in the
  working directory the way a coding agent should, so instead of shipping its
  chat text the shim collects ``git status``/``git diff`` and returns the
  changes in Kollektiv's worker contract — fenced blocks tagged with a path
  (`` ```python path=src/app.py ``). The collector then merges, reviews and
  commits them like any other worker's output.
* **Ads never reach the artifacts.** The free tier pays for itself with text
  ads in the terminal; :func:`strip_ads` removes ad lines that are outside code
  fences, and :func:`strip_ansi` removes the colour codes every TUI emits.
* **It respects the terms.** Freebuff's terms (as reported by reviewers) require
  an operator to start a session and stay present while the agent works. This
  shim is therefore for **one supervised project at a time** — it deliberately
  does not fake parallelism, and the README says so. Do not point a fleet of
  agents at it.

Run it:

```bash
pip install fastapi uvicorn
FREEBUFF_COMMAND='freebuff' WORKER_PROMPT_VIA=stdin \\
WORKER_WORKDIR=/srv/kollektiv/workspace WORKER_TOKEN=local-dev-token \\
    python examples/freebuff_shim.py
```

then register the worker in ``.env``:

```jsonc
ARENA_ACCOUNTS='[{"name":"freebuff","session_token":"local-dev-token",
                  "base_url":"http://127.0.0.1:8099/v1","model":"freebuff"}]'
```

Configuration (all optional except the command, which the operator must verify):

======================================  ==========================================
``FREEBUFF_COMMAND``                    Invocation to run. ``{prompt}`` is
                                        replaced for argv mode.
``WORKER_PROMPT_VIA``                   ``stdin`` (default) or ``argv``.
``WORKER_WORKDIR``                      Directory the agent edits (default: the
                                        current directory).
``WORKER_TIMEOUT``                      Seconds per run (default 1800).
``WORKER_COLLECT_CHANGES``              ``1`` (default) to return the git diff
                                        instead of the raw terminal text.
``WORKER_DROP_PATTERNS``                Extra regexes, ``;``-separated, for
                                        lines that must never reach an artifact.
``WORKER_TOKEN``                        Bearer token clients must present.
======================================  ==========================================

TODO(rule 9): the exact non-interactive invocation could not be verified against
a live installation, so it is *not* hard-coded here. Run ``freebuff --help``,
find the one-shot/print flag or the prompt-on-stdin behaviour of your version,
and put the full command in ``FREEBUFF_COMMAND``. Anything else is a guess, and
a wrong guess would look like a broken worker instead of a wrong command.

The only network access is the CLI's own; the shim itself makes none.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from src.utils.paths import safe_relative_path
from src.utils.retry import async_retry

#: Command to run. See the TODO above: verify it against your installation.
COMMAND = os.environ.get("FREEBUFF_COMMAND", "freebuff")
#: ``stdin`` pipes the prompt in; ``argv`` substitutes ``{prompt}`` into COMMAND.
PROMPT_VIA = os.environ.get("WORKER_PROMPT_VIA", "stdin").strip().lower()
#: Directory the agent edits (the project workspace).
WORKDIR = os.environ.get("WORKER_WORKDIR", "") or os.getcwd()
#: Seconds before a run is killed. Agentic CLIs are slow; be generous.
TIMEOUT = float(os.environ.get("WORKER_TIMEOUT", "1800"))
#: Return the git diff (Kollektiv's contract) instead of the terminal text.
COLLECT_CHANGES = os.environ.get("WORKER_COLLECT_CHANGES", "1") == "1"
#: Bearer token clients must present (empty disables the check).
TOKEN = os.environ.get("WORKER_TOKEN", "")
#: Extra ad/host-noise patterns, ``;``-separated.
EXTRA_DROP_PATTERNS = [item for item in os.environ.get("WORKER_DROP_PATTERNS", "").split(";") if item.strip()]

#: Files larger than this are listed but not inlined into an artifact.
MAX_INLINE_BYTES = 256 * 1024
#: Extensions Kollektiv's collector understands, mapped to a fence language.
LANGUAGES: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".json": "json",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
    ".sh": "bash",
    ".sql": "sql",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".txt": "text",
}

#: Ad and banner patterns for the free tier's terminal output. Kept narrow on
#: purpose: only lines that cannot plausibly be source code are dropped, and
#: nothing inside a fenced block is ever touched.
AD_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*[\[(│|]?\s*(ad|ads|advert|advertisement|sponsored)\b[:.\]\s-]", re.IGNORECASE),
    re.compile(r"\bsponsored by\b", re.IGNORECASE),
    re.compile(r"\bfreebucks?\b", re.IGNORECASE),
    re.compile(r"^\s*[│|]\s*(upgrade|go pro|try pro|pricing)\b", re.IGNORECASE),
    re.compile(r"^\s*[─━═]{4,}\s*$"),
)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_FENCE = re.compile(r"^\s*(```|~~~)")
#: Lines longer than this are kept even if they match an ad pattern: real ads
#: are one-liners, and dropping a long line risks losing code.
MAX_AD_LINE_LENGTH = 200

app = FastAPI(title="Kollektiv Freebuff worker shim", version="0.1.0")


# ----------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------
class ChatMessage(BaseModel):
    """One OpenAI-style chat message."""

    role: str = "user"
    content: str = ""


class ChatRequest(BaseModel):
    """The subset of the OpenAI chat completion request the pool sends."""

    model: str = "freebuff"
    messages: List[ChatMessage] = Field(default_factory=list)
    stream: bool = False


class PromptRequest(BaseModel):
    """The minimal dialect."""

    prompt: str


# ----------------------------------------------------------------------
# Text hygiene
# ----------------------------------------------------------------------
def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from terminal output.

    Args:
        text: Raw bytes decoded from the CLI.

    Returns:
        The same text without colour, cursor or OSC sequences.
    """
    cleaned = _ANSI.sub("", text.replace("\r\n", "\n"))
    return "\n".join(line.rstrip() for line in cleaned.split("\n"))


def strip_ads(text: str, extra: Optional[Sequence[str]] = None) -> str:
    """Drop advertisement and upgrade-banner lines, but never code.

    Lines inside a fenced block (``` or ~~~) are always kept, as are lines
    longer than :data:`MAX_AD_LINE_LENGTH` — a real ad is a one-liner, and
    cutting a long line risks deleting code.

    Args:
        text: Output already passed through :func:`strip_ansi`.
        extra: Additional regexes from ``WORKER_DROP_PATTERNS``.

    Returns:
        The text with ad lines removed and consecutive blank lines collapsed.
    """
    patterns = list(AD_PATTERNS)
    for candidate in extra or EXTRA_DROP_PATTERNS:
        try:
            patterns.append(re.compile(candidate))
        except re.error:
            # A bad operator pattern must not break the worker.
            continue
    kept: List[str] = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            kept.append(line)
            continue
        if not in_fence and len(line) <= MAX_AD_LINE_LENGTH and any(
            pattern.search(line) for pattern in patterns
        ):
            continue
        kept.append(line)
    collapsed: List[str] = []
    for line in kept:
        if line.strip() == "" and collapsed and collapsed[-1].strip() == "":
            continue
        collapsed.append(line)
    return "\n".join(collapsed).strip("\n")


def language_for(path: str) -> str:
    """Return a fence language for a file path.

    Args:
        path: Repository-relative path.

    Returns:
        A language identifier for the fenced block (``text`` when unknown).
    """
    return LANGUAGES.get(Path(path).suffix.lower(), "text")


# ----------------------------------------------------------------------
# The worker output contract
# ----------------------------------------------------------------------
def build_artifact(files: Sequence[Tuple[str, str]], *, notes: str = "") -> str:
    """Render changed files as Kollektiv's fenced-block contract.

    Args:
        files: ``(relative_path, content)`` pairs.
        notes: Optional text to prepend (e.g. files that were skipped).

    Returns:
        Markdown the collector can parse: one fenced block per file, tagged with
        ``path=``, which is the contract ``src/orchestrator/collector.py`` reads.
    """
    parts: List[str] = []
    if notes.strip():
        parts.append(notes.strip())
    for path, content in files:
        language = language_for(path)
        parts.append(f"```{language} path={path}\n{content.rstrip()}\n```")
    return "\n\n".join(parts).strip() + "\n"


async def _git(args: Sequence[str], *, cwd: str) -> str:
    """Run one git command in ``cwd`` and return its stdout.

    Git is retried on failure: index lock contention is transient and common
    when the agent and the shim touch the repository in quick succession.

    Args:
        args: Arguments after ``git``.
        cwd: Repository directory.

    Returns:
        Standard output, decoded.

    Raises:
        RuntimeError: When git is missing or the command fails.
    """

    @async_retry(max_retries=2, base_delay=0.2, max_delay=2.0, retry_on=(RuntimeError,))
    async def _run() -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {stderr.decode('utf-8', 'replace').strip()}")
        return stdout.decode("utf-8", "replace")

    return await _run()


async def collect_changes(cwd: str) -> Tuple[List[Tuple[str, str]], str]:
    """Turn the working-tree changes under ``cwd`` into artifact files.

    Untracked and modified text files are read from disk and validated with
    :func:`src.utils.paths.safe_relative_path`, so a path the agent invented
    cannot escape the workspace. Deleted files, files over
    :data:`MAX_INLINE_BYTES` and unreadable files are reported in the notes
    instead of being inlined.

    Args:
        cwd: Repository directory the agent worked in.

    Returns:
        ``(files, notes)`` where ``files`` is a list of
        ``(relative_path, content)`` pairs.
    """
    status_output = await _git(["status", "--porcelain", "--untracked-files=all"], cwd=cwd)
    files: List[Tuple[str, str]] = []
    skipped: List[str] = []
    for line in status_output.splitlines():
        if len(line) < 4:
            continue
        code, raw_path = line[:2].strip(), line[3:].strip()
        if raw_path.startswith('"') and raw_path.endswith('"'):
            raw_path = raw_path[1:-1]
        if code.startswith("D"):
            skipped.append(f"{raw_path} (deleted)")
            continue
        try:
            relative = safe_relative_path(raw_path, label="changed file path")
        except ValueError as exc:
            skipped.append(f"{raw_path} (rejected: {exc})")
            continue
        candidate = Path(cwd) / relative
        try:
            size = candidate.stat().st_size
        except OSError:
            skipped.append(f"{relative} (unreadable)")
            continue
        if size > MAX_INLINE_BYTES:
            skipped.append(f"{relative} ({size} bytes, not inlined)")
            continue
        try:
            content = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped.append(f"{relative} (binary or unreadable)")
            continue
        files.append((relative, content))
    notes = ""
    if skipped:
        notes = "Files the shim did not inline:\n" + "\n".join(f"- {item}" for item in skipped)
    return files, notes


# ----------------------------------------------------------------------
# Running the agent
# ----------------------------------------------------------------------
def build_argv(command: str, prompt: str) -> Tuple[List[str], bool]:
    """Split the command template and decide how the prompt is delivered.

    The template is split *before* substitution so the prompt always stays one
    argv element: a prompt with spaces, quotes or newlines cannot be re-split
    into extra flags, and nothing is ever handed to a shell.

    Args:
        command: ``FREEBUFF_COMMAND``, optionally containing ``{prompt}``.
        prompt: The prompt to hand to the agent.

    Returns:
        ``(argv, pipe_prompt)`` — argv to execute, and whether the prompt should
        be written to stdin instead of being part of argv.
    """
    if PROMPT_VIA == "argv" or "{prompt}" in command:
        parts = shlex.split(command)
        if any("{prompt}" in part for part in parts):
            return [part.replace("{prompt}", prompt) for part in parts], False
        return [*parts, prompt], False
    return shlex.split(command), True


def resolve_executable(argv: Sequence[str]) -> List[str]:
    """Make a relative program path work regardless of the working directory.

    ``asyncio.create_subprocess_exec`` resolves a relative program against the
    process's ``cwd`` — which this shim sets to ``WORKER_WORKDIR``, so an
    operator who started it from the repository and wrote ``.venv/bin/python``
    would get a confusing "not executable". Resolving against the directory the
    shim itself runs in matches that intent; bare names (``freebuff``) are left
    alone so ``PATH`` keeps working.

    Args:
        argv: The command and its arguments.

    Returns:
        A new argv with the program made absolute when that is unambiguous.
    """
    if not argv:
        return []
    program = argv[0]
    if os.sep in program and not os.path.isabs(program):
        candidate = Path.cwd() / program
        if candidate.exists():
            return [str(candidate), *argv[1:]]
    return list(argv)


async def run_agent(prompt: str, *, cwd: Optional[str] = None) -> Dict[str, Any]:
    """Run the Freebuff CLI once and return its output and file changes.

    The agent itself is never retried automatically: each run edits the working
    tree, so a blind retry would re-do (and possibly re-break) real work on a
    prompt that is no longer a footnote in history. Git reads *are* retried.

    Args:
        prompt: The task the agent should perform.
        cwd: Directory to run in (defaults to ``WORKER_WORKDIR``).

    Returns:
        ``{text, artifact, files, exit_code, seconds}``.

    Raises:
        HTTPException: 500 when the command is missing, 504 on timeout.
    """
    directory = cwd or WORKDIR
    argv, pipe_prompt = build_argv(COMMAND, prompt)
    argv = resolve_executable(argv)
    started = time.perf_counter()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=directory,
            stdin=asyncio.subprocess.PIPE if pipe_prompt else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"FREEBUFF_COMMAND is not executable: {exc}. Bare names are looked up "
                f"on PATH and run in {directory}; give an absolute path for anything else."
            ),
        ) from exc

    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(prompt.encode("utf-8") if pipe_prompt else None), timeout=TIMEOUT
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"the agent did not finish within {TIMEOUT:.0f}s",
        ) from exc

    raw = strip_ansi((stdout or b"").decode("utf-8", "replace"))
    text = strip_ads(raw)
    files: List[Tuple[str, str]] = []
    notes = ""
    if COLLECT_CHANGES:
        try:
            files, notes = await collect_changes(directory)
        except RuntimeError as exc:
            notes = f"Could not read the working tree: {exc}"
    artifact = build_artifact(files, notes=notes) if files else text
    return {
        "text": text,
        "artifact": artifact,
        "files": [path for path, _ in files],
        "exit_code": process.returncode,
        "seconds": round(time.perf_counter() - started, 2),
    }


# ----------------------------------------------------------------------
# HTTP surface
# ----------------------------------------------------------------------
def _check_auth(authorization: Optional[str]) -> None:
    """Reject callers that do not present ``WORKER_TOKEN`` (when configured)."""
    if not TOKEN:
        return
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid worker token")


def _prompt_from(body: ChatRequest) -> str:
    """Flatten chat messages into one prompt for a CLI agent."""
    return "\n\n".join(message.content for message in body.messages if message.content)


@app.get("/health")
async def health() -> Dict[str, Any]:
    """Report the shim's configuration (never the token)."""
    return {
        "status": "ok",
        "command": COMMAND,
        "prompt_via": "argv" if (PROMPT_VIA == "argv" or "{prompt}" in COMMAND) else "stdin",
        "workdir": WORKDIR,
        "timeout": TIMEOUT,
        "collect_changes": COLLECT_CHANGES,
        "auth": bool(TOKEN),
        "note": "one supervised session per run; see the module docstring",
    }


@app.post("/prompt")
async def prompt(body: PromptRequest, authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """Run the agent for one prompt and return its output plus file changes."""
    _check_auth(authorization)
    if not body.prompt.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="prompt is empty")
    return await run_agent(body.prompt)


@app.post("/v1/chat/completions")
async def chat_completions(
    body: ChatRequest, authorization: Optional[str] = Header(default=None)
) -> Dict[str, Any]:
    """OpenAI-compatible endpoint so the pool can use this shim as a worker."""
    _check_auth(authorization)
    text_prompt = _prompt_from(body)
    if not text_prompt.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no message content")
    result = await run_agent(text_prompt)
    return {
        "id": f"chatcmpl-freebuff-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result["artifact"]},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


if __name__ == "__main__":  # pragma: no cover - manual entry point
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8099)
