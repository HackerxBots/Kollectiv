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

import json
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


def test_dashboard_is_not_plain() -> None:
    """The visual language that makes it a 2026 product, not a grey admin panel."""
    css = _read("assets/styles.css")
    # Aurora: blurred, drifting colour fields behind everything, plus grain.
    assert "kv-drift" in css and "@keyframes kv-drift" in css, "the aurora should drift"
    assert "radial-gradient" in css and "filter: blur(" in css, "the backdrop is gradients + blur"
    assert "mix-blend-mode: overlay" in css, "grain keeps the gradients from banding"
    # Glass: real backdrop blur with a luminous inner edge.
    assert css.count("backdrop-filter") >= 8, "surfaces should be frosted glass"
    assert "kv-glass-edge" in css, "glass needs its 1px inner highlight"
    assert "saturate(" in css, "frosted glass should lift saturation"
    # Depth: 3D tilt driven by custom properties + a pointer spotlight.
    assert "perspective(1000px)" in css and "--kv-rx" in css, "cards should tilt in 3D"
    assert "translateZ(" in css, "spark tiles live off the card plane"
    assert "--kv-tint" in css, "each subsystem owns a hue"
    # Candy palette + generous shape.
    for hue in ("--kv-grape", "--kv-cyan", "--kv-pink", "--kv-lime", "--kv-peach"):
        assert hue in css, f"{hue} missing from the palette"
    assert "--kv-radius-xl: 32px" in css, "generous rounded edges"


def test_dashboard_has_onboarding_and_mobile_chrome() -> None:
    """First-run onboarding, the mobile tab bar and the slide-in sidebar exist."""
    html = _read("index.html")
    js = _read("assets/app.js")
    assert 'id="onboard"' in html, "the onboarding overlay should be in the markup"
    assert html.count('class="kv-onboard-step"') == 3, "three onboarding steps"
    assert 'id="tour-start"' in html, "onboarding must be reopenable"
    assert 'class="kv-tabbar"' in html, "phones get a thumb-sized tab bar"
    assert 'id="menu-toggle"' in html, "the sidebar needs a mobile toggle"
    for hook in ("initOnboarding", "initTilt", "initSidebar", "renderOnboardStep", "confetti"):
        assert f"function {hook}(" in js, f"{hook} should exist in app.js"
    assert "kollektiv.onboarded" in js, "finishing onboarding is remembered locally"
    assert "pointermove" in js, "the 3D tilt follows the pointer"
    # Every table cell carries the label the mobile card layout shows.
    assert 'data-label="Project"' in js and 'data-label="Status"' in js
    # Reduced motion and coarse pointers opt out of the effects.
    assert "prefers-reduced-motion" in js and "pointer: fine" in js


def test_dashboard_is_installable() -> None:
    """A web manifest makes the shell installable without a build step."""
    manifest = json.loads(_read("assets/manifest.webmanifest"))
    assert manifest["name"] and manifest["short_name"]
    assert manifest["display"] == "standalone"
    assert manifest["icons"], "an installable app needs at least one icon"
    assert _read("index.html").count("assets/manifest.webmanifest") == 1
    # Identity and platform hints: without an id a reinstall can duplicate the app.
    assert manifest["id"] and manifest["start_url"] and manifest["scope"]
    assert manifest["display_override"], "window-controls-overlay is how it feels native on desktop"
    shortcuts = {item["name"] for item in manifest["shortcuts"]}
    assert {"Overview", "Projects", "Connectors"} <= shortcuts, "the app icon should offer shortcuts"


def test_dashboard_registers_a_service_worker() -> None:
    """The shell is installable and offline-capable, and app.js wires it up."""
    worker = WEB / "sw.js"
    assert worker.is_file(), "an installable app needs a service worker at /sw.js"
    source = _read("assets/app.js")
    assert 'navigator.serviceWorker.register("sw.js")' in source, "app.js should register the worker"
    assert "function initPwa(" in source and "initPwa();" in source, "boot should call initPwa"
    # The install path: prompt when offered, hint on iOS (which never offers one).
    assert "beforeinstallprompt" in source and "appinstalled" in source
    assert 'id="install-app"' in _read("index.html") and 'id="install-hint"' in _read("index.html")
    assert "display-mode: standalone" in source, "hide the prompt when already installed"


def test_service_worker_never_caches_the_api() -> None:
    """Offline is for the shell; operator data must always come from the network."""
    worker = _read("sw.js")
    assert "caches.open" in worker and "addAll" in worker, "the shell should be pre-cached"
    assert "skipWaiting" in worker and "clients.claim" in worker, "deploys should take over"
    # Cross-origin requests (the API you pointed it at) are never cached, and the
    # worker only answers for its own shell paths.
    assert "url.origin !== SCOPE.origin" in worker, "another origin must be left to the network"
    assert "SHELL_PATHS.has" in worker, "only shell files may be served from the cache"
    assert 'request.mode === "navigate"' in worker, "pages are network-first"
    # And it carries a cache version to bump, rather than guessing at staleness.
    assert re.search(r"const VERSION = \"v\d+\"", worker), "the cache needs a version"


def test_dashboard_install_prompt_is_quiet_by_default() -> None:
    """Installing is offered, never demanded: the button starts hidden."""
    html = _read("index.html")
    assert re.search(r'id="install-app"[^>]*hidden', html), "the install button starts hidden"
    assert "<style" not in html.lower(), "no inline styles, even for the button"


def test_dashboard_ships_no_trackers_or_remote_assets() -> None:
    """No analytics, no CDN, no third-party origin of any kind."""
    for name in ("index.html", "assets/styles.css", "assets/app.js", "sw.js"):
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
