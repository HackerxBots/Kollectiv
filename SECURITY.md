# Security Policy

## Supported versions

Kollektiv is pre-1.0 and ships beta releases. Security fixes go into the newest
release line only.

| Version | Supported |
| --- | --- |
| newest `0.x` beta (`v0.x.y-beta.*`) | ✅ |
| anything older | ❌ — upgrade first |

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use GitHub's private vulnerability reporting:

1. Go to the repository's **Security** tab → **Advisories** → **Report a vulnerability**.
2. Or open <https://github.com/HackerxBots/Kollektiv/security/advisories/new> directly.

That channel is encrypted, visible only to the maintainers, and gives us a
private fork to develop and test a fix before anything is public.

Helpful details, if you have them:

- affected version/commit and how you installed it (`pip`, clone, container);
- a minimal reproduction (config shape + request, with secrets replaced by
  placeholders);
- the impact you believe it has;
- any suggested fix.

**What to expect:** an acknowledgement within 72 hours, an assessment within a
week, and credit in the advisory and changelog once a fix is released — unless
you ask to stay anonymous. We will not pursue legal action against researchers
who report in good faith, stay within their own systems, and do not exfiltrate
or destroy data.

## Scope

In scope:

- the API (`src/api/`), the connector layer (`src/connectors/`) and the CLI;
- credential handling (`TokenStore`, `SECRET_KEY`, connector tokens);
- the auth middleware (`src/api/auth.py`: Clerk verification, Svix webhook
  signatures, public-path rules);
- storage and sync paths that handle untrusted input (file names, paths,
  agent output, webhooks);
- the dashboard (`web/`) and its static deployment.

Out of scope (report upstream instead):

- vulnerabilities in dependencies — report to the upstream project, though
  we appreciate a heads-up;
- your own deployment's configuration (weak `SECRET_KEY`, `AUTH_REQUIRED=false`
  on a public host, exposed `.env`);
- social engineering, physical access, or denial of service by sheer volume.

## Threat model (what Kollektiv assumes)

Kollektiv coordinates AI agents that produce code. It is a *tool*, not a
sandbox, and it assumes:

- **The operator owns the deployment.** Anyone with API access can run projects
  and call connectors. Set `AUTH_REQUIRED=true` and Clerk keys before exposing
  it. `/health`, `/docs` and `/webhooks/*` stay public by design
  (`/webhooks/*` verifies its own signatures).
- **Agent output is untrusted text.** Collected files are written to the
  workspace, never executed by Kollektiv. Running generated code is the one
  feature that needs the sandbox (tracked in issue #15) — until then, review
  before you run.
- **Tokens are secrets.** They live in the encrypted store or `.env`, are
  masked in logs and `/health`, and `SECRET_KEY` is the key to all of them.
  Losing or leaking it means rotating every stored credential.
- **Dangerous actions are gated, not forbidden.** Connector actions that send,
  create or comment require `confirm=True`; the allowlist work (issue #6) adds a
  second lock for agent-initiated calls.
- **No telemetry.** Nothing leaves your deployment except the endpoints you
  configure. There is no phone-home for the maintainers to compromise.

## Static analysis (CodeQL) and the baseline

`.github/workflows/codeql.yml` runs CodeQL with `security-and-quality` on every
pull request and `security-extended` weekly on `main`, and uploads the results to
the repository's *Code scanning* tab. Alerts are triaged before a pull request is
merged: fix it, or dismiss it with a reason. The first run on this branch reported 43 findings: 1 critical, 18 high,
2 medium, 1 warning and 21 notes — mostly one query over the storage layer plus
notices about import cycles and unused names. The baseline, so a new contributor
knows what has already been decided:

**Addressed in code** (CodeQL's taint analysis does not recognise a custom
validator, so these alerts stay open until they are dismissed with a reason —
use *"Mitigated: validated by `src/utils/paths.py`"*)

- *Path injection* (`py/path-injection`) — every project id, account id and file
  path that becomes part of a filesystem or bucket path goes through
  `src/utils/paths.py`. `safe_path_segment()` rejects separators, `..`/`.`,
  control characters, leading dots and over-long values; `safe_relative_path()`
  additionally rejects absolute paths and empty segments. Callers: the state
  manager's local and remote paths, artifact uploads, and the project id *and*
  file path of `GET /projects/{id}/files/{path}/url`. The API answers the
  resulting `ValueError` with `400` (see the handler in `src/api/routes.py`).
- *Stack-trace exposure* (`py/stack-trace-exposure`) on `GET /health`, the
  project event stream and the presigned-URL route — failures are logged with
  `exc_info=True` and the client receives a generic message plus the exception
  *class* name, never the internal message.
- *Open storage-client findings remain:* the alerts against
  `src/storage/r2_pool.py`, `src/storage/pool_manager.py` and
  `examples/aider_shim.py` are the same query and the same reasoning (the storage
  API takes the remote key it is given, and the shim runs the command the
  operator configured).

**Accepted by design** (dismissed with a reason in the Security tab)

- `py/clear-text-logging-sensitive-data` in `src/api/cli.py` — `kollektiv
  bootstrap` and `kollektiv secret` print a *newly generated* `SECRET_KEY`, and
  `kollektiv login` prints a masked preview of the **account id**, to the
  operator's own terminal. Showing the value once is the entire purpose of those
  commands; the real token is stored encrypted through `TokenStore` and never
  printed.
- `py/command-line-injection` in `examples/aider_shim.py` — the shim runs the
  command the *operator* set in `AIDER_COMMAND`, by design, exactly like a shell.
- `py/path-injection` in the storage clients (`src/storage/r2_pool.py`,
  `src/storage/pool_manager.py`) — those are the storage layer's public API:
  callers pass the remote key they want, like a filesystem API. Every call site
  inside Kollektiv composes that key from validated segments.
- *Overwritten inherited attribute* in `src/utils/errors.py` —
  `RateLimitError.retry_after` deliberately narrows the optional attribute
  inherited from `RetryableError` to a float.
- Import-cycle and unused-name notices (`src/connectors/*`, `src/db/models.py`,
  `src/utils/logger.py`) are style notes, not security findings.

## Hardening checklist for operators

- [ ] `SECRET_KEY` set (long, random) and backed up with the database.
- [ ] `AUTH_REQUIRED=true` with Clerk keys when the API is reachable publicly.
- [ ] TLS in front (required for GitHub webhooks); restrict `CORS_ORIGINS`.
- [ ] `GITHUB_WEBHOOK_SECRET` and `CLERK_WEBHOOK_SECRET` set.
- [ ] Database and shared drive credentials scoped to the minimum (R2 tokens
      with object read/write on one bucket, not account-wide).
- [ ] Backups tested (see issue #18 for the built-in job).
- [ ] `EVENT_WEBHOOKS` URLs treated as secrets (they usually carry tokens).
- [ ] Leave the path validation in `src/utils/paths.py` in place: it is what
      keeps `/projects/{id}/...` and artifact keys inside your workspace and
      bucket when the API is exposed.
