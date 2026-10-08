# Repository settings (maintainer checklist)

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

Everything in this repository that lives in **files** is already in place: the
README, `LICENSE`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`, `SECURITY.md`, the
issue forms, the pull-request template, `dependabot.yml` and `codeql.yml`.

This page is the other half — the switches that only exist in the GitHub UI.
They cannot be set from a repository-scoped automation token (every call to
`PATCH /repos/...` and to the security-feature endpoints answers
`403 Resource not accessible by integration`), so they are a five-minute manual
pass. Each line says where to click and what to type.

Two of them also have a **sequencing** rule: GitHub reads `SECURITY.md` and
`codeql.yml` from the **default branch**, so until this work is on `main`, the
Security tab says *"Security policy · Disabled"* and *"Code scanning alerts ·
Needs setup"* even though both files exist in the repository. That is expected,
not a bug.

---

## 1. Profile (Settings → General)

**Description** — paste exactly this (249 characters):

```
Plan, dispatch and review work across pooled AI workers, with free shared storage (Cloudflare R2/TeraBox), GitHub sync and connectors. Self-hosted, open source, no telemetry.
```

**Website** — leave empty until the dashboard is deployed, then set it to the
Pages URL (`https://hackerxbots.github.io/Kollectiv/` from
`.github/workflows/pages.yml`, or `https://<project>.pages.dev` from Cloudflare —
see `web/README.md`).

**Topics** — the sidebar uses these to explain and find the project:

```
ai-agents  multi-agent  orchestrator  autonomous-agents  self-hosted
open-source  python  fastapi  llm  mcp  cloudflare-r2  free-tier
github-sync
```

**Social preview** — upload a screenshot of `web/index.html` (1280×640). The
page itself is the best explanation of what the project is; no marketing art
needed.

## 2. Code security and analysis (Settings → Code security and analysis)

| Item | Action | Notes |
| --- | --- | --- |
| Security policy | nothing to click | Flips to *enabled* and shows a "Report a vulnerability" button as soon as `SECURITY.md` is on `main`. |
| Security advisories | already enabled | — |
| **Private vulnerability reporting** | **Enable** | The address `SECURITY.md` points at; requires the file on the default branch. |
| **Dependabot alerts** | **Enable** | Free for public repositories; warns when a dependency in `pyproject.toml` gets a CVE. |
| **Dependabot security updates** | **Enable** | Turns those alerts into pull requests automatically. |
| Dependabot version updates | already configured | `.github/dependabot.yml` — grouped weekly pip, Actions, monthly Docker. |
| **Code scanning** | **Set up → Advanced** | Choose *Advanced* so it keeps the existing `.github/workflows/codeql.yml`; *Default* would replace it with a generated workflow. Alerts appear once `codeql.yml` has run on `main` (it already runs on pull requests). |
| Secret scanning | already enabled | Also enable **Push protection** if the toggle is offered — it blocks a token before it is committed. |

Result after the pass: every row on that settings page reads *enabled*, and the
Security tab shows a security policy, live advisories, and open/closed CodeQL
alerts with their triage (`SECURITY.md` documents the expected baseline and the
findings that are intentional).

## 3. Rules for `main` (Settings → Rules → Rulesets)

The branch is the public record, so make "no merging by accident" a platform
rule rather than a convention:

- **Target**: the default branch (`main`).
- **Require a pull request before merging**: 1 approval; dismiss stale approvals.
- **Require status checks to pass**: `test (3.11)`, `test (3.12)`,
  `Build the distribution`, and `Analyse Python` (CodeQL).
- **Block force pushes** and **restrict deletions**.
- Optional: *Require linear history* — the release checklist assumes tags point
  at commits on `main`.

## 4. Pages (Settings → Pages)

- **Source: GitHub Actions** — `.github/workflows/pages.yml` publishes `web/` to
  `https://<owner>.github.io/<repo>/` with no configuration.
- Or **Cloudflare Pages** for the same files with a custom domain and
  `_headers`/`_redirects` support (walkthrough in `web/README.md`); set
  `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID` and the workflow uses it
  instead.

## 5. What the API could and could not do

| Done through the API | Blocked (403, manual) |
| --- | --- |
| Commits, branches, pull requests and comments | Repository description, website, topics, social preview |
| Issues (labels, bodies, comments) | Private vulnerability reporting, Dependabot alerts and security updates |
| Releases (create, edit, publish, notes) | Code-scanning default setup, secret-scanning push protection, rulesets |

Nothing about the project is waiting on these switches — they change how the
repository *presents* itself and what GitHub watches for you.
