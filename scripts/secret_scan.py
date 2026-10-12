"""Fail when a tracked file holds a secret that is not in ``.secrets.baseline``.

``detect-secrets-hook`` rewrites the baseline whenever a reviewed line moves, and
it fails the run when it does. Line numbers differ between Python builds, so the
hook can fail in CI on a clean tree. This check matches on the file name and the
secret's hash instead, so moving a reviewed line never fails the build. A new
secret, or a reviewed secret moved to another file, still does.

Usage (from the repository root)::

    python scripts/secret_scan.py            # current tree; exit 1 on a new, unreviewed secret
    python scripts/secret_scan.py --history  # every blob in git history; exit 1 on any unreviewed hit

``--history`` is for a manual audit. CI does not run it, because history holds
old fixtures whose hashes the current baseline does not list. Lockfiles
(integrity hashes) and the baseline itself are skipped in history mode.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 - fixed local tool calls
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

BASELINE = Path(".secrets.baseline")


def tracked_files() -> List[str]:
    """Return the files Git tracks, NUL-separated so odd names are safe."""
    listing = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout  # nosec B603 B607 - fixed git arguments, no shell
    return [name for name in listing.decode("utf-8", "replace").split("\0") if name]


def reviewed_hashes(baseline: Path) -> Set[Tuple[str, str]]:
    """Return the ``(file, hash)`` pairs that a reviewer has already seen."""
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    return {
        (name, item["hashed_secret"])
        for name, items in payload.get("results", {}).items()
        for item in items
    }


HISTORY_SKIP_NAMES = {"package-lock.json", ".secrets.baseline"}


def history_blobs() -> Dict[str, str]:
    """Map every blob reachable from any ref to a path it was stored under."""
    git = ["git"]
    commits = subprocess.run([*git, "rev-list", "--all"], capture_output=True, check=True, text=True).stdout.split()  # nosec B603 - fixed arguments, no shell
    blobs: Dict[str, str] = {}
    for commit in commits:
        listing = subprocess.run(  # nosec B603 - fixed arguments, no shell
            [*git, "ls-tree", "-r", "-z", "--full-tree", commit], capture_output=True, check=True
        ).stdout.decode("utf-8", "replace")
        for entry in filter(None, listing.split("\0")):
            meta, name = entry.split("\t", 1)
            _mode, kind, sha = meta.split()
            if kind == "blob" and Path(name).name not in HISTORY_SKIP_NAMES:
                blobs.setdefault(sha, name)
    return blobs


def scan_history(reviewed: Set[Tuple[str, str]]) -> List[str]:
    """Scan each historical blob; return the unreviewed findings as text lines."""
    import tempfile

    from detect_secrets.core.secrets_collection import SecretsCollection
    from detect_secrets.settings import default_settings

    unreviewed: List[str] = []
    with tempfile.TemporaryDirectory() as scratch, default_settings():
        for sha, name in history_blobs().items():
            content = subprocess.run(["git", "cat-file", "-p", sha], capture_output=True, check=True).stdout  # nosec B603 B607 - fixed git arguments, no shell
            if b"\0" in content[:8000]:
                continue  # binary
            path = Path(scratch) / sha
            path.write_bytes(content)
            collection = SecretsCollection()
            collection.scan_file(str(path))
            for secrets in collection.data.values():
                for secret in secrets:
                    if (name, secret.secret_hash) not in reviewed:  # type: ignore[attr-defined]
                        unreviewed.append(f"{name} (blob {sha[:7]}): {secret.type}")  # type: ignore[attr-defined]
    return unreviewed


def main(argv: Optional[List[str]] = None) -> int:
    """Scan tracked files (or, with --history, every blob in history) for unreviewed secrets."""
    from detect_secrets.core.secrets_collection import SecretsCollection
    from detect_secrets.settings import default_settings

    args = sys.argv[1:] if argv is None else argv
    reviewed = reviewed_hashes(BASELINE)
    if "--history" in args:
        history_hits = scan_history(reviewed)
        if history_hits:
            print("Unreviewed secrets in git history (check each one; a live credential must be rotated):")
            for line in sorted(set(history_hits)):
                print(f"  - {line}")
            return 1
        print("git history: no unreviewed secrets")
        return 0

    collection = SecretsCollection()
    with default_settings():
        for name in tracked_files():
            if name == str(BASELINE) or not Path(name).is_file():
                continue
            collection.scan_file(name)

    unreviewed: List[str] = []
    findings: Dict[str, List[object]] = collection.data  # type: ignore[assignment]
    for name, secrets in findings.items():
        for secret in secrets:
            if (name, secret.secret_hash) not in reviewed:  # type: ignore[attr-defined]
                unreviewed.append(f"{name}: {secret.type}")  # type: ignore[attr-defined]

    if unreviewed:
        print("Unreviewed secrets (review them, then add them to .secrets.baseline):")
        for line in sorted(unreviewed):
            print(f"  - {line}")
        return 1
    print(f"secret scan OK: {len(reviewed)} reviewed finding(s), no new secrets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
