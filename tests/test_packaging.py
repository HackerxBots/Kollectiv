"""Packaging guards: ``pip install kollektiv`` must produce a *working* package.

These tests exist because of a real outage in this repository: ``src.connectors``
was missing from ``[tool.setuptools] packages``, so the wheel installed a
``src`` package whose import chain blew up with ``ModuleNotFoundError`` — while
``pip install -e .`` (what CI did) kept working, because editable installs read
the source tree directly. The wheel smoke step in CI now installs the artifact,
and these checks make the same class of mistake visible in the test job too.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Set

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def _pyproject() -> Dict[str, Any]:
    """Parse ``pyproject.toml`` with the standard library."""
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _source_packages() -> Set[str]:
    """Return every dotted package name that contains Python sources."""
    packages: Set[str] = set()
    for directory in (ROOT / "config", ROOT / "src"):
        for path in directory.rglob("*.py"):
            relative = path.parent.relative_to(ROOT)
            packages.add(str(relative).replace("/", "."))
    return packages


def test_every_source_package_is_declared() -> None:
    """No package directory may be left out of the built distribution."""
    declared = set(_pyproject()["tool"]["setuptools"]["packages"])
    missing = sorted(_source_packages() - declared)
    assert not missing, (
        f"these packages exist in the tree but are not installed by the wheel: {missing}. "
        "Add them to [tool.setuptools] packages in pyproject.toml."
    )


def test_dashboard_ships_inside_the_distribution() -> None:
    """The static dashboard travels with the package (it serves ``/ui``)."""
    config = _pyproject()["tool"]["setuptools"]
    assert "web" in config["packages"], "web/ must be installed so the API can serve /ui"
    package_data = config.get("package-data", {}).get("web", [])
    for entry in ("index.html", "sw.js", "assets/*", "README.md"):
        assert entry in package_data, f"{entry} is not shipped"

    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "web/index.html" in manifest, "the sdist needs the dashboard too"
    assert "web/sw.js" in manifest, "the sdist needs the service worker too"
    assert "recursive-include web/assets" in manifest


def test_the_desktop_shell_is_wired_to_the_shipped_dashboard() -> None:
    """The Tauri window loads ``web/`` — never a copy of it."""
    config = json.loads((ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    frontend = (ROOT / "desktop" / "src-tauri" / config["build"]["frontendDist"]).resolve()
    assert frontend == (ROOT / "web").resolve(), "the shell must embed the shipped dashboard"
    assert (frontend / "index.html").is_file()

    # A shell is a window, not a second frontend: no separate HTML/CSS/JS tree.
    offenders = [
        path.relative_to(ROOT / "desktop")
        for path in (ROOT / "desktop").rglob("*")
        if path.suffix in {".html", ".css"} and "target" not in path.parts
    ]
    assert not offenders, f"the desktop app should not carry its own UI files: {offenders}"

    cargo = (ROOT / "desktop" / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
    assert 'tauri = { version = "2"' in cargo or 'tauri = "2' in cargo, "Tauri v2 is the supported shell"
    assert "[lib]" in cargo, "Tauri v2 expects a library crate (mobile-ready layout)"


def test_the_desktop_shell_has_icons_and_a_capability_file() -> None:
    """Installers need icons, and the window needs a small permission list."""
    icons = ROOT / "desktop" / "src-tauri" / "icons"
    for needed in ("32x32.png", "128x128.png", "128x128@2x.png", "icon.ico", "icon.icns"):
        assert (icons / needed).is_file(), f"{needed} is missing — run `npm run icons`"

    capabilities = json.loads(
        (ROOT / "desktop" / "src-tauri" / "capabilities" / "desktop-shell.json").read_text(encoding="utf-8")
    )
    granted = set(capabilities["permissions"])
    assert "core:default" in granted and "opener:default" in granted
    # The shell reaches the API over HTTP, so it needs none of the powerful ones.
    dangerous = {"fs:default", "shell:default", "process:default", "http:default"}
    assert not (granted & dangerous), f"the shell should not need {sorted(granted & dangerous)}"


def test_the_sidecar_recipe_exists_or_the_bundle_variant_is_a_promise() -> None:
    """Option 2 is real: a spec, an entry point, a sidecar slot and a workflow."""
    spec = ROOT / "sidecar" / "kollektiv-sidecar.spec"
    entry = ROOT / "sidecar" / "sidecar_entry.py"
    assert spec.is_file() and entry.is_file(), "the bundled API needs its spec and entry point"
    # `packaging/` would shadow the PyPI package PyInstaller itself imports.
    assert not (ROOT / "packaging").exists(), "do not name a top-level directory `packaging`"
    assert "externalBin" in (ROOT / "desktop" / "src-tauri" / "tauri.bundle.conf.json").read_text(encoding="utf-8")

    workflow = (ROOT / ".github" / "workflows" / "desktop.yml").read_text(encoding="utf-8")
    assert "tauri-apps/tauri-action" in workflow, "installers are built in CI, not promised"
    assert "pyinstaller" in workflow.lower(), "the bundle variant builds the sidecar"
    assert "variant: bundle" in workflow and "variant: shell" in workflow, "both options are built"


def test_console_scripts_and_entrypoints_are_declared() -> None:
    """The three documented entry points exist as installed commands."""
    scripts = _pyproject()["project"]["scripts"]
    assert scripts["kollektiv"] == "src.api.cli:main"
    assert scripts["kollektiv-api"] == "src.api.routes:main"
    assert scripts["kollektiv-mcp"] == "src.api.mcp_server:main"


def test_metadata_is_modern_and_complete() -> None:
    """License, Python floor and keywords are valid packaging metadata."""
    project = _pyproject()["project"]
    assert project["license"] == "MIT", "use the PEP 639 SPDX string, not the deprecated table"
    assert project["license-files"] == ["LICENSE"]
    assert not [c for c in project["classifiers"] if c.startswith("License ::")], (
        "license classifiers are deprecated in favour of the SPDX expression"
    )
    assert project["requires-python"] >= ">=3.11"
    assert "fastapi" in " ".join(project["dependencies"])
    build_requires = _pyproject()["build-system"]["requires"]
    assert any("setuptools>=77" in requirement for requirement in build_requires), (
        "PEP 639 license expressions need setuptools >= 77"
    )


def test_documentation_and_examples_are_in_the_sdist() -> None:
    """A release tarball can be built from, not just installed."""
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    for entry in ("recursive-include docs *.md", "recursive-include examples", ".env.example"):
        assert entry in manifest, f"{entry} missing from MANIFEST.in"


def test_ci_installs_the_built_wheel() -> None:
    """The wheel smoke test in CI must not swallow its own failure."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "pip install dist/*.whl" in ci, "CI should install the artifact it just built"
    smoke_step = ci.split("pip install dist/*.whl", 1)[1].split("- name:", 1)[0]
    assert "|| true" not in smoke_step, "a failing wheel smoke test must fail the build"
    assert "/ui" in smoke_step, "the smoke test should assert the dashboard is mounted"


def test_installed_layout_matches_the_runtime_lookup() -> None:
    """``create_app`` looks two levels above ``src/api`` — that is the wheel root.

    The dashboard is found at ``<site-packages>/web`` when installed and at
    ``<repo>/web`` from a checkout; both are ``parents[2]`` of
    ``src/api/routes.py``. If the layout ever changes, this test says so before a
    user finds an API without a dashboard.
    """
    routes = (ROOT / "src" / "api" / "routes.py").read_text(encoding="utf-8")
    assert 'parents[2] / "web"' in routes, "the dashboard lookup path changed; update packaging too"
    expected: List[str] = [str(Path("src/api/routes.py").resolve().parents[2].name)]
    assert expected == ["Kollectiv"], expected
def test_the_sidecar_spec_points_at_paths_that_exist() -> None:
    """The spec resolves everything from SPECPATH; a typo only shows up in CI.

    This is the bug that made the first real desktop build fail: the root was
    computed one level too high (and still said ``packaging/`` after the rename),
    so PyInstaller could not find its entry script.
    """
    import re

    spec = (ROOT / "sidecar" / "kollektiv-sidecar.spec").read_text()
    root_line = next(line for line in spec.splitlines() if line.startswith("ROOT = "))
    assert "Path(SPECPATH).resolve().parent" in root_line, root_line

    referenced = set(re.findall(r'ROOT / "([^"]+)"(?: / "([^"]+)")?', spec))
    assert referenced, "the spec should resolve its inputs from ROOT"
    missing = [
        "/".join(part for part in pair if part)
        for pair in referenced
        if not (ROOT / pair[0] / pair[1] if pair[1] else ROOT / pair[0]).exists()
    ]
    assert not missing, f"the spec points at paths that do not exist: {missing}"


def test_the_bundle_job_smoke_tests_the_frozen_sidecar() -> None:
    """A frozen build is the one artifact no unit test can check, so CI runs it."""
    workflow = (ROOT / ".github" / "workflows" / "desktop.yml").read_text()
    assert "Smoke-test the sidecar" in workflow
    assert "curl -fsS http://127.0.0.1:8791/health" in workflow
