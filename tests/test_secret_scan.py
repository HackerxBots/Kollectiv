"""The secret scan matches on file and hash, and history mode finds old leaks.

Both checks run against a throwaway git repository, never the real one. The
planted values are fake AWS-shaped strings, not real credentials.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_scanner() -> Any:
    spec = importlib.util.spec_from_file_location("secret_scan", REPO_ROOT / "scripts" / "secret_scan.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    scanner = _load_scanner()
    _git(tmp_path, "init", "-q")
    # An empty baseline: every finding is unreviewed.
    (tmp_path / ".secrets.baseline").write_text(json.dumps({"results": {}}), encoding="utf-8")
    (tmp_path / "config.py").write_text('AWS = "AKIAZ7Q3X2M5N8P1R4T6"\n', encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "leak")
    (tmp_path / "config.py").write_text('AWS = os.environ["AWS"]\n', encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "remove leak")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scanner, "BASELINE", Path(".secrets.baseline"))
    return tmp_path


def test_current_tree_is_clean_after_the_leak_is_removed(repo: Path) -> None:
    scanner = _load_scanner()
    scanner.BASELINE = Path(".secrets.baseline")
    assert scanner.main([]) == 0


def test_history_mode_finds_a_leak_removed_from_the_tree(repo: Path) -> None:
    scanner = _load_scanner()
    scanner.BASELINE = Path(".secrets.baseline")
    assert scanner.main(["--history"]) == 1


def test_history_mode_passes_once_the_leak_is_reviewed(repo: Path) -> None:
    from detect_secrets.core.secrets_collection import SecretsCollection
    from detect_secrets.settings import default_settings

    scanner = _load_scanner()
    scanner.BASELINE = Path(".secrets.baseline")
    assert scanner.scan_history(set()), "the planted key should be reported before review"

    # Reviewing a finding means recording its (file, hash) pair. Build those pairs
    # from the old text of the file, exactly as the baseline would store them.
    old_text = subprocess.run(
        ["git", "show", "HEAD~1:config.py"], cwd=repo, capture_output=True, check=True, text=True
    ).stdout
    probe = repo / "probe_scratch.py"
    probe.write_text(old_text, encoding="utf-8")
    collection = SecretsCollection()
    with default_settings():
        collection.scan_file(str(probe))
    probe.unlink()
    reviewed = {("config.py", s.secret_hash) for secrets in collection.data.values() for s in secrets}

    assert reviewed, "the probe should find the planted key"
    assert scanner.scan_history(reviewed) == []
