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
| 2 | Check env variables | partial | `kollektiv check` and the settings warnings cover missing and inconsistent values. Production is now fail-closed for the two values that matter most: it refuses to start without `SECRET_KEY`, and without `AUTH_REQUIRED=true` unless `ALLOW_OPEN_API=true` is set (third pass). Other values are still warnings. |
| 3 | Check for keys in git | pass | Full history audit on 2026-10-12: every blob reachable from any ref (62 commits, 632 blobs) was scanned with detect-secrets 1.5.0 (`python scripts/secret_scan.py --history`). Every hit is a fixture or placeholder (`gsk_test_key_…`, the test `SECRET_KEY`s, `user:password` in examples), a documented example key (AWS's `AKIAIOSFODNN7EXAMPLE` and its secret, the `AKIA` prefix in a comment), a lockfile integrity hash, or the baseline's own hashes. No live credential was found. CI runs `scripts/secret_scan.py` on every tracked file against the reviewed baseline (`.secrets.baseline`, job *Secret scan*). History is not scanned in CI, because old fixtures would need their own baseline. Re-run the history audit before each release, and rotate any secret that is ever found in history, even if it was later removed. **Reconciled 2026-10-12 (second pass).** The history scan now covers more commits, so it reports six blobs. Each was read: test placeholders (`tok`), the AWS documentation example key in `tests/test_secret_scan.py` and its CI comment, code that mentions the word "secret", and one fake credential-shaped URL (`user:pw@`) in an early version of `tests/test_sponsors.py`, since replaced. None is a live credential. They are not added to `.secrets.baseline`, because that baseline covers the current tree only. Rotate anything that is ever found live. |
| 4 | Protect admin routes | fixed | Admin routes (`/sync`, `POST`/`DELETE /links`, connector call and probe, `/agents/status`, `/agents/health`, `/budget`, `/storage/status`) require a user in `ADMIN_USER_IDS` when auth is on. An empty list denies everyone. With auth off (single-user local mode) every caller is the owner. Tested. Remaining: the MCP server has its own access path (item 5). |
| 5 | Add auth | partial | Clerk JWT verification works. `AUTH_REQUIRED` defaults to `false`. The standalone MCP server has no auth of its own. It binds to `127.0.0.1` by default and refuses a non-loopback bind unless `MCP_ALLOW_UNAUTHENTICATED_REMOTE=true`. The gateway (per-client token and policy) is the authenticated path. |
| 6 | Check user permissions | pass (app) | Checked route by route on 2026-10-12: all 14 `/projects/{id}/...` routes depend on `project_access`, which returns 404 to anyone who is not the owner. Key routes act only on the caller's own identity. Link and connector routes are admin-only. Remaining: the MCP server is not per-user (its own gateway policy applies). |
| 7 | Sanitize user inputs | partial | Requests use typed models. The upload path is now constrained. Not reviewed field by field. |
| 8 | Protect against XSS | pass (dashboard) | Checked on 2026-10-12: every `innerHTML` site interpolates user-controlled text through `esc()`, `pill()` or `textContent`. The remaining interpolations are numbers, booleans or fixed strings. A test now fails if an inline `style=` attribute is added, because the Pages CSP (`style-src 'self'`) would block it (third pass). |
| 9 | Protect from SQL injection | pass | All queries go through the SQLModel ORM. No string-built SQL. The one raw statement is a fixed `ALTER TABLE` added for the owner column. |
| 10 | Check DB rules | partial | Row ownership is enforced in the app, not by database rules (no Postgres row-level security). Fine while the app is the only client. |
| 11 | Rate limiting | partial | Write requests are limited per client address in the app (`RATE_LIMIT_WRITES_PER_MINUTE`, default 60, returns 429 with `Retry-After`). The limit is per process, so several replicas need a limit at the proxy as well. Reads are not limited. Tested with a fake clock. |
| 12 | Spend caps | partial | `BUDGET_MAX_USD` and the dry-run estimate gate spending in the app. Provider-side caps (DeepSeek, Groq, Neon, Resend, Cloudflare) must be set in each provider's dashboard by you. |
| 13 | Secure file uploads | pass (scoped) | There is no multipart upload. The one upload route archives a file already on the server, and only from inside the workspace (checked before the file is opened). Size limit `MAX_UPLOAD_MB` (default 100). File types do not apply because no bytes come from the client. Tested. |
| 14 | Enable CSRF protection | fixed | The API no longer accepts the `__session` cookie as a credential. Only the `Authorization` header counts, and a cross-site page cannot set that header without a CORS preflight. Tested. Live project streams use a 60-second ticket (`POST /auth/stream-ticket`), since EventSource cannot send headers. The ticket works only on a project stream, and the owner check still applies. The ticket sits in the stream URL, so it can appear in access logs for up to 60 seconds. |
| 15 | Check CORS settings | fixed | Wildcard no longer allows credentials. The default `CORS_ORIGINS=*` remains for local development, with a warning when auth is on. |
| 16 | Enable HTTPS | partial | The app does not terminate TLS. In production it sends HSTS (`max-age=31536000; includeSubDomains`) on every response. Terminate TLS at the host or proxy. |
| 17 | Add security headers | pass (API) | Set on every API response (added here). The dashboard's CSP and headers live in `web/_headers`. |
| 18 | Secure cookies | partial | The app sets no cookies. Clerk sets `__session`; check its flags (Secure, HttpOnly, SameSite) in the Clerk dashboard. |
| 19 | Disable debug mode | pass | The Docker image runs uvicorn without `--reload`. Reload exists only in the sidecar's development flag. |
| 20 | Check production settings | partial | Production refuses to start without `SECRET_KEY`, and without auth unless the localhost-only opt-in is set (third pass). Warnings cover CORS and the other settings. A going-public checklist is in [deploy checklist, section 9](deploy-checklist.md#9-going-public). |

## Third pass (2026-10-12, item audit and production)

| Severity | Finding | Fix |
| --- | --- | --- |
| High | Production with `AUTH_REQUIRED=false` started with every project, key and connector open to anyone who could reach it. | `ENVIRONMENT=production` now refuses to start in that state, with the setting to change named in the error. The desktop sidecar, which is one user on localhost, sets `ALLOW_OPEN_API=true` only when it binds to a loopback address. A test covers both paths. |
| High | The dashboard's Pages CSP (`style-src 'self'`) blocks inline `style=""` attributes. About 30 of them (layout, skeletons, progress bars, the 404 page) would have silently disappeared on Cloudflare Pages, and only local runs would have looked right. | Static styles became classes. Progress widths are set through the CSSOM. A test fails if an inline `style` attribute comes back, and another asserts the CSP still forbids inline styles. Checked in Chromium under the real `_headers` CSP: no violations. |
| Medium | Broken links: 14 files pointed at `github.com/HackerxBots/Kollektiv`, which returns 404. The repository is `Kollectiv`. This included the footer, the README badges, the install command and the security advisory link. | Corrected everywhere. Checked: the new URL returns 200 and the old one 404. |
| Medium | The changelog's 0.1.0 to 0.4.0 links point at git tags that do not exist on GitHub. The only tag is `v0.3.0-beta.1`. | Not changed. Needs a decision: create the tags, or re-point the links. |
| Info | Upload path, ownership and XSS were audited by reading the code, not by an external reviewer. | Results are in the table above. |

Feedback is a footer link that opens GitHub in a new tab when clicked. The dashboard sends nothing. Back-to-top is a button that appears after 480 px of scroll.

## Next, in order

1. Authentication for the standalone MCP server, or keep it off hosted deployments (item 5).
2. An external review before a public launch.
3. Proxy-level rate limiting for multi-replica deployments (item 11).
4. Resolve the four remaining CodeQL notes.
5. Decide the changelog tag scheme (see the third pass).
