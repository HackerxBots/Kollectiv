"""``python -m src.gateway`` — run the MCP gateway.

Equivalent to ``kollektiv gateway serve`` / ``kollektiv-serve-gateway``.
"""

from __future__ import annotations

from src.gateway.app import main

if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
