"""The packaged API gets its own signing secret on first run.

Production refuses to start without SECRET_KEY, and a desktop user never sets
one, so the sidecar creates a per-install secret in its data directory.
"""

from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_entry() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sidecar_entry", REPO_ROOT / "sidecar" / "sidecar_entry.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """Give the sidecar a private copy of os.environ, so nothing leaks between tests."""
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.chdir(tmp_path)  # no stray .env from the checkout
    return os.environ  # type: ignore[return-value]


def test_first_run_creates_a_private_secret(isolated_env: dict[str, str], tmp_path: Path) -> None:
    entry = _load_entry()
    isolated_env.pop("SECRET_KEY", None)
    data_dir = tmp_path / "data"
    entry.configure_environment(str(data_dir))
    secret = isolated_env["SECRET_KEY"]
    assert len(secret) >= 40
    assert "insecure" not in secret
    key_file = data_dir / "secret.key"
    assert key_file.read_text(encoding="utf-8").strip() == secret
    if os.name == "posix":
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_later_runs_reuse_the_same_secret(isolated_env: dict[str, str], tmp_path: Path) -> None:
    """Rotating it on every start would log every user out and orphan encrypted credentials."""
    entry = _load_entry()
    data_dir = tmp_path / "data"
    isolated_env.pop("SECRET_KEY", None)
    entry.configure_environment(str(data_dir))
    first = isolated_env["SECRET_KEY"]
    isolated_env.pop("SECRET_KEY", None)
    entry.configure_environment(str(data_dir))
    assert isolated_env["SECRET_KEY"] == first


def test_an_operator_secret_always_wins(isolated_env: dict[str, str], tmp_path: Path) -> None:
    entry = _load_entry()
    operator_value = "x" * 48  # a stand-in for an operator's own value
    isolated_env["SECRET_KEY"] = operator_value
    data_dir = tmp_path / "data"
    entry.configure_environment(str(data_dir))
    assert isolated_env["SECRET_KEY"] == operator_value
    assert not (data_dir / "secret.key").exists()


def test_loopback_sidecar_may_run_without_sign_in(isolated_env: dict[str, str], tmp_path: Path) -> None:
    """The desktop app is one user on this machine, so it opts in to an open API."""
    entry = _load_entry()
    isolated_env.pop("ALLOW_OPEN_API", None)
    entry.configure_environment(str(tmp_path / "data"), host="127.0.0.1")
    assert isolated_env["ALLOW_OPEN_API"] == "true"


def test_network_bound_sidecar_does_not_get_the_open_api_opt_in(isolated_env: dict[str, str], tmp_path: Path) -> None:
    """Binding beyond localhost must still require sign-in: the opt-in is loopback only."""
    entry = _load_entry()
    isolated_env.pop("ALLOW_OPEN_API", None)
    entry.configure_environment(str(tmp_path / "data"), host="0.0.0.0")
    assert "ALLOW_OPEN_API" not in isolated_env
