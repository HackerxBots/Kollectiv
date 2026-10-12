"""Guards for the Docker files, so a first `docker compose up` keeps working.

These are text checks because the sandbox cannot run Docker. They cover the
three first-run failures found in review: a missing .env stopping compose, the
MCP service having no build of its own, and a missing LICENSE in the image.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _service_block(compose: str, name: str) -> str:
    """Text of one service: from its key to the next key at the same indent."""
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)", compose, re.M | re.S)
    assert match, f"service {name} not found"
    return match.group(1)


def test_env_file_is_optional_so_a_missing_dotenv_does_not_stop_compose() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- .env\n" not in compose  # the bare form fails when .env is absent
    assert compose.count("required: false") >= 2  # shared anchor + MCP service


def test_mcp_service_can_build_on_its_own() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "build:" in _service_block(compose, "kollektiv-mcp")


def test_dockerfile_copies_license_required_by_pyproject() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license-files = ["LICENSE"]' in pyproject
    copy_lines = [line for line in dockerfile.splitlines() if line.startswith("COPY ")]
    assert any("LICENSE" in line.split() for line in copy_lines)


def test_compose_defaults_to_development_for_local_use() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "ENVIRONMENT: ${ENVIRONMENT:-development}" in compose
    assert "ENVIRONMENT: ${ENVIRONMENT:-production}" not in compose


def _dockerignore_excludes(path: str, patterns: list[str]) -> bool:
    """Approximate Docker's rule: the last matching pattern wins, and a match on a directory covers its files.

    Matching uses fnmatch, which is close enough for the simple patterns this repo uses.
    """
    parts = path.split("/")
    excluded = False
    for raw in patterns:
        negated = raw.startswith("!")
        pattern = (raw[1:] if negated else raw).strip("/")
        if any(fnmatch.fnmatch("/".join(parts[: i + 1]), pattern) for i in range(len(parts))):
            excluded = not negated
    return excluded


def test_every_file_the_dockerfile_copies_survives_dockerignore() -> None:
    """A COPY source that .dockerignore removes fails `docker build` with "not found"."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    ignore = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    sources: list[str] = []
    for line in dockerfile.splitlines():
        if line.startswith("COPY "):
            args = [arg for arg in line.split()[1:] if not arg.startswith("--")]
            sources.extend(args[:-1])  # the last argument is the destination
    assert sources, "no COPY sources found"
    for source in sources:
        assert (ROOT / source).exists(), f"COPY source missing from the repo: {source}"
        assert not _dockerignore_excludes(source, ignore), f".dockerignore removes COPY source {source}"
