"""Fail when a tracked file holds a secret that is not in ``.secrets.baseline``.

``detect-secrets-hook`` rewrites the baseline whenever a reviewed line moves, and
it fails the run when it does. Line numbers differ between Python builds, so the
hook can fail in CI on a clean tree. This check matches on the file name and the
secret's hash instead, so moving a reviewed line never fails the build. A new
secret, or a reviewed secret moved to another file, still does.

Usage (from the repository root)::

    python scripts/secret_scan.py            # exit 1 on a new, unreviewed secret
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

BASELINE = Path(".secrets.baseline")


def tracked_files() -> List[str]:
    """Return the files Git tracks, NUL-separated so odd names are safe."""
    listing = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout
    return [name for name in listing.decode("utf-8", "replace").split("\0") if name]


def reviewed_hashes(baseline: Path) -> Set[Tuple[str, str]]:
    """Return the ``(file, hash)`` pairs that a reviewer has already seen."""
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    return {
        (name, item["hashed_secret"])
        for name, items in payload.get("results", {}).items()
        for item in items
    }


def main() -> int:
    """Scan tracked files and report any finding the baseline does not cover."""
    from detect_secrets.core.secrets_collection import SecretsCollection
    from detect_secrets.settings import default_settings

    reviewed = reviewed_hashes(BASELINE)
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
