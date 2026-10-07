"""Tests for the Freebuff worker shim (``examples/freebuff_shim.py``).

The shim wraps an ad-supported CLI coding agent, so the tests pin exactly the
two things that make it safe to use as a worker: ads and ANSI noise never reach
an artifact, and the agent's file changes come back in Kollektiv's fenced-block
contract. Git and the "agent" are real processes, but they run against a
throwaway repository in ``tmp_path`` — no network, no credentials.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest

import examples.freebuff_shim as shim


def _reload(monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
    """Reload the shim with ``env`` applied (it reads configuration at import)."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(shim)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A tiny git repository with one committed file."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "app.py").write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    return tmp_path


# ----------------------------------------------------------------------
# Text hygiene
# ----------------------------------------------------------------------
def test_strip_ansi_removes_colour_and_cursor_codes() -> None:
    """Terminal escapes never survive into an artifact."""
    raw = "\x1b[1;32m✓ done\x1b[0m\r\n\x1b]0;title\x07next"
    assert shim.strip_ansi(raw) == "✓ done\nnext"


def test_strip_ads_drops_banners_but_keeps_code() -> None:
    """Ad lines go, code and fenced blocks stay."""
    text = "\n".join(
        [
            "│ AD: try Freebuff Pro today",
            "Sponsored by SomeCompany",
            "You have 100 freebucks left",
            "────────────────────",
            "Running tests…",
            "```python",
            'print("sponsored by nobody")  # inside a fence, kept verbatim',
            "```",
        ]
    )
    cleaned = shim.strip_ads(text)
    assert "AD: try" not in cleaned
    assert "Sponsored by SomeCompany" not in cleaned
    assert "freebucks" not in cleaned
    assert "────────" not in cleaned
    assert "Running tests…" in cleaned
    assert 'print("sponsored by nobody")' in cleaned


def test_strip_ads_keeps_long_lines_and_custom_patterns() -> None:
    """Long lines are never dropped, and operators can add their own patterns."""
    long_line = "sponsored by " + "x" * 300
    assert long_line in shim.strip_ads(long_line)
    text = "noise: ACME promo code\nreal output"
    assert "ACME promo" not in shim.strip_ads(text, extra=["ACME promo code"])


def test_strip_ads_ignores_a_broken_operator_pattern() -> None:
    """An invalid regex from the environment cannot break a run."""
    assert shim.strip_ads("keep me", extra=["([unclosed"]) == "keep me"


def test_language_for_maps_extensions() -> None:
    """Fence languages come from the file extension."""
    assert shim.language_for("src/app.py") == "python"
    assert shim.language_for("web/assets/app.js") == "javascript"
    assert shim.language_for("LICENSE") == "text"


def test_build_artifact_uses_the_collector_contract() -> None:
    """Every changed file becomes one fenced block tagged with its path."""
    artifact = shim.build_artifact([("src/app.py", "print('ok')\n")], notes="- skipped: logo.png")
    assert artifact.startswith("- skipped: logo.png")
    assert "```python path=src/app.py" in artifact
    assert artifact.rstrip().endswith("```")


# ----------------------------------------------------------------------
# The working tree
# ----------------------------------------------------------------------
async def test_collect_changes_reads_modified_and_untracked_files(repo: Path) -> None:
    """Edits and new files are returned; deletions are reported, not inlined."""
    (repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "util.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / "old.py").write_text("gone\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "old.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add old"], check=True)
    (repo / "old.py").unlink()

    files, notes = await shim.collect_changes(str(repo))
    collected: Dict[str, str] = dict(files)

    assert collected["app.py"] == "print('v2')\n"
    assert collected["src/util.py"] == "VALUE = 2\n"
    assert "old.py" not in collected
    assert "old.py (deleted)" in notes


