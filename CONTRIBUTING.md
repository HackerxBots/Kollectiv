# Contributing to Kollektiv

Thanks for wanting to help. This project values small, reviewable changes,
hermetic tests and honest documentation over volume.

## Quick start

```bash
git clone https://github.com/HackerxBots/Kollektiv.git
cd Kollektiv
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                 # 287 hermetic tests, ~10 s
kollektiv bootstrap       # prepares the database/workspace, prints the checklist
kollektiv serve-api       # dashboard at http://localhost:8000/ui
```

## The three gates

Every pull request must be green on all three:

```bash
pytest -q                 # tests (add some with behaviour changes)
ruff check .              # lint
mypy src config examples  # types
```

CI runs the same on Python 3.11 and 3.12, plus a packaging job, and posts any
failures back onto your PR.

## Rules that are not negotiable

1. **Tests stay hermetic.** No network, no real credentials, no clock
   dependencies: use `pytest.importorskip`, `httpx.MockTransport` and the fakes
   in `tests/conftest.py`. A test that reaches the internet works on your
   machine and fails everywhere else (this has already happened once).
2. **No telemetry.** Nothing collected, no analytics, no identifiers, no usage
   pings — in the API, CLI or dashboard. See `SECURITY.md` and the README's
   privacy section.
3. **Free on the critical path.** A new feature cannot require a paid service;
   paid/free-tier integrations stay optional and degrade gracefully.
4. **Arena stays the default worker provider.** Other providers are welcome as
   options, never as requirements.
5. **Credentials go through the token store.** Encrypted at rest, masked in
   logs and `/health`, never committed.
6. **All async, all `httpx`**, retries on every external call, module docstring
   and type hints everywhere (see `CLAUDE.md` for the full conventions).

## What to work on

- Look for issues labelled **good first issue**.
- `DECISIONS.md` is the agreed backlog: accepted items have issues, rejected
  ones are recorded so they are not re-litigated.
- For anything larger than a bug fix, open an issue (or comment on one) first —
  it saves you building something that does not fit.

## Making a change

```bash
git checkout -b feat/short-description
# ... edit, add tests ...
pytest -q && ruff check . && mypy src config examples
```

Commit messages use a prefix: `feat:`, `fix:`, `test:`, `docs:`, `chore:`.
Keep pull requests focused; one idea per PR is easier to review and revert.

**Update in the same PR:**

- `CHANGELOG.md` — add a line under `[Unreleased]`;
- the matching page in `docs/` — and the README only if the pitch, the
  quickstart, the feature table or the test count changed (the README is a front
  page now; depth lives in `docs/`);
- `DECISIONS.md` — if you are changing an agreed decision (say why);
- `.env.example` — if you added configuration;
- the peak block (release name, test count) — see the release checklist below.

## Adding a connector

Connectors are the most common contribution and the most fun:

1. Subclass `Connector` in `src/connectors/<service>.py`.
2. Declare `name`, `category`, `description`, `required_env` and `actions()`
   (name, description, params, `dangerous` for anything that writes).
3. Implement `call()` — never raise a bare exception: use the typed errors in
   `src/utils/errors.py` and let the registry handle isolation.
4. Add a `probe_action`/`probe_params` (a cheap read) so
   `kollektiv connectors --probe` can verify it.
5. Register it in `ConnectorRegistry.from_settings()`.
6. Add tests with `httpx.MockTransport` — happy path *and* the failure paths
   (401, 403, 429, 5xx).
7. Document it in the README connectors table and `.env.example`.

## Release checklist (maintainers)

1. Bump `version` in `pyproject.toml` **and** `src/__init__.py` (SemVer).
2. Move `CHANGELOG.md`'s `[Unreleased]` entries under the new version with
   today's date and add the compare links.
3. Update the README peak block (status, test count) and any changed page in
   `docs/`;
   table or command.
4. Tag and push: `git tag v0.x.0-beta.N && git push origin v0.x.0-beta.N`.
   The workflow builds the sdist/wheel, refuses to publish without a changelog
   section, and publishes the release with notes from `CHANGELOG.md`.
5. Never rewrite a published tag; cut a patch release instead.

## Code of conduct

Participation is covered by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Security
issues go through [SECURITY.md](SECURITY.md) — never a public issue.

## License

Contributions are accepted under the [MIT licence](LICENSE).
