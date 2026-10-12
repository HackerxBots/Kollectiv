"""The CLI worker shim passes the prompt as one argument, never through a shell."""

from __future__ import annotations

import asyncio
import sys

import pytest

pytest.importorskip("fastapi")

from examples import aider_shim  # noqa: E402


def test_prompt_arrives_as_one_intact_argument() -> None:
    """Spaces, quotes and shell metacharacters stay inside the one argument."""
    prompt = "fix the bug; echo PWNED && rm -rf / \"quoted\" $HOME"
    command = f"{sys.executable} -c 'import sys; print(sys.argv[1])' {{prompt}}"
    result = asyncio.run(aider_shim._run(command, prompt))
    assert result["exit_code"] == 0
    assert result["text"].strip() == prompt


def test_empty_command_is_refused() -> None:
    """An empty template is a configuration error, not a crash."""
    with pytest.raises(aider_shim.HTTPException):
        asyncio.run(aider_shim._run("   ", "hi"))


def test_shell_mode_is_gone() -> None:
    assert not hasattr(aider_shim, "USE_SHELL")
