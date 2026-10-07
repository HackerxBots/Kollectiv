# Kollektiv desktop

Two ways to get a native window, in the order they are worth doing.

| Option | What you download | API | Status |
| --- | --- | --- | --- |
| **1. Shell** (`desktop/`) | 5–15 MB installer | runs anywhere: your laptop, a VM, a container, the Cloudflare tunnel | **built** — `tauri build` |
| **2. Bundle** (shell + sidecar) | 60–120 MB installer | starts with the app, on `127.0.0.1`, no terminal, nothing to configure | **wired** — `sidecar/` and `.github/workflows/desktop.yml` |

Both use the same `web/` dashboard that Cloudflare Pages publishes: the shell is a
window, not a second frontend. The difference is only whether the Python engine
comes with it.

## Option 1 — the shell

```bash
cd desktop
npm install                     # @tauri-apps/cli only
npm run dev                     # loads http://localhost:8088/ui/ while you work
npm run build                   # installers in src-tauri/target/release/bundle/
```

At runtime the dashboard asks where the API is (the same field as in a browser)
and remembers it locally. Point it at `http://127.0.0.1:8000` for an API on the
same machine, or at a tunnel URL for one somewhere else.

The shell's whole job is a window: `src-tauri/src/lib.rs` is ~120 lines, and the
only things it adds over a tab are opening external links in the real browser and
reflecting the connection state in the window title. It has no filesystem, shell
or process permissions (`src-tauri/capabilities/desktop-shell.json`), because the
orchestrator is reached over HTTP exactly like in a browser.

Icons come from `web/assets/favicon.svg` — the project's own mark:

```bash
npm run icons                   # tauri icon ../web/assets/favicon.svg
```

## Option 2 — bundle the API as a sidecar

The API becomes a **sidecar**: one executable, built by
`sidecar/kollektiv-sidecar.spec`, that the shell runs as a child process.

```bash
pip install pyinstaller
pyinstaller sidecar/kollektiv-sidecar.spec --noconfirm     # -> dist/kollektiv-api/
TARGET=$(rustc -Vv | sed -n 's/host: //p')
cp -r dist/kollektiv-api "desktop/src-tauri/binaries/kollektiv-api-${TARGET}"
cd desktop && npm run build
```

The sidecar listens on `127.0.0.1:8000` only, keeps its database and workspace in
`~/.kollektiv/`, reads the same `.env` every other entry point reads (see
`sidecar/sidecar_entry.py`), and prints the URL it serves.

In the **bundle** the shell starts it on `127.0.0.1:8765` and kills it when the
window closes, so a stale process never holds the port. Its settings come from
`~/.kollektiv/.env` (or the working directory's `.env`), which is exactly what
`kollektiv keys` writes — so a bundled install needs one command, once, and then
never a terminal again.

**Why this is not "one placeholder away from done".** Everything above works in a
checkout; what is *not* free is distribution, and the honest list is:

* **code signing** — macOS needs an Apple Developer certificate and a
  notarisation pass, Windows needs an Authenticode certificate, or users get a
  scary warning on first launch;
* **three builds per release** — the workflow does them, but each takes minutes
  and each produces a different binary;
* **a Python per platform** — the sidecar is built on the runner for that
  platform, so "the desktop app" is three artifacts, not one;
* **maintenance** — every dependency upgrade re-runs all of that.

None of that needs new product code, which is why the workflow is checked in
rather than promised. Get installers from
**Actions → Desktop installers → Run workflow**, or push a `desktop-v*` tag. With
no signing secrets configured the build still succeeds and produces *unsigned*
installers; add `APPLE_*`, `AZURE_*` and `TAURI_SIGNING_*` secrets (the names are
in the workflow) when you want signed ones.

## Is the engine in Rust yet?

No, and `docs/performance.md` has the measurements explaining why the Python
orchestrator stays Python: a run's wall clock is model latency, and the Python
side is under 1 % of it. The Rust in this directory is a launcher. If a profiler
ever names a Python function that matters, the move is a PyO3 extension module —
not a rewrite of the engine, the connectors, the budget ledger and the test suite.
