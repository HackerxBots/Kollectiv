"""Tests that keep the documentation navigable and the README a front page.

The README used to be the manual: 1 300 lines with everything in it. It is now a
front page (pitch, feature table, quickstart, links) and the depth lives in
``docs/``, in the style of large projects such as OpenHands. These tests make the
split a rule rather than a one-off edit:

* the README stays inside a line budget, so it cannot silently grow back;
* every local link in the README and in ``docs/*.md`` resolves to a file that
  exists — including ``file.md#anchor`` links, whose anchor must be a real
  heading after GitHub's slug rules;
* every page in ``docs/`` is reachable from the docs hub, so nothing is orphaned.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterator, List, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
DOCS = ROOT / "docs"

#: The README is a front page. 260 lines leaves room for a feature table and a
#: quickstart while staying scannable; depth belongs in ``docs/``.
README_LINE_BUDGET = 260

#: Files that exist in the tree but are not documentation pages.
DOC_EXCEPTIONS = {"README.md"}

_LINK = re.compile(r"\]\((?P<target>[^)\s]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(?P<title>.*?)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_CODE_SPAN = re.compile(r"`[^`]*`")
_IMAGE = re.compile(r"<img[^>]+src=\"(?P<src>[^\"]+)\"")


def headings_of(path: Path) -> List[str]:
    """Return GitHub slugs for the headings in a markdown file.

    Headings inside fenced code blocks are ignored (they are examples), and
    duplicate slugs get the ``-1``, ``-2`` … suffixes GitHub adds.

    Args:
        path: Markdown file to scan.

    Returns:
        The list of slugs, in document order.
    """
    slugs: List[str] = []
    seen: Dict[str, int] = {}
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING.match(line)
        if not match:
            continue
        slug = slugify(match.group("title"))
        if slug in seen:
            seen[slug] += 1
            slug = f"{slug}-{seen[slug]}"
        else:
            seen[slug] = 0
        slugs.append(slug)
    return slugs


def slugify(title: str) -> str:
    """Convert a heading to GitHub's anchor slug.

    Args:
        title: Raw heading text (may contain inline code, links, punctuation).

    Returns:
        The anchor GitHub generates: lowercase, punctuation removed, spaces to
        hyphens.
    """
    text = _CODE_SPAN.sub(lambda m: m.group(0).strip("`"), title)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [text](url) -> text
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text)


def links_in(path: Path) -> Iterator[Tuple[str, str]]:
    """Yield ``(target, raw)`` for every local link in a markdown file."""
    text = path.read_text(encoding="utf-8")
    for match in _LINK.finditer(text):
        target = match.group("target")
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        yield target, match.group(0)
    for match in _IMAGE.finditer(text):
        target = match.group("src")
        if not target.startswith(("http://", "https://", "data:")):
            yield target, match.group(0)


def check_links(path: Path) -> List[str]:
    """Return a list of broken-link descriptions for one markdown file."""
    problems: List[str] = []
    for target, raw in links_in(path):
        file_part, _, anchor = target.partition("#")
        if not file_part:  # same-page anchor
            if anchor and anchor not in headings_of(path):
                problems.append(f"{path.name}: dead anchor {raw}")
            continue
        resolved = (path.parent / file_part).resolve()
        if not resolved.exists():
            problems.append(f"{path.name}: missing file {raw}")
            continue
        if anchor and resolved.suffix == ".md" and anchor not in headings_of(resolved):
            problems.append(f"{path.name}: dead anchor {raw}")
    return problems


def test_readme_is_a_front_page() -> None:
    """The README stays short enough to read in one sitting."""
    lines = README.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= README_LINE_BUDGET, (
        f"README.md grew to {len(lines)} lines (budget {README_LINE_BUDGET}); "
        "move the depth into docs/ and link it"
    )
    # It must still be useful on its own: pitch, a quickstart and links.
    text = "\n".join(lines)
    assert "## Quickstart" in text
    assert "docs/README.md" in text
    assert "```bash" in text and "kollektiv serve-api" in text


def test_readme_and_docs_links_resolve() -> None:
    """Every local link and anchor in the README and docs/ points somewhere real."""
    problems = check_links(README)
    for page in sorted(DOCS.glob("*.md")):
        problems.extend(check_links(page))
    assert not problems, "broken documentation links:\n" + "\n".join(problems)


def test_docs_hub_lists_every_page() -> None:
    """The docs index mentions every page, and no page is orphaned."""
    hub = (DOCS / "README.md").read_text(encoding="utf-8")
    pages: Set[str] = {
        page.name for page in DOCS.glob("*.md") if page.name not in DOC_EXCEPTIONS | {"README.md"}
    }
    missing = sorted(name for name in pages if name not in hub)
    assert not missing, f"docs/README.md does not link: {missing}"


def test_split_kept_the_content() -> None:
    """The moved sections really landed in docs/ (spot checks, not vibes)."""
    expectations = {
        "docs/why-kollektiv.md": ["Why people run it", "What it is, and what it is not"],
        "docs/architecture.md": ["How a run works", "The shared state document", "Project layout"],
        "docs/configuration.md": ["Worker agents", "Shared storage", "Brain"],
        "docs/connectors.md": ["Connect your services", "Declaring your own connector", "dangerous"],
        "docs/deployment.md": ["Run it for free", "Self-hosting checklist", "Docker Compose"],
        "docs/faq.md": ["Troubleshooting", "FAQ"],
        "docs/privacy.md": ["no telemetry"],
        "docs/api.md": ["HTTP API", "MCP server", "CLI"],
    }
    for relative, needles in expectations.items():
        text = (ROOT / relative).read_text(encoding="utf-8")
        for needle in needles:
            assert needle.lower() in text.lower(), f"{relative} lost {needle!r}"

    # And the README no longer carries the long-form sections itself.
    readme = README.read_text(encoding="utf-8")
    for heading in ("## Configuration", "## Deployment", "## Project layout", "## FAQ"):
        assert heading not in readme, f"{heading} belongs in docs/"


def test_every_doc_links_back() -> None:
    """Each page says where it sits: a link to the hub and back to the README."""
    for page in sorted(DOCS.glob("*.md")):
        if page.name == "README.md":
            continue
        text = page.read_text(encoding="utf-8")
        assert "](../README.md)" in text, f"{page.name} does not link back to the README"
        assert "[Kollektiv documentation](README.md)" in text, f"{page.name} does not link the hub"
