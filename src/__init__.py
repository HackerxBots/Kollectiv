"""Kollektiv: a multi-agent collaborative development team orchestrator.

Kollektiv coordinates a pool of worker agents, shared object storage,
a GitHub-backed synchronisation layer and a small LLM "brain" that plans
and reviews work.

The package is intentionally import-light: subpackages are imported
explicitly (``src.storage``, ``src.agents``, ...) so that using, for
example, only the storage layer does not pull in FastAPI or the MCP SDK.
"""

__version__ = "0.4.0"
__all__ = ["__version__"]
