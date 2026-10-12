#!/usr/bin/env python3
"""Entry point for the packaged Kollektiv API (the desktop sidecar).

A PyInstaller build imports this module instead of ``src.api.routes`` directly,
for two reasons:

* it can set defaults *before* settings are constructed — the packaged app must
  listen on localhost, keep its data beside the user's home, and never depend on
  a shell profile;
* it prints the URL it is serving, so the desktop shell's supervisor and a human
  reading a terminal see the same thing.

Everything else is the normal application: the same routes, the same dashboard at
``/ui/``, the same SQLite schema.

Usage (after PyInstaller, or directly from a checkout):

    python -m packaging.sidecar_entry --port 8000 --host 127.0.0.1
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

#: Where the packaged app keeps state when the user configured nothing.
DEFAULT_DATA_DIR = Path.home() / ".kollektiv"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the few flags the sidecar accepts.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(description="Kollektiv API sidecar")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: localhost only)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("API_PORT", "8000")))
    parser.add_argument("--data-dir", default="", help="Where the database and workspace live")
    parser.add_argument("--reload", action="store_true", help="Development only: auto-reload")
    return parser.parse_args(argv)


LOOPBACK_BINDS = frozenset({"127.0.0.1", "localhost", "::1"})


def configure_environment(data_dir: str = "", host: str = "127.0.0.1") -> Path:
    """Set the defaults the packaged app needs, without clobbering real settings.

    Args:
        data_dir: Explicit data directory; defaults to ``~/.kollektiv``.
        host: The address the sidecar will bind. The open-API opt-in is only
            granted for loopback, so a sidecar exposed to the network still
            refuses to start without auth.

    Returns:
        The data directory in use (created when missing).
    """
    root = Path(data_dir).expanduser() if data_dir else DEFAULT_DATA_DIR
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # ``setdefault`` on purpose: a container, a systemd unit or an operator's own
    # environment always wins over the packaged defaults.
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{root / 'kollektiv.db'}")
    os.environ.setdefault("WORKSPACE_DIR", str(workspace))
    os.environ.setdefault("AUTO_INIT_DB", "true")
    os.environ.setdefault("ENVIRONMENT", "production")
    if host in LOOPBACK_BINDS:
        # Desktop install: one user, no sign-in, reachable only from this machine.
        os.environ.setdefault("ALLOW_OPEN_API", "true")

    # The sidecar is launched by the desktop shell (or by the user), so it reads
    # the same .env file every other entry point reads.
    from src.api.cli import load_env_file

    for candidate in (Path.cwd() / ".env", root / ".env"):
        applied = load_env_file(str(candidate))
        if applied:
            print(f"Kollektiv: loaded {len(applied)} setting(s) from {candidate}", flush=True)
            break

    # Production refuses to start without a signing secret. A desktop user never
    # sets one, so each install makes its own on first run and keeps it locally.
    if not os.environ.get("SECRET_KEY"):
        os.environ["SECRET_KEY"] = install_secret(root)
    return root


def install_secret(root: Path) -> str:
    """Return this install's signing secret, creating it on the first run.

    The secret is random per install, so no two installs share one, and it is
    stored in the data directory with owner-only permissions.

    Args:
        root: The data directory (created by the caller).

    Returns:
        The secret, as stored in ``root / "secret.key"``.
    """
    import secrets

    path = root / "secret.key"
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    value = secrets.token_urlsafe(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    return value


def main(argv: list[str] | None = None) -> int:
    """Start the API in-process.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on a clean shutdown; uvicorn's error surface otherwise.
    """
    args = parse_args(argv)
    root = configure_environment(args.data_dir, host=args.host)
    try:
        import uvicorn
    except Exception as exc:  # noqa: BLE001 - a packaged app must say what is wrong
        print(f"Kollektiv: uvicorn is unavailable in this build ({exc})", file=sys.stderr)
        return 1

    print(f"Kollektiv API on http://{args.host}:{args.port}  (dashboard at /ui/)", flush=True)
    print(f"Data   : {root}", flush=True)
    if args.host not in {"127.0.0.1", "localhost"}:
        print("Warning: binding beyond localhost exposes the API to your network.", flush=True)
    uvicorn.run(
        "src.api.routes:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