async def test_collect_changes_skips_binaries_and_oversized_files(repo: Path) -> None:
    """Binary and huge files are listed instead of being pasted into an artifact."""
    (repo / "logo.bin").write_bytes(b"\x00\x01\x02\xff")
    (repo / "big.py").write_text("x = 1  # padded\n" * (shim.MAX_INLINE_BYTES // 10), encoding="utf-8")
    (repo / "good.py").write_text("OK = True\n", encoding="utf-8")

    files, notes = await shim.collect_changes(str(repo))
    collected = dict(files)

    assert list(collected) == ["good.py"]
    assert "logo.bin" in notes and "big.py" in notes


async def test_collect_changes_rejects_paths_outside_the_workspace(repo: Path) -> None:
    """A crafted path cannot pull a file from outside the repository."""
    (repo.parent / "outside.txt").write_text("secret\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        # git itself refuses to report a path outside the work tree, which is
        # the first line of defence; the shim's validator is the second.
        await shim.collect_changes(str(repo.parent))
    with pytest.raises(ValueError):
        shim.safe_relative_path("../outside.txt", label="changed file path")


# ----------------------------------------------------------------------
# Running the agent
# ----------------------------------------------------------------------
def test_build_argv_supports_both_prompt_delivery_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prompt goes into argv, or into stdin, but never both."""
    module = _reload(monkeypatch, WORKER_PROMPT_VIA="stdin", FREEBUFF_COMMAND="freebuff")
    argv, pipe = module.build_argv(module.COMMAND, "write a test")
    assert argv == ["freebuff"] and pipe is True

    module = _reload(monkeypatch, WORKER_PROMPT_VIA="argv", FREEBUFF_COMMAND="freebuff --print {prompt}")
    argv, pipe = module.build_argv(module.COMMAND, "write a test")
    assert argv == ["freebuff", "--print", "write a test"] and pipe is False

    # The prompt stays one argv element, however it is written.
    tricky = 'add "quotes", --not-a-flag and; a newline\nsecond line'
    argv, pipe = module.build_argv("freebuff --print {prompt}", tricky)
    assert argv == ["freebuff", "--print", tricky] and pipe is False

    # No placeholder: the prompt is appended as the final argument.
    argv, pipe = module.build_argv("freebuff run", "do the thing")
    assert argv == ["freebuff", "run", "do the thing"] and pipe is False


async def test_run_agent_returns_the_diff_as_an_artifact(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """The agent edits files; the worker output is the change, not the chatter."""
    script = (
        "import pathlib, sys; "
        "print('AD: buy something'); "
        "pathlib.Path('made.py').write_text('VALUE = 42\\n'); "
        "print('\\x1b[32mdone\\x1b[0m'); "
        "sys.stdin.read()"
    )
    module = _reload(
        monkeypatch,
        FREEBUFF_COMMAND=f"{sys.executable} -c \"{script}\"",
        WORKER_WORKDIR=str(repo),
        WORKER_COLLECT_CHANGES="1",
        WORKER_TIMEOUT="60",
    )
    result = await module.run_agent("create made.py")

    assert result["files"] == ["made.py"]
    assert "```python path=made.py" in result["artifact"]
    assert "VALUE = 42" in result["artifact"]
    # The ad and the escape codes are stripped from the raw text, and the
    # artifact contains no terminal chatter at all.
    assert "AD: buy" not in result["text"]
    assert "\x1b" not in result["text"]
    assert "buy something" not in result["artifact"]


async def test_run_agent_can_return_raw_text(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """With ``WORKER_COLLECT_CHANGES=0`` the terminal text is the answer."""
    module = _reload(
        monkeypatch,
        FREEBUFF_COMMAND=f'{sys.executable} -c "import sys; sys.stdin.read(); print(\'plain answer\')"',
        WORKER_WORKDIR=str(repo),
        WORKER_COLLECT_CHANGES="0",
    )
    result = await module.run_agent("say something")
    assert result["artifact"] == "plain answer"
    assert result["files"] == []


def test_relative_executable_resolves_against_the_shim_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative program path means "the one next to me", not "in the workdir"."""
    binary = tmp_path / "agent.py"
    binary.write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert shim.resolve_executable(["./agent.py", "--flag"]) == [str(binary), "--flag"]
    assert shim.resolve_executable(["freebuff", "--print"]) == ["freebuff", "--print"]
    assert shim.resolve_executable(["/usr/bin/env", "python3"]) == ["/usr/bin/env", "python3"]
    assert shim.resolve_executable(["./missing.py"]) == ["./missing.py"]


async def test_run_agent_reports_a_missing_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong FREEBUFF_COMMAND is a clear 500, not a stack trace."""
    module = _reload(monkeypatch, FREEBUFF_COMMAND="definitely-not-installed-agent")
    with pytest.raises(module.HTTPException) as excinfo:
        await module.run_agent("hi")
    assert excinfo.value.status_code == 500
    assert "not executable" in excinfo.value.detail
    assert "PATH" in excinfo.value.detail, "the message says how to fix it"


async def test_run_agent_times_out(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """A hung agent is killed and reported as a timeout."""
    module = _reload(
        monkeypatch,
        FREEBUFF_COMMAND=f'{sys.executable} -c "import time; time.sleep(30)"',
        WORKER_WORKDIR=str(repo),
        WORKER_TIMEOUT="1",
    )
    with pytest.raises(module.HTTPException) as excinfo:
        await module.run_agent("hang")
    assert excinfo.value.status_code == 504


# ----------------------------------------------------------------------
# HTTP surface
# ----------------------------------------------------------------------
async def test_health_reports_configuration(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """``/health`` describes the shim without leaking the token."""
    module = _reload(
        monkeypatch,
        FREEBUFF_COMMAND="freebuff",
        WORKER_WORKDIR=str(repo),
        WORKER_TOKEN="local-dev-token",
    )
    transport = httpx.ASGITransport(app=module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    body = response.json()
    assert body["status"] == "ok"
    assert body["auth"] is True
    assert "local-dev-token" not in response.text
    assert body["note"].startswith("one supervised session")


async def test_chat_completions_requires_the_token(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """The OpenAI-compatible endpoint is guarded and returns the contract."""
    script = "import pathlib, sys; pathlib.Path('x.py').write_text('X = 1\\n'); sys.stdin.read()"
    module = _reload(
        monkeypatch,
        FREEBUFF_COMMAND=f"{sys.executable} -c \"{script}\"",
        WORKER_WORKDIR=str(repo),
        WORKER_TOKEN="local-dev-token",
        WORKER_COLLECT_CHANGES="1",
    )
    transport = httpx.ASGITransport(app=module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        anonymous = await client.post("/v1/chat/completions", json={"messages": [{"content": "hi"}]})
        assert anonymous.status_code == 401

        response = await client.post(
            "/v1/chat/completions",
            json={"model": "freebuff", "messages": [{"role": "user", "content": "write x.py"}]},
            headers={"Authorization": "Bearer local-dev-token"},
        )
        empty = await client.post("/prompt", json={"prompt": "  "}, headers={"Authorization": "Bearer local-dev-token"})

    assert response.status_code == 200
    body: Dict[str, Any] = response.json()
    content: str = body["choices"][0]["message"]["content"]
    assert "```python path=x.py" in content
    assert body["model"] == "freebuff"
    assert empty.status_code == 400


async def test_prompt_endpoint_returns_files_and_timing(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """``/prompt`` reports which files changed, for logs and dashboards."""
    script = "import pathlib, sys; pathlib.Path('y.py').write_text('Y = 2\\n'); sys.stdin.read()"
    module = _reload(
        monkeypatch,
        FREEBUFF_COMMAND=f"{sys.executable} -c \"{script}\"",
        WORKER_WORKDIR=str(repo),
        WORKER_COLLECT_CHANGES="1",
    )
    transport = httpx.ASGITransport(app=module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/prompt", json={"prompt": "write y.py"})
    body = response.json()
    assert body["files"] == ["y.py"]
    assert isinstance(body["seconds"], float)
    assert body["exit_code"] == 0


def test_documented_defaults_match_the_code() -> None:
    """The module docstring and the defaults cannot drift apart."""
    docstring = (Path(__file__).resolve().parents[1] / "examples" / "freebuff_shim.py").read_text(encoding="utf-8")
    for variable in (
        "FREEBUFF_COMMAND",
        "WORKER_PROMPT_VIA",
        "WORKER_WORKDIR",
        "WORKER_TIMEOUT",
        "WORKER_COLLECT_CHANGES",
        "WORKER_DROP_PATTERNS",
        "WORKER_TOKEN",
    ):
        assert variable in docstring, variable
    assert "TODO(rule 9)" in docstring, "the unverified invocation flags must stay flagged"


def test_shim_reuses_the_shared_helpers() -> None:
    """Guard against duplication: the shim imports the project's own helpers."""
    source = Path(shim.__file__).read_text(encoding="utf-8")
    assert "from src.utils.paths import safe_relative_path" in source
    assert "from src.utils.retry import async_retry" in source
