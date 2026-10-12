# Summary

<!-- One or two sentences: what changes, and why. -->

Closes #

## What changed

- [ ] …

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Tests / CI
- [ ] Refactor (no behaviour change)

## Gates

- [ ] `pytest -q` passes (and new behaviour has tests)
- [ ] `ruff check .` passes
- [ ] `mypy src config examples` passes
- [ ] Tests are hermetic (no network, no real credentials)

## Documentation

- [ ] `CHANGELOG.md` updated under `[Unreleased]`
- [ ] `README.md` updated if the pitch, quickstart, feature table or test count changed
- [ ] the matching page in `docs/` updated if commands, config keys or interfaces changed
- [ ] `.env.example` updated if new configuration was added
- [ ] `DECISIONS.md` updated if an agreed decision changed

## Project constraints

- [ ] Nothing requires a paid service (free tier or self-hosted is enough)
- [ ] No telemetry, analytics or data collection was added
- [ ] Secrets/tokens go through the token store and are masked in logs

## Notes for reviewers

<!-- Anything non-obvious: trade-offs, follow-ups, screenshots for UI changes. -->
