# Security audit

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

Checked against the 20-point list in the project's security guidelines. Each row
states what was verified and where. **Status:** `pass`, `partial`, `fail`, or `fixed`
(changed in this audit).

This is a self-review, not an independent penetration test. Before a public
launch, have someone outside the project test the hosted deployment.

## Found and fixed in this audit

| Severity | Finding | Fix |
| --- | --- | --- |
| Critical | `POST /projects/{id}/upload` read any path the server can read (for example `.env`) and archived it to shared storage. The MCP server and gateway tools reached the same code. | Uploads are limited to files inside `WORKSPACE_DIR`. The API refuses others with 403. The check runs in the orchestrator, so every entry point is covered. Tested. |
| Critical | Any signed-in user could list and operate on every project, because projects had no owner. | Projects record their owner (Clerk user id). Lists and every project route return 404 to anyone else. Tested with two users. |
| High | `CORS_ORIGINS=*` was combined with credentials, so any website could make signed-in requests and read the replies. | Credentials are allowed only when specific origins are listed. A settings warning appears when `*` is used with `AUTH_REQUIRED=true`. Tested. |
| High | The API and MCP ports were published on all network interfaces. The MCP server has no auth. | Docker Compose binds both to `127.0.0.1`. A public deployment must put a TLS proxy in front. |
| Medium | No security headers on API responses. | `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options` and `Permissions-Policy` are set on every response. Tested. |

## Second pass (external review of the branch)

An external review of the branch found the items below. Each one is fixed, and each fix has a test.

| Severity | Finding | Fix |
| --- | --- | --- |
| High | Gateway: a malformed or non-object stored policy became `allow=["*"]`, so damaged data widened access to every tool, including dangerous ones. | A damaged policy allows nothing and logs an error. Patterns must be a list of strings, so a bare string can no longer be split into characters. |
| High | Gateway: a configured policy file that was missing or unreadable was ignored, so the operator's restrictions were silently dropped. | Every client is denied until the file is fixed. |
| High | MCP server: `--host 0.0.0.0` served project, connector and upload tools with no authentication. | A non-loopback network bind is refused unless `MCP_ALLOW_UNAUTHENTICATED_REMOTE=true`. The default host is `127.0.0.1`. |
| Medium | Gateway: a `client` role, or any unknown role, got the `dashboard` preset, which allows every tool. | Those roles get the `read-only` preset. |
| Medium | Live-stream tickets could be reused until they expired, and the access log recorded them. | A ticket works once, and access-log lines have the ticket redacted. |
| Critical (CodeQL) | `examples/aider_shim.py` built a shell command from the prompt. The earlier dismissal as "by design" is withdrawn. | The shim has no shell mode. The prompt is passed as one argv element. |

Still open from the CodeQL run: a cyclic import (`config/settings.py`), an unused global (`arena_client.py`), an empty `except` (`arena_client.py`) and a statement with no effect (`session_manager.py`). These are low severity and tracked, not fixed here.

## The 20 points

