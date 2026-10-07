# Development

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

## Development

```bash
pip install -e ".[dev]"

pytest -q                 # 354 tests, ~11 s, fully mocked
pytest tests/test_api.py -q
ruff check .              # lint (clean)
mypy src config examples scripts tests  # types (clean)
pytest --cov=src          # optional coverage (pip install pytest-cov)
```

`tests/test_r2.py` pins the SigV4 vectors and, when `botocore` is installed
(it is part of the `dev` extra), cross-checks the hand-rolled signer against
`botocore.auth.S3SigV4Auth` — so the free-tier storage client provably matches
the reference implementation.

### Design decisions

- **Typed errors, two retry classes.** Transport failures and 5xx responses are
  retried with exponential backoff; 4xx/API-level errors fail fast, and *rate
  limits are never retried inside a client* — the pool and the brain own
  failover so no request sleeps through a cooldown.
- **Graceful degradation everywhere.** Missing brain key → heuristics; missing
  TeraBox → local fallback file; missing GitHub → sync reports it and moves on.
  `/health` and `kollektiv check` always describe what is degraded.
- **Encrypted credentials.** Tokens are Fernet-encrypted before they touch
  SQLite and are never logged (emails and tokens are masked in every message).
- **Provider-agnostic workers.** The pool speaks plain HTTP with two request
  shapes, so it works with hosted APIs, local models or your own bridge — no
  vendor lock-in and no automation of services that forbid it.
- **Project-local task ids.** Plan ids (`t1`, `t2`, …) are unique per project;
  the `tasks` table is keyed by `(id, project_id)` so many projects coexist.
- **Free tier first, paid never required.** Cloudflare R2, Neon, Clerk, Resend
  and Pages are all optional: `STORAGE_BACKEND=auto`, SQLite, open routes and
  log-only notifications are the defaults, so a fresh clone runs end to end
  with zero accounts and upgrades in place when keys appear.
- **One engine per orchestrator.** Passing a `Settings` object to an
  `Orchestrator` rebinds the database engine to that configuration, which is
  what lets tests run dozens of isolated in-memory orchestrators.

### Testing notes

- Every test runs against mocked HTTP transports and in-memory SQLite, so no
  credentials or network access are required.
- Retry backoff collapses to milliseconds in tests via
  `KOLLEKTIV_RETRY_BASE_DELAY` / `KOLLEKTIV_RETRY_MAX_DELAY`; the same variables
  tune (or effectively disable) waiting in production.
- `tests/test_state.py` covers the markdown round trip and the storage-outage
  fallback; `tests/test_api.py` covers the routes through
  `httpx.ASGITransport` and calls the real MCP tools through the SDK.

---
