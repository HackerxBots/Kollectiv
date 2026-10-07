"""Tests that keep the static dashboard in step with the API.

The dashboard in ``web/`` is deliberately dependency-free — plain HTML, CSS and
ES modules with no build step — which also means nothing else can catch a
renamed endpoint. These tests parse the shipped JavaScript and assert that every
path it calls really exists in the API's OpenAPI schema.

They also enforce the three house rules for the UI:

1. it is split across files (markup, stylesheet, module, icon) rather than one
   giant HTML file;
2. every interactive component defines hover, focus-visible and reduced-motion
   behaviour in CSS;
3. it ships no third-party script, font or tracker, and talks only to the API
   origin the operator configures.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

from config.settings import Settings

WEB = Path(__file__).resolve().parents[1] / "web"

#: Hosts the dashboard is allowed to link to or load from. Everything else —
#: fonts, CDNs, analytics — would be a supply-chain and privacy problem.
ALLOWED_HOSTS: Set[str] = {"github.com", "localhost", "127.0.0.1"}

_TRACKERS = re.compile(
    r"google-analytics|googletagmanager|gtag\s*\(|hotjar|mixpanel|posthog|plausible|"
    r"segment\.(?:io|com)|sentry|umami|clarity\.ms|doubleclick|facebook\.net",
    re.IGNORECASE,
)
_JS_URL = re.compile(r"\bapi\(\s*([`\"'])(.*?)\1", re.DOTALL)
_JS_POST = re.compile(r"method:\s*\"POST\"")
_SSE = re.compile(r"new\s+EventSource\(\s*")
_INTERPOLATION = re.compile(r"\$\{[^}]*\}")
_PARAMETER = re.compile(r"\{[^}]*\}")
_ABSOLUTE_URL = re.compile(r"https?://([A-Za-z0-9.-]+)")
_HOVER = re.compile(r"\.(kv-[a-z0-9-]+)[^{}]*:hover", re.DOTALL)

#: Components whose hover state is part of the design, not an optional extra.
HOVER_COMPONENTS = (
    "kv-card",
    "kv-btn",
    "kv-nav",
    "kv-pill",
    "kv-table",
    "kv-palette-item",
    "kv-brand",
)


def _read(relative: str) -> str:
    """Return the text of a file inside ``web/``."""
    return (WEB / relative).read_text(encoding="utf-8")


def _normalise(path: str) -> str:
    """Reduce a JS template literal and a FastAPI path to a comparable shape.

    ``/projects/${id}/files/${encodeURIComponent(p)}/url`` and
    ``/projects/{project_id}/files/{file_path:path}/url`` both become
    ``/projects/*/files/*/url``.
    """
    path = _INTERPOLATION.sub("*", path)
    path = _PARAMETER.sub("*", path)
    return path.rstrip("/") or "/"


def _js_calls() -> List[Tuple[str, str]]:
    """Extract ``(method, path)`` for every ``api(...)`` call in ``app.js``."""
    source = _read("assets/app.js")
    calls: List[Tuple[str, str]] = []
    for match in _JS_URL.finditer(source):
        tail = source[match.end() : match.end() + 240]
        method = "POST" if _JS_POST.search(tail) else "GET"
        calls.append((method, match.group(2)))
    return calls


def _schema_paths(settings: Settings) -> Dict[str, Set[str]]:
    """Map normalised path -> allowed methods, straight from the live app."""
    from src.api.routes import create_app

    schema = create_app(settings).openapi()
    return {
        _normalise(path): {method.upper() for method in operations if method != "parameters"}
        for path, operations in schema["paths"].items()
    }


def test_dashboard_calls_the_documented_endpoints(settings: Settings) -> None:
    """Every endpoint the dashboard calls exists, with the method it uses."""
    known = _schema_paths(settings)
    calls = _js_calls()
    assert calls, "app.js no longer calls the API — did the fetch wrapper change?"

    unknown = [f"{method} {path}" for method, path in calls if method not in known.get(_normalise(path), set())]
    assert not unknown, f"dashboard calls endpoints the API does not expose: {unknown}"


def test_dashboard_streams_live_project_state(settings: Settings) -> None:
    """The SSE consumer points at a route that is really registered."""
    source = _read("assets/app.js")
    assert _SSE.search(source), "app.js should open an EventSource for live updates"
    known = _schema_paths(settings)
    assert "GET" in known.get("/projects/*/events/stream", set())


def test_dashboard_is_split_across_files() -> None:
    """Markup, styles, behaviour and icon live in separate files."""
    html = _read("index.html")
    for asset in ("assets/styles.css", "assets/app.js", "assets/favicon.svg"):
        assert (WEB / asset).is_file(), f"{asset} is referenced but missing"
        assert asset in html, f"index.html should link {asset}"
    assert 'type="module"' in html, "app.js should be loaded as an ES module"
    assert "<style" not in html.lower(), "styles belong in assets/styles.css"
    assert "<script>" not in html.lower(), "behaviour belongs in assets/app.js"
    assert (WEB / "README.md").is_file(), "document how to deploy the dashboard"
    assert len(html.encode()) < 40_000, "index.html is growing into a monolith again"


def test_dashboard_defines_hover_and_focus_states() -> None:
    """The interactive components ship real hover, focus and motion rules."""
    css = _read("assets/styles.css")
    hovered = set(_HOVER.findall(css))
    missing = [name for name in HOVER_COMPONENTS if name not in hovered]
    assert not missing, f"no :hover rule for {missing}"
    assert ".kv-table tbody tr:hover" in css, "table rows should highlight on hover"
    assert ":focus-visible" in css, "keyboard focus must be visible"
    assert "prefers-reduced-motion" in css, "respect prefers-reduced-motion"
    assert "@media (prefers-color-scheme: light)" in css, "support light mode"
    assert "--kv-" in css, "colours should come from design tokens"


def test_dashboard_ships_no_trackers_or_remote_assets() -> None:
    """No analytics, no CDN, no third-party origin of any kind."""
    for name in ("index.html", "assets/styles.css", "assets/app.js"):
        text = _read(name)
        assert not _TRACKERS.search(text), f"{name} references a tracker"
        hosts = set(_ABSOLUTE_URL.findall(text))
        assert hosts <= ALLOWED_HOSTS, f"{name} reaches out to {sorted(hosts - ALLOWED_HOSTS)}"
        for remote in ("cdn.", "unpkg", "jsdelivr", "googleapis", "@import url("):
            assert remote not in text, f"{name} loads a remote asset ({remote})"


def test_dashboard_documents_the_api_base() -> None:
    """Operators can point the page at their own API without editing files."""
    source = _read("assets/app.js")
    assert "localStorage" in source and "kollektiv.api" in source
    assert "URLSearchParams" in source and '"api"' in source
    assert "authorization" in source.lower(), "send the Clerk/bearer token when present"
    assert 'location.pathname.startsWith("/ui")' in source, "detect same-origin hosting under /ui"
    assert '"/auth/me"' not in source, "the dashboard only calls documented endpoints"
