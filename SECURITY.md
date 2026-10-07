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

## Hardening checklist for operators

- [ ] `SECRET_KEY` set (long, random) and backed up with the database.
- [ ] `AUTH_REQUIRED=true` with Clerk keys when the API is reachable publicly.
- [ ] TLS in front (required for GitHub webhooks); restrict `CORS_ORIGINS`.
- [ ] `GITHUB_WEBHOOK_SECRET` and `CLERK_WEBHOOK_SECRET` set.
- [ ] Database and shared drive credentials scoped to the minimum (R2 tokens
      with object read/write on one bucket, not account-wide).
- [ ] Backups tested (see issue #18 for the built-in job).
- [ ] `EVENT_WEBHOOKS` URLs treated as secrets (they usually carry tokens).
