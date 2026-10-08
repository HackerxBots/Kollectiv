# Why this directory is called `sidecar/` and not `packaging/`: a top-level
# directory named `packaging` shadows the PyPI `packaging` distribution, which
# PyInstaller, pip and setuptools all import. Keeping the name would break the
# build it contains — do not "tidy" it back.
#
# PyInstaller spec for the Kollektiv API sidecar.
#
# What this builds: one executable that *is* the orchestrator — the FastAPI app
# from `src.api.routes`, the dashboard from `web/`, SQLite, the connectors and
# the MCP surface, with a Python interpreter inside. The desktop shell
# (`desktop/`) bundles it as a Tauri sidecar so "download Kollektiv" means one
# installer and no terminal.
#
#   pip install pyinstaller
#   pyinstaller sidecar/kollektiv-sidecar.spec --noconfirm
#   ./dist/kollektiv-api            # http://127.0.0.1:8000/ui/
#
# The output must be named `<name>-<target-triple>` for Tauri to find it
# (`rustc -Vv | grep host` prints the triple); the CI workflow in
# `.github/workflows/desktop.yml` does that renaming.
#
# Deliberate choices, so the build stays reproducible and honest:
#
# * **onefile, despite the cost.** Tauri's `externalBin` requires a single
#   executable, so the sidecar is one file; the price is that it unpacks its
#   ~100 MB to a temp directory on first start (a second or two once per launch,
#   versus an instant onedir start). Run it *unedited* if you only want a
#   standalone server and prefer the faster start.
# * **No PyYAML dependency assumption.** `.kollektiv.yml` parsing falls back to
#   `src/utils/yaml_subset.py` when PyYAML is absent; the spec collects it when it
#   is installed and the app keeps working when it is not.
# * **The dashboard is a data file.** `web/` ships beside the code so `/ui/`
#   works offline and the desktop shell can also serve it.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# ``SPECPATH`` is the directory containing this file (``sidecar/``), so the
# repository root is its parent — not its grandparent.
ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH is PyInstaller's
NAME = "kollektiv-api"

# The bundled dashboard and configuration the app expects next to itself.
# ``.`` means "next to the executable", which is where `create_app()` looks when
# the sources are not on disk (a frozen build has no ``src/`` tree to walk).
datas = [
    (str(ROOT / "web"), "web"),
    (str(ROOT / "config"), "config"),
    (str(ROOT / ".env.example"), "."),
]

# Connectors, storage backends and the gateway import their modules lazily, so
# PyInstaller cannot see them by following imports.
hiddenimports = (
    collect_submodules("src.connectors")
    + collect_submodules("src.storage")
    + collect_submodules("src.gateway")
    + ["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto"]
)

a = Analysis(  # noqa: F821 - PyInstaller injects these names
    [str(ROOT / "sidecar" / "sidecar_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    icon=str(ROOT / "desktop" / "src-tauri" / "icons" / "icon.ico") if sys.platform == "win32" else None,
)

# One file in, one file out: `dist/kollektiv-api` (or `dist\kollektiv-api.exe`).
# `.github/workflows/desktop.yml` renames it to the target triple Tauri expects:
#   desktop/src-tauri/binaries/kollektiv-api-<target-triple>[.exe]
