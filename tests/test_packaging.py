"""Packaging guards: ``pip install kollektiv`` must produce a *working* package.

These tests exist because of a real outage in this repository: ``src.connectors``
was missing from ``[tool.setuptools] packages``, so the wheel installed a
``src`` package whose import chain blew up with ``ModuleNotFoundError`` — while
``pip install -e .`` (what CI did) kept working, because editable installs read
the source tree directly. The wheel smoke step in CI now installs the artifact,
and these checks make the same class of mistake visible in the test job too.
"""

from __future__ import annotations

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