| # | Point | Status | Evidence and what remains |
| --- | --- | --- | --- |
| 1 | Hide API keys | pass | Keys come from the environment or the encrypted store. The API and CLI never print them. `.env` is gitignored. Remaining: whoever runs the host can read its environment. |
| 2 | Check env variables | partial | `kollektiv check` and the settings warnings cover missing and inconsistent values. There is no single fail-closed production profile yet. |
| 3 | Check for keys in git | pass | The full history was scanned once by hand. The history scan found only documented example keys and fixtures (for example AWS's `AKIAIOSFODNN7EXAMPLE` in `tests/test_r2.py`), not live credentials. CI now runs `scripts/secret_scan.py` (detect-secrets) on every tracked file, against a reviewed baseline (`.secrets.baseline`, job *Secret scan*). It scans the current tree, not history, so a secret that was ever committed must be rotated. Remaining: history is not scanned in CI. |
| 4 | Protect admin routes | fixed | Admin routes (`/sync`, `POST`/`DELETE /links`, connector call and probe, `/agents/status`, `/agents/health`, `/budget`, `/storage/status`) require a user in `ADMIN_USER_IDS` when auth is on. An empty list denies everyone. With auth off (single-user local mode) every caller is the owner. Tested. Remaining: the MCP server has its own access path (item 5). |
| 5 | Add auth | partial | Clerk JWT verification works. `AUTH_REQUIRED` defaults to `false`. The standalone MCP server has no auth of its own. It binds to `127.0.0.1` by default and refuses a non-loopback bind unless `MCP_ALLOW_UNAUTHENTICATED_REMOTE=true`. The gateway (per-client token and policy) is the authenticated path. |
| 6 | Check user permissions | partial | Project routes are scoped to the owner. Global routes are admin-only (item 4). Gateway policies deny when damaged (second pass below). Remaining: the MCP server is not per-user. |
| 7 | Sanitize user inputs | partial | Requests use typed models. The upload path is now constrained. Not reviewed field by field. |
| 8 | Protect against XSS | partial | Dashboard values go through `esc()`. Not every template was reviewed. The dashboard also uses inline `style=` attributes, which the Pages CSP (`style-src 'self'`) blocks, so some bars may not render. |
| 9 | Protect from SQL injection | pass | All queries go through the SQLModel ORM. No string-built SQL. The one raw statement is a fixed `ALTER TABLE` added for the owner column. |
| 10 | Check DB rules | partial | Row ownership is enforced in the app, not by database rules (no Postgres row-level security). Fine while the app is the only client. |
| 11 | Rate limiting | partial | Write requests are limited per client address in the app (`RATE_LIMIT_WRITES_PER_MINUTE`, default 60, returns 429 with `Retry-After`). The limit is per process, so several replicas need a limit at the proxy as well. Reads are not limited. Tested with a fake clock. |
| 12 | Spend caps | partial | `BUDGET_MAX_USD` and the dry-run estimate gate spending in the app. Provider-side caps (DeepSeek, Groq, Neon, Resend, Cloudflare) must be set in each provider's dashboard by you. |
| 13 | Secure file uploads | partial | Path restriction (fixed). Size limit added (`MAX_UPLOAD_MB`, default 100; larger files get 413). File types are not checked. Tested. |
| 14 | Enable CSRF protection | fixed | The API no longer accepts the `__session` cookie as a credential. Only the `Authorization` header counts, and a cross-site page cannot set that header without a CORS preflight. Tested. Live project streams use a 60-second ticket (`POST /auth/stream-ticket`), since EventSource cannot send headers. The ticket works only on a project stream, and the owner check still applies. The ticket sits in the stream URL, so it can appear in access logs for up to 60 seconds. |
| 15 | Check CORS settings | fixed | Wildcard no longer allows credentials. The default `CORS_ORIGINS=*` remains for local development, with a warning when auth is on. |
| 16 | Enable HTTPS | partial | The app does not terminate TLS and sets no HSTS. Use the host's or a proxy's TLS, then HSTS there. |
| 17 | Add security headers | pass (API) | Set on every API response (added here). The dashboard's CSP and headers live in `web/_headers`. |
| 18 | Secure cookies | partial | The app sets no cookies. Clerk sets `__session`; check its flags (Secure, HttpOnly, SameSite) in the Clerk dashboard. |
| 19 | Disable debug mode | pass | The Docker image runs uvicorn without `--reload`. Reload exists only in the sidecar's development flag. |
| 20 | Check production settings | partial | Warnings for CORS and auth are in place. A going-public checklist is in [deploy checklist, section 9](deploy-checklist.md#9-going-public). There is still no single fail-closed production profile. |

## Next, in order

1. Authentication for the standalone MCP server, or keep it off hosted deployments (item 5).
2. A production settings profile that refuses to start with unsafe values (items 2, 20).
3. An external review before a public launch.
4. Proxy-level rate limiting for multi-replica deployments (item 11).
5. Resolve the four remaining CodeQL notes.
