# Operations

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

## Operations

### Releases, versioning and the README

Kollektiv ships small and often, and documents every step. **Everything is a
beta tag for now:** the interfaces still move, so every tag is `v0.3.0-beta.1`,
`v0.3.0-beta.2`, … and the release notes open with a beta warning. The release
itself is published as a *normal* GitHub release (not a pre-release) so it shows
up in the sidebar's "Latest" widget instead of hiding — the tag is the honest
signal, and the README is updated in the same PR as the change.

- **Versioning** — `0.x` while the API/config still evolves. Inside a minor
  line: `-beta.N` increments per batch of changes, `MAJOR`/`MINOR` bumps when
  behaviour changes, `PATCH` (`v0.3.1-beta.1`) for fixes only.
- **Changelog first.** `CHANGELOG.md` is updated in the same PR; the release
  workflow refuses to publish a tag whose version has no changelog section.
- **Beta tags, findable releases:** the workflow keeps `vX.Y.Z-beta.N` tagging
  and a beta banner in the notes, attaches the sdist + wheel, and publishes the
  release so it is listed (pre-releases are skipped by "Latest").
- **The README is part of the release.** The peak block (release name, test
  count) and the tool/table sections change with it; the checklist in
  `CLAUDE.md` keeps that honest.

Cutting a release:

```bash
# 1. bump version in pyproject.toml and src/__init__.py
# 2. move the CHANGELOG "Unreleased" entries under the new version
# 3. update the README peak block
git tag v0.3.0-beta.1 && git push origin v0.3.0-beta.1   # workflow publishes the release
```

### Repository settings (maintainers)

The switches that exist only in the GitHub UI — description, topics, Dependabot
alerts, private vulnerability reporting, code scanning, rulesets — are listed
with the exact values to paste in [docs/repo-settings.md](repo-settings.md).
Worth knowing: GitHub reads `SECURITY.md` and `codeql.yml` from the **default
branch**, so the Security tab reports "security policy · disabled" and "code
scanning · needs setup" for as long as they live on a branch — the files are in
place, the settings follow the merge.

### Health and observability

- `GET /health` — always `200`; lists subsystems, counts and configuration
  warnings so a supervisor can distinguish "running degraded" from "down".
- `kollektiv check --json` — the same information from a shell/cron.
- Structured logs (`LOG_LEVEL=DEBUG` shows request payloads with secrets
  redacted) plus an append-only `events` table: every dispatch, review, sync
  and webhook lands there with project id, agent id and result.
- `GET /agents/status?probe=true` actively pings each worker endpoint instead of
  reporting the cached snapshot.

### Background sync

The cron pass runs inside the API process (APScheduler), every
`CRON_INTERVAL_MINUTES`. Set `CRON_ENABLED=false` when you scale the API
horizontally so only one replica performs the sync, or run the CLI `sync`
command from an external scheduler (cron, Kubernetes CronJob).

### Storage hygiene

- `data/kollektiv.db` — projects, tasks, plans, events, encrypted tokens.
- `data/workspace/<project_id>/` — collected files for the local project; the
  copy in the shared drive (R2/TeraBox) is authoritative, this is the cache.
- Delete a project's artifacts with `GET /projects/{id}/files` plus the pool's
  `delete_file`, or remove the remote folder `/Kollektiv/<project_id>`.

### Scaling

| Symptom | Knob |
| --- | --- |
| Tasks queue behind each other | add `ARENA_ACCOUNTS`, or raise `ARENA_MAX_CONCURRENCY` |
| Storage fills up | add `R2_ACCOUNTS` buckets or `TERABOX_ACCOUNTS` (the pool routes by free space) |
| Brain is slow/expensive | keep the cheap model for planning, set a larger `BRAIN_MODEL` only for reviews |
| Sync takes long | raise `CRON_INTERVAL_MINUTES` |

SQLite handles a single API process comfortably. For multiple replicas, point
`DATABASE_URL` at Neon (the `[postgres]` extra installs the driver) and disable
the in-process scheduler so the cron runs in exactly one place.

---
