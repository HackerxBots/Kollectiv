"""Validation for values that become part of a filesystem or storage path.

Project ids, account ids and file paths arrive from HTTP paths, query strings,
MCP tool calls, the CLI and webhook payloads. A value such as ``../../.ssh``
must never be joined onto a workspace directory or a bucket prefix, so every
such value goes through :func:`safe_path_segment` (one path component) or
:func:`safe_relative_path` (a nested path) before it is used.

Both helpers are deliberately strict and raise :class:`ValueError` with a
readable message: the API translates that into ``400`` (see the handler in
``src/api/routes.py``), the CLI prints it, and no code path ever silently
rewrites an identifier into a different one.
"""

from __future__ import annotations

import re

__all__ = ["MAX_RELATIVE_PATH_LENGTH", "MAX_SEGMENT_LENGTH", "safe_path_segment", "safe_relative_path"]

#: Longest accepted single path component (identifiers are short; URLs are not).
MAX_SEGMENT_LENGTH = 128
#: Longest accepted relative path, including separators.
MAX_RELATIVE_PATH_LENGTH = 1024

#: Characters that are separators, reserved or control characters on the
#: platforms Kollektiv runs on (POSIX and Windows).
_UNSAFE_CHARACTERS = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]")
#: A segment starts with a letter or digit and contains only safe characters,
#: which rules out ``.``, ``..``, ``.git``-style hidden names and trailing dots.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def safe_path_segment(value: str, *, label: str = "path segment", max_length: int = MAX_SEGMENT_LENGTH) -> str:
    """Validate one path component (a project id, an account id, a file name).

    Args:
        value: The untrusted value.
        label: What to call the value in error messages, e.g. ``"project id"``.
        max_length: Maximum accepted length in characters.

    Returns:
        The value, stripped of surrounding whitespace.

    Raises:
        ValueError: When the value is empty, too long, contains a path
            separator or control/reserved character, or is ``.``/``..``.
    """
    segment = (value or "").strip()
    if not segment:
        raise ValueError(f"{label} must not be empty")
    if len(segment) > max_length:
        raise ValueError(f"{label} is longer than {max_length} characters")
    if _UNSAFE_CHARACTERS.search(segment) or not _SAFE_SEGMENT.match(segment):
        raise ValueError(f"{label} contains characters that are not allowed in a path: {value!r}")
    return segment


def safe_relative_path(value: str, *, label: str = "path", max_length: int = MAX_RELATIVE_PATH_LENGTH) -> str:
    """Validate a nested relative path such as ``src/app/main.py``.

    Args:
        value: The untrusted path. Forward slashes separate the segments.
        label: What to call the path in error messages.
        max_length: Maximum accepted length in characters.

    Returns:
        The validated path, normalised to forward slashes.

    Raises:
        ValueError: When the path is empty, absolute, too long, contains a
            segment that is ``.`` or ``..``, contains a backslash or a control
            character, or has an empty segment (``a//b``).
    """
    text = (value or "").strip().replace("\\", "/")
    if not text:
        raise ValueError(f"{label} must not be empty")
    if text.startswith("/"):
        raise ValueError(f"{label} must be relative, not absolute: {value!r}")
    if len(text) > max_length:
        raise ValueError(f"{label} is longer than {max_length} characters")
    segments = text.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError(f"{label} contains an empty or traversal segment: {value!r}")
    return "/".join(safe_path_segment(segment, label=f"{label} segment") for segment in segments)
