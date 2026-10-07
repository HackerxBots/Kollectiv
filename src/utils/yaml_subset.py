"""A strict YAML subset reader, so ``.kollektiv.yml`` works with zero dependencies.

Kollektiv's only config file is small and deliberate: mappings, lists and
scalars, with comments. PyYAML parses it fine — and when PyYAML is installed,
:mod:`src.utils.project_config` uses it and this module never runs. This reader
exists for the other case, because "install a YAML library to set your agent
count" is a bad trade and a half-parser that *guesses* is worse than one that
refuses.

What it supports:

```yaml
# comments, blank lines
key: value
number: 3          # int
ratio: 0.8         # float
flag: true         # bool (true/false, yes/no, on/off)
quoted: "text"     # single or double quotes, with \\" and \\n escapes in double
empty:
list: [a, b, 3]    # inline list
nested:
  child: 1
  deeper:
    leaf: x
block:
  - one
  - two
```

What it refuses (with the offending line number, never a guess): tabs for
indentation, anchors and aliases (``&``/``*``), tags (``!``), multi-line block
scalars (``|``/``>``), documents (``---``/``...``), flow mappings (``{a: 1}``),
duplicate keys, and any line it cannot classify. Refusing loudly is the whole
point: a config file that silently parsed into the wrong shape would be a bug
nobody finds until a run costs money.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from src.utils.errors import ConfigurationError

#: Values that mean "true" / "false" when unquoted, as in YAML 1.1.
TRUE_VALUES = {"true", "yes", "on"}
FALSE_VALUES = {"false", "no", "off"}
#: Values that mean "nothing", as in YAML.
NULL_VALUES = {"null", "~", ""}
#: Characters that would make a scalar mean something else entirely.
UNSUPPORTED_PREFIXES = ("&", "*", "!", "|", ">", "---", "...")


class _Line:
    """One classified line of the document."""

    def __init__(self, number: int, indent: int, content: str, is_item: bool = False) -> None:
        """Store the line.

        Args:
            number: 1-based line number.
            indent: Number of leading spaces.
            content: The text after the indentation (and after ``- `` for items).
            is_item: Whether the line started with ``- ``.
        """
        self.number = number
        self.indent = indent
        self.content = content
        self.is_item = is_item


def loads(text: str, source: str = "<yaml>") -> Any:
    """Parse a YAML subset document.

    Args:
        text: The document.
        source: Name used in error messages.

    Returns:
        Python mappings, lists and scalars. An empty document returns ``{}``.

    Raises:
        ConfigurationError: For any construct outside the supported subset.
    """
    lines = _classify(text, source)
    if not lines:
        return {}
    value, index = _parse_block(lines, 0, lines[0].indent, source)
    if index != len(lines):  # pragma: no cover - defensive
        raise ConfigurationError(f"{source}: line {lines[index].number}: unexpected indentation")
    return value


def _classify(text: str, source: str) -> List[_Line]:
    """Strip comments and blanks and record indentation.

    Args:
        text: The document.
        source: Name used in error messages.

    Returns:
        The significant lines, in order.

    Raises:
        ConfigurationError: For tabs, document markers or unsupported scalars.
    """
    result: List[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
            raise ConfigurationError(f"{source}: line {number}: tabs are not valid indentation, use spaces")
        stripped = _strip_comment(raw).rstrip()
        if not stripped.strip():
            continue
        if stripped.strip() in ("---", "...") or stripped.strip().startswith("---"):
            raise ConfigurationError(f"{source}: line {number}: multi-document files are not supported")
        indent = len(stripped) - len(stripped.lstrip(" "))
        content = stripped.strip()
        is_item = content.startswith("- ") or content == "-"
        if is_item:
            content = content[2:].strip() if content.startswith("- ") else ""
        if content.startswith(UNSUPPORTED_PREFIXES):
            raise ConfigurationError(
                f"{source}: line {number}: anchors, aliases, tags and block scalars are not supported; "
                "install pyyaml for full YAML"
            )
        result.append(_Line(number, indent, content, is_item))
    return result


def _strip_comment(raw: str) -> str:
    """Remove a trailing ``# comment`` that is not inside quotes.

    Args:
        raw: The raw line.

    Returns:
        The line without its comment.
    """
    quote: Optional[str] = None
    for index, char in enumerate(raw):
        if quote:
            if char == quote and raw[index - 1 : index] != "\\":
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "#" and (index == 0 or raw[index - 1] in " \t"):
            return raw[:index]
    return raw


def _parse_block(lines: List[_Line], index: int, indent: int, source: str) -> Tuple[Any, int]:
    """Parse a mapping or a list at ``indent``.

    Args:
        lines: Classified lines.
        index: Position to start at.
        indent: The indentation this block is expected at.
        source: Name used in error messages.

    Returns:
        ``(value, next_index)``.

    Raises:
        ConfigurationError: On malformed structure.
    """
    if lines[index].is_item:
        return _parse_list(lines, index, indent, source)
    return _parse_mapping(lines, index, indent, source)


def _parse_mapping(lines: List[_Line], index: int, indent: int, source: str) -> Tuple[Dict[str, Any], int]:
    """Parse ``key: value`` lines (and their nested blocks).

    Args:
        lines: Classified lines.
        index: Position to start at.
        indent: Expected indentation.
        source: Name used in error messages.

    Returns:
        ``(mapping, next_index)``.

    Raises:
        ConfigurationError: On a missing colon, a deeper indent or a duplicate key.
    """
    result: Dict[str, Any] = {}
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise ConfigurationError(f"{source}: line {line.number}: unexpected indentation")
        if line.is_item:
            break
        if ":" not in line.content:
            raise ConfigurationError(f"{source}: line {line.number}: expected 'key: value'")
        key, _, rest = line.content.partition(":")
        key = key.strip()
        if not key:
            raise ConfigurationError(f"{source}: line {line.number}: empty key")
        if key in result:
            raise ConfigurationError(f"{source}: line {line.number}: duplicate key {key!r}")
        rest = rest.strip()
        index += 1
        if rest:
            result[key] = _scalar(rest, line.number, source)
            continue
        if index < len(lines) and lines[index].indent > indent:
            value, index = _parse_block(lines, index, lines[index].indent, source)
            result[key] = value
        else:
            result[key] = None
    return result, index


def _parse_list(lines: List[_Line], index: int, indent: int, source: str) -> Tuple[List[Any], int]:
    """Parse ``- item`` lines.

    Args:
        lines: Classified lines.
        index: Position to start at.
        indent: Expected indentation of the dashes.
        source: Name used in error messages.

    Returns:
        ``(list, next_index)``.

    Raises:
        ConfigurationError: On a malformed item.
    """
    result: List[Any] = []
    while index < len(lines):
        line = lines[index]
        if line.indent < indent or not line.is_item:
            break
        if line.indent > indent:
            raise ConfigurationError(f"{source}: line {line.number}: unexpected indentation in a list")
        if not line.content:
            # A bare dash starts a nested block on the following lines.
            if index + 1 < len(lines) and lines[index + 1].indent > indent:
                value, index = _parse_block(lines, index + 1, lines[index + 1].indent, source)
                result.append(value)
                continue
            result.append(None)
            index += 1
            continue
        if ":" in line.content and not line.content.startswith(("'", '"')):
            # "- key: value" starts an inline mapping item.
            nested: Dict[str, Any] = {}
            head, _, rest = line.content.partition(":")
            nested[head.strip()] = _scalar(rest.strip(), line.number, source) if rest.strip() else None
            index += 1
            while index < len(lines) and lines[index].indent > indent and not lines[index].is_item:
                extra_line = lines[index]
                if ":" not in extra_line.content:
                    raise ConfigurationError(f"{source}: line {extra_line.number}: expected 'key: value'")
                child_key, _, child_rest = extra_line.content.partition(":")
                if child_key.strip() in nested:
                    raise ConfigurationError(f"{source}: line {extra_line.number}: duplicate key {child_key.strip()!r}")
                nested[child_key.strip()] = _scalar(child_rest.strip(), extra_line.number, source) if child_rest.strip() else None
                index += 1
            result.append(nested)
            continue
        result.append(_scalar(line.content, line.number, source))
        index += 1
    return result, index


def _scalar(text: str, number: int, source: str) -> Any:
    """Convert one scalar token to a Python value.

    Args:
        text: The token.
        number: Line number for messages.
        source: Name used in error messages.

    Returns:
        ``str``, ``int``, ``float``, ``bool`` or ``None``.

    Raises:
        ConfigurationError: For flow mappings, which would need a real parser.
    """
    token = text.strip()
    if token.startswith("{"):
        raise ConfigurationError(
            f"{source}: line {number}: flow mappings are not supported; install pyyaml for full YAML"
        )
    if token.startswith(UNSUPPORTED_PREFIXES):
        # ``a: &anchor`` or ``a: |`` would otherwise be read as the literal text
        # "&anchor" / "|", which is exactly the silent wrong answer this module
        # exists to refuse.
        raise ConfigurationError(
            f"{source}: line {number}: anchors, aliases, tags and block scalars are not supported; "
            "install pyyaml for full YAML"
        )
    if token.startswith("["):
        if not token.endswith("]"):
            raise ConfigurationError(f"{source}: line {number}: unterminated inline list")
        inner = token[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part, number, source) for part in _split_inline(inner)]
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return _unquote(token)
    lowered = token.lower()
    if lowered in NULL_VALUES:
        return None
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return token


def _split_inline(inner: str) -> List[str]:
    """Split an inline list on commas that are not inside quotes.

    Args:
        inner: The text between the brackets.

    Returns:
        The parts, stripped.
    """
    parts: List[str] = []
    current = ""
    quote: Optional[str] = None
    for char in inner:
        if quote:
            current += char
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            current += char
        elif char == ",":
            parts.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        parts.append(current.strip())
    return parts


def _unquote(token: str) -> str:
    """Remove quotes and resolve the escapes the quoted form allows.

    Args:
        token: A quoted scalar.

    Returns:
        The unquoted text.
    """
    body = token[1:-1]
    if token[0] == "'":
        return body.replace("''", "'")
    return body.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")


__all__ = ["FALSE_VALUES", "NULL_VALUES", "TRUE_VALUES", "loads"]
