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

## The 20 points

| # | Point | Status | Evidence and what remains |
| --- | --- | --- | --- |
| 1 | Hide API keys | pass | Keys come from the environment or the encrypted store. The API and CLI never print them. `.env` is gitignored. Remaining: whoever runs the host can read its environment. |
| 2 | Check env variables | partial | `kollektiv check` and the settings warnings cover missing and inconsistent values. There is no single fail-closed production profile yet. |
| 3 | Check for keys in git | pass | The full history was scanned for key patterns. The only hits are AWS's documented example key `AKIAIOSFODNN7EXAMPLE` in `tests/test_r2.py`, which is not a secret. Remaining: add a secret scanner (gitleaks) to CI. |
| 4 | Protect admin routes | **fail** | `POST /sync`, `POST`/`DELETE /links`, and `POST /connectors/{name}/call` and `/probe` are open to any signed-in user. There is no admin role yet. **Next step.** |
| 5 | Add auth | partial | Clerk JWT verification works. `AUTH_REQUIRED` defaults to `false`. The MCP server has no auth at all (bound to localhost in Compose now). |
| 6 | Check user permissions | partial | Project routes are scoped to the owner (fixed). Global routes (item 4) and the MCP server are not. |
| 7 | Sanitize user inputs | partial | Requests use typed models. The upload path is now constrained. Not reviewed field by field. |
| 8 | Protect against XSS | partial | Dashboard values go through `esc()`. Not every template was reviewed. The dashboard also uses inline `style=` attributes, which the Pages CSP (`style-src 'self'`) blocks, so some bars may not render. |
| 9 | Protect from SQL injection | pass | All queries go through the SQLModel ORM. No string-built SQL. The one raw statement is a fixed `ALTER TABLE` added for the owner column. |
| 10 | Check DB rules | partial | Row ownership is enforced in the app, not by database rules (no Postgres row-level security). Fine while the app is the only client. |
| 11 | Rate limiting | **fail** | None on the API. Add it at the proxy, or in the app for the write routes. **Next step.** |
| 12 | Spend caps | partial | `BUDGET_MAX_USD` and the dry-run estimate gate spending in the app. Provider-side caps (DeepSeek, Groq, Neon, Resend, Cloudflare) must be set in each provider's dashboard by you. |
| 13 | Secure file uploads | partial | Path restriction (fixed). No size limit on uploads yet. |
| 14 | Enable CSRF protection | **fail** | The API accepts the Clerk `__session` cookie as a credential (`bearer_token`). Cookie-authenticated POSTs need CSRF protection, or cookie auth should be removed in favour of the `Authorization` header. **Next step.** |
| 15 | Check CORS settings | fixed | Wildcard no longer allows credentials. The default `CORS_ORIGINS=*` remains for local development, with a warning when auth is on. |
| 16 | Enable HTTPS | partial | The app does not terminate TLS and sets no HSTS. Use the host's or a proxy's TLS, then HSTS there. |
| 17 | Add security headers | pass (API) | Set on every API response (added here). The dashboard's CSP and headers live in `web/_headers`. |
| 18 | Secure cookies | partial | The app sets no cookies. Clerk sets `__session`; check its flags (Secure, HttpOnly, SameSite) in the Clerk dashboard. |
| 19 | Disable debug mode | pass | The Docker image runs uvicorn without `--reload`. Reload exists only in the sidecar's development flag. |
| 20 | Check production settings | partial | Warnings for CORS and auth are in place. No single production profile yet. |

## Next, in order

1. Admin role for `/sync`, `/links` and connector calls (item 4), and MCP auth or removal from the hosted build (item 5).
2. CSRF: stop accepting the cookie as a credential, or add CSRF tokens (item 14).
3. Rate limiting on write routes and an upload size limit (items 11, 13).
4. A production settings profile that refuses to start with unsafe values (items 2, 20).
5. Secret scanning in CI (item 3), and an external review before a public launch.
