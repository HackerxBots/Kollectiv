"""Shared project memory stored as ``PROJECT_STATE.md`` on TeraBox.

``PROJECT_STATE.md`` is the single source of truth every agent reads at the
start of a task and writes to when a task finishes. It is deliberately human
readable markdown: a GitHub webhook viewer, a TeraBox web UI or a human
operator can all read it, and it survives the loss of the local SQLite file.

Layout::

    ---                              <- YAML-ish front matter (JSON encoded)
    {"project_name": "...", ...}
    ---

    # PROJECT_STATE -- demo

    ## Tasks
    | id | title | status | agent | score |

    ## Agents
    | account | status | tasks_done |

    ## Files
    - `src/app.py` (1234 bytes)

    ## History
    - 2026-01-01T00:00:00+00:00 | agent-1 | task_completed | ...

The parser tolerates hand edits and missing sections; anything it cannot
parse is preserved in :attr:`StateManager.last_parse_errors` and in the
document's ``history`` so context is never silently dropped.

Usage::

    state = StateManager(pool)
    current = await state.read_state()
    current["tasks"].append({"id": "t1", "title": "Write the parser"})
    await state.write_state(current)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

from config.settings import PROJECT_STATE_FILENAME, Settings, get_settings
from src.storage.pool_manager import TeraBoxPoolManager
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)

#: Fields the state document always exposes, even when empty.
STATE_DEFAULTS: Dict[str, Any] = {
    "project_name": "",
    "project_id": "",
    "description": "",
    "tasks": [],
    "agents": [],
    "last_commit": "",
    "files": [],
    "history": [],
    "metadata": {},
}

_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class StateManager:
    """Read/write the shared ``PROJECT_STATE.md`` document.

    Args:
        pool: The TeraBox pool used for persistence.
        settings: Optional settings override.
        project_id: When set, the document lives at
            ``{TERABOX_REMOTE_ROOT}/{project_id}/PROJECT_STATE.md``.
        local_fallback_dir: Directory used to cache the document locally so a
            TeraBox outage degrades gracefully. Defaults to the workspace.
    """

    def __init__(
        self,
        pool: TeraBoxPoolManager,
        settings: Optional[Settings] = None,
        project_id: Optional[str] = None,
        local_fallback_dir: Optional[str] = None,
    ) -> None:
        self.pool = pool
        self.settings = settings or get_settings()
        self.project_id = project_id or ""
        self.last_parse_errors: List[str] = []
        self._cache: Optional[Dict[str, Any]] = None
        self._remote_path_cache: Optional[str] = None
        self._lock = asyncio.Lock()
        base_dir = local_fallback_dir or str(self.settings.workspace_path / "state")
        self._local_dir = base_dir
        os.makedirs(self._local_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------
    @property
    def filename(self) -> str:
        """File name of the state document."""
        return PROJECT_STATE_FILENAME

    def remote_path(self, project_id: Optional[str] = None) -> str:
        """Return the remote path of the state document.

        Args:
            project_id: Override the instance level project id.

        Returns:
            An absolute TeraBox path such as
            ``/Kollektiv/<project>/PROJECT_STATE.md``.
        """
        root = self.settings.TERABOX_REMOTE_ROOT.rstrip("/") or ""
        target = project_id if project_id is not None else self.project_id
        if target:
            return f"{root}/{target}/{self.filename}"
        return f"{root}/{self.filename}"

    def local_path(self, project_id: Optional[str] = None) -> str:
        """Return the local cache path of the state document."""
        target = project_id if project_id is not None else self.project_id
        name = f"{target or 'global'}-{self.filename}"
        return os.path.join(self._local_dir, name)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    async def read_state(self, project_id: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        """Load, parse and return the shared state.

        Args:
            project_id: Override the instance level project id.
            force: Ignore the in-process cache.

        Returns:
            The parsed state dict (missing sections filled with defaults).
            Never raises: an unreachable TeraBox falls back to the local
            cache and then to an empty-but-valid document.
        """
        use_cache = project_id is None or project_id == self.project_id
        if self._cache is not None and not force and use_cache:
            return self._cache

        remote = self.remote_path(project_id)
        local = self.local_path(project_id)
        text: Optional[str] = None

        try:
            text = await self.pool.read_text(remote)
        except Exception as exc:  # noqa: BLE001 - fall back to local cache
            LOGGER.warning("Could not read %s from TeraBox: %s", remote, exc)

        if not text and os.path.isfile(local):
            try:
                with open(local, encoding="utf-8") as handle:
                    text = handle.read()
                LOGGER.info("Using locally cached state for %s", remote)
            except OSError as exc:
                LOGGER.error("Local state cache unreadable: %s", exc)

        if not text:
            LOGGER.info("No existing state document at %s; starting a new one", remote)
            state = self._blank_state(project_id)
            self._cache = state
            self._remote_path_cache = remote
            return state

        state = self.parse_markdown(text)
        state.setdefault("project_id", project_id or self.project_id)
        self._write_local(local, text)
        self._cache = state
        self._remote_path_cache = remote
        return state

    def _pool_configured(self) -> bool:
        """Return ``True`` when the storage pool has at least one account."""
        checker = getattr(self.pool, "is_configured", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:  # noqa: BLE001 - treat a broken check as unconfigured
                return False
        return bool(getattr(self.pool, "accounts", None) or getattr(self.pool, "files", None))

    def _blank_state(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """Return a fresh state document."""
        state = json.loads(json.dumps(STATE_DEFAULTS))
        state["project_id"] = project_id or self.project_id
        state["updated_at"] = _now_iso()
        return state

    def _write_local(self, path: str, text: str) -> None:
        """Persist the raw document to the local cache (atomic write)."""
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=os.path.dirname(path) or ".", delete=False
            ) as handle:
                handle.write(text)
                temp_name = handle.name
            os.replace(temp_name, path)
        except OSError as exc:
            LOGGER.warning("Could not cache state locally at %s: %s", path, exc)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    async def write_state(self, state: Dict[str, Any], project_id: Optional[str] = None) -> bool:
        """Serialise ``state`` to markdown and upload it to TeraBox.

        Args:
            state: The state dict (mutated in place with ``updated_at``).
            project_id: Override the instance level project id.

        Returns:
            ``True`` when the document reached TeraBox. When TeraBox is
            unavailable the local cache is still updated and ``False`` is
            returned so callers can surface a degraded-mode warning.
        """
        document = dict(state)
        document["updated_at"] = _now_iso()
        markdown = self.render_markdown(document)
        remote = self.remote_path(project_id)
        local = self.local_path(project_id)

        self._write_local(local, markdown)
        self._cache = document
        self._remote_path_cache = remote

        async with self._lock:
            try:
                await self.pool.write_text(remote, markdown)
                LOGGER.debug("Wrote state document to %s (%s chars)", remote, len(markdown))
                return True
            except Exception as exc:  # noqa: BLE001 - degraded mode is expected
                if not self._pool_configured():
                    # No storage configured at all: the local cache is the
                    # state document by design, so this is not an error.
                    LOGGER.info(
                        "Shared storage is disabled; keeping %s in the local workspace", local
                    )
                else:
                    LOGGER.error("Could not upload the state document to %s: %s", remote, exc)
                return False

    async def append_event(
        self, event: Dict[str, Any], project_id: Optional[str] = None, persist: bool = True
    ) -> bool:
        """Append an event to the state history and persist the document.

        Args:
            event: Event payload; ``timestamp`` is added automatically when
                missing. Expected keys: ``agent_id``, ``action``, ``result``.
            project_id: Override the instance level project id.
            persist: When ``False`` only the in-memory cache is updated.

        Returns:
            ``True`` when the document was persisted to TeraBox.
        """
        state = await self.read_state(project_id)
        entry = {
            "timestamp": event.get("timestamp") or _now_iso(),
            "agent_id": str(event.get("agent_id", "")),
            "action": str(event.get("action", "")),
            "result": str(event.get("result", "")),
        }
        for key, value in event.items():
            if key not in entry:
                entry[key] = value

        history = list(state.get("history") or [])
        history.append(entry)
        limit = max(10, int(self.settings.STATE_HISTORY_LIMIT))
        if len(history) > limit:
            history = history[-limit:]
        state["history"] = history
        state["updated_at"] = _now_iso()

        if not persist:
            self._cache = state
            return False
        return await self.write_state(state, project_id)

    async def update_fields(self, project_id: Optional[str] = None, **fields: Any) -> bool:
        """Merge ``fields`` into the state document and persist it.

        Example::

            await state.update_fields(last_commit="abc123", project_name="demo")
        """
        state = await self.read_state(project_id)
        state.update(fields)
        return await self.write_state(state, project_id)

    # ------------------------------------------------------------------
    # Context for agents
    # ------------------------------------------------------------------
    async def get_latest_context(self, agent_id: str, max_history: int = 10) -> str:
        """Build the shared-context block injected into agent prompts.

        Args:
            agent_id: The agent requesting context (helps it ignore its own
                previous work).
            max_history: How many trailing history events to include.

        Returns:
            A formatted markdown block ready to paste into a prompt.
        """
        state = await self.read_state()
        tasks = state.get("tasks") or []
        history = (state.get("history") or [])[-max(0, max_history) :]
        files = state.get("files") or []

        lines: List[str] = ["## Shared project context", ""]
        lines.append(f"- Project: {state.get('project_name') or state.get('project_id') or 'unnamed'}")
        if state.get("description"):
            lines.append(f"- Goal: {str(state['description'])[:400]}")
        if state.get("last_commit"):
            lines.append(f"- Last commit: {state['last_commit']}")
        lines.append(f"- You are: {agent_id}")
        lines.append("")

        completed = [task for task in tasks if str(task.get("status")) == "completed"]
        pending = [task for task in tasks if str(task.get("status")) in {"pending", "in_progress", "failed"}]
        if completed:
            lines.append("### Already completed (do not redo)")
            for task in completed[-10:]:
                lines.append(
                    f"- `{task.get('id', '?')}` {task.get('title', '')} "
                    f"({task.get('assigned_agent') or task.get('agent') or 'unassigned'})"
                )
            lines.append("")
        if pending:
            lines.append("### Still outstanding")
            for task in pending[:10]:
                lines.append(
                    f"- `{task.get('id', '?')}` {task.get('title', '')} [{task.get('status', 'pending')}]"
                )
            lines.append("")

        if files:
            lines.append("### Files in the project")
            for entry in files[:40]:
                if isinstance(entry, dict):
                    size = entry.get("size")
                    suffix = f" ({size} bytes)" if size else ""
                    lines.append(f"- `{entry.get('path') or entry.get('name', '?')}`{suffix}")
                else:
                    lines.append(f"- `{entry}`")
            lines.append("")

        if history:
            lines.append(f"### Recent activity (last {len(history)})")
            for event in history:
                lines.append(
                    f"- {event.get('timestamp', '')} | {event.get('agent_id') or 'system'} | "
                    f"{event.get('action', '')} | {str(event.get('result', ''))[:160]}"
                )
            lines.append("")

        lines.append("_Work only on your assigned task, keep changes small, and report files you touched._")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Rendering / parsing
    # ------------------------------------------------------------------
    def render_markdown(self, state: Dict[str, Any]) -> str:
        """Render a state dict as the ``PROJECT_STATE.md`` document.

        Args:
            state: The state dict.

        Returns:
            A markdown document with JSON front matter.
        """
        front = {key: value for key, value in state.items() if key not in {"tasks", "agents", "files", "history"}}
        lines: List[str] = ["---"]
        lines.append(json.dumps(front, default=str, ensure_ascii=False, indent=2))
        lines.append("---")
        lines.append("")

        title = state.get("project_name") or state.get("project_id") or "unassigned"
        lines.append(f"# PROJECT_STATE -- {title}")
        lines.append("")
        lines.append("<!-- Generated by Kollektiv. Edit with care: unknown sections are preserved. -->")
        lines.append("")

        lines.append("## Tasks")
        lines.append("")
        lines.append("| id | title | status | assigned_agent | score |")
        lines.append("| --- | --- | --- | --- | --- |")
        for task in state.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            lines.append(
                "| {id} | {title} | {status} | {agent} | {score} |".format(
                    id=_cell(task.get("id")),
                    title=_cell(_truncate(task.get("title"), 80)),
                    status=_cell(task.get("status", "pending")),
                    agent=_cell(task.get("assigned_agent") or task.get("agent") or "-"),
                    score=_cell(task.get("score") if task.get("score") is not None else "-"),
                )
            )
        lines.append("")

        lines.append("## Agents")
        lines.append("")
        lines.append("| account_id | status | tasks_done |")
        lines.append("| --- | --- | --- |")
        for agent in state.get("agents") or []:
            if not isinstance(agent, dict):
                continue
            lines.append(
                "| {account} | {status} | {done} |".format(
                    account=_cell(agent.get("account_id") or agent.get("id") or agent.get("label")),
                    status=_cell(agent.get("status", "unknown")),
                    done=_cell(agent.get("tasks_done", 0)),
                )
            )
        lines.append("")

        lines.append("## Files")
        lines.append("")
        for entry in state.get("files") or []:
            if isinstance(entry, dict):
                size = entry.get("size")
                suffix = f" ({size} bytes)" if size else ""
                account = f" [account {entry['account_id']}]" if entry.get("account_id") else ""
                lines.append(f"- `{entry.get('path') or entry.get('name')}`{suffix}{account}")
            else:
                lines.append(f"- `{entry}`")
        lines.append("")

        lines.append("## History")
        lines.append("")
        for event in state.get("history") or []:
            if not isinstance(event, dict):
                lines.append(f"- {event}")
                continue
            lines.append(
                "- {ts} | {agent} | {action} | {result}".format(
                    ts=event.get("timestamp", ""),
                    agent=event.get("agent_id") or "system",
                    action=event.get("action", ""),
                    result=_truncate(_flatten(event.get("result", "")), 300),
                )
            )
        lines.append("")

        extra_keys = [
            key
            for key in state
            if key
            not in {
                "project_name",
                "project_id",
                "description",
                "last_commit",
                "updated_at",
                "metadata",
                "tasks",
                "agents",
                "files",
                "history",
            }
        ]
        if extra_keys:
            lines.append("## Extra")
            lines.append("")
            for key in extra_keys:
                lines.append(f"- **{key}**: {_truncate(_flatten(state[key]), 500)}")
            lines.append("")

        return "\n".join(lines)

    def parse_markdown(self, text: str) -> Dict[str, Any]:
        """Parse a ``PROJECT_STATE.md`` document back into a dict.

        Sections that cannot be parsed are reported in
        :attr:`last_parse_errors` and stored under ``metadata.unparsed`` so no
        information is lost.

        Args:
            text: Raw markdown.

        Returns:
            The parsed state dict.
        """
        self.last_parse_errors = []
        state: Dict[str, Any] = json.loads(json.dumps(STATE_DEFAULTS))

        match = _FRONT_MATTER_RE.match(text)
        if match:
            try:
                front = json.loads(match.group(1))
                if isinstance(front, dict):
                    state.update(front)
            except json.JSONDecodeError as exc:
                self.last_parse_errors.append(f"front matter: {exc}")
            text = text[match.end() :]
        else:
            self.last_parse_errors.append("front matter missing; using section parsing only")

        sections = _split_sections(text)
        state["tasks"] = _parse_table(sections.get("tasks", "")) or state.get("tasks") or []
        state["agents"] = _parse_table(sections.get("agents", "")) or state.get("agents") or []
        state["files"] = _parse_list(sections.get("files", "")) or state.get("files") or []
        state["history"] = _parse_history(sections.get("history", "")) or state.get("history") or []

        known_sections = {"_preamble", "tasks", "agents", "files", "history"}
        unknown = {
            name: body for name, body in sections.items() if name not in known_sections and body
        }
        if unknown:
            state.setdefault("metadata", {})
            if isinstance(state["metadata"], dict):
                state["metadata"]["unparsed_sections"] = unknown

        if self.last_parse_errors:
            LOGGER.warning("State document issues: %s", "; ".join(self.last_parse_errors))
        return state

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------
    def invalidate_cache(self) -> None:
        """Drop the in-process state cache so the next read hits storage."""
        self._cache = None

    async def export_to_path(self, local_path: str) -> str:
        """Render the current state to a local markdown file.

        Args:
            local_path: Destination file path.

        Returns:
            The path written.
        """
        state = await self.read_state()
        markdown = self.render_markdown(state)
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        with open(local_path, "w", encoding="utf-8") as handle:
            handle.write(markdown)
        return local_path

    async def archive_file(self, local_path: str, remote_subdir: str = "artifacts") -> Dict[str, Any]:
        """Upload an arbitrary local file into the project's storage folder.

        Args:
            local_path: Local file to archive.
            remote_subdir: Sub-folder under the project root.

        Returns:
            The pooled upload result.
        """
        root = self.settings.TERABOX_REMOTE_ROOT.rstrip("/")
        project = self.project_id or "global"
        name = os.path.basename(local_path)
        remote = f"{root}/{project}/{remote_subdir}/{name}"
        result = await self.pool.upload_file(local_path, remote)
        state = await self.read_state()
        files = [entry for entry in (state.get("files") or []) if isinstance(entry, dict)]
        files = [entry for entry in files if entry.get("path") != remote]
        files.append(
            {
                "path": remote,
                "name": name,
                "size": result.get("size", 0),
                "account_id": result.get("account_id", ""),
            }
        )
        state["files"] = files
        await self.write_state(state)
        return result


# ----------------------------------------------------------------------
# Markdown helpers
# ----------------------------------------------------------------------
def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _cell(value: Any) -> str:
    """Sanitise a value for a markdown table cell."""
    if value is None:
        return "-"
    return str(value).replace("|", "\\|").replace("\n", " ").strip() or "-"


def _truncate(value: Any, limit: int) -> str:
    """Truncate ``value`` to ``limit`` characters with an ellipsis."""
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _flatten(value: Any) -> str:
    """Render ``value`` as a single log friendly line."""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, default=str)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return str(value)
    return str(value)


def _split_sections(text: str) -> Dict[str, str]:
    """Split the document body into ``{section_name: body}`` (lower-cased names)."""
    sections: Dict[str, str] = {}
    current = "_preamble"
    buffer: List[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            sections[current] = "\n".join(buffer).strip()
            current = line[3:].strip().lower()
            buffer = []
            continue
        buffer.append(line)
    sections[current] = "\n".join(buffer).strip()
    return sections


def _parse_table(body: str) -> List[Dict[str, str]]:
    """Parse a markdown table body into a list of dicts.

    Escaped pipes (``\\|``) are treated as literal characters, not column
    separators, so titles containing ``|`` survive the round trip.
    """
    rows: List[Dict[str, str]] = []
    header: Optional[List[str]] = None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [
            cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip("|"))
        ]
        if header is None:
            header = cells
            continue
        if all(set(cell) <= {"-", ":", " "} and cell for cell in cells):
            continue
        if not any(cells):
            continue
        row = {header[index]: cells[index] for index in range(min(len(header), len(cells)))}
        rows.append(row)
    return rows


def _parse_list(body: str) -> List[Dict[str, Any]]:
    """Parse a bullet list of ``- `path` (size bytes) [account x]`` entries."""
    entries: List[Dict[str, Any]] = []
    pattern = re.compile(r"^-\s+`(?P<path>[^`]+)`\s*(?:\((?P<size>\d+) bytes\))?\s*(?:\[account (?P<account>[^\]]+)\])?")
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith("-"):
            continue
        match = pattern.match(line)
        if match:
            path = match.group("path")
            entries.append(
                {
                    "path": path,
                    "name": os.path.basename(path),
                    "size": int(match.group("size") or 0),
                    "account_id": match.group("account") or "",
                }
            )
        else:
            entries.append({"path": line.lstrip("- ").strip(), "name": "", "size": 0, "account_id": ""})
    return entries


def _parse_history(body: str) -> List[Dict[str, Any]]:
    """Parse the history bullet list back into structured events."""
    events: List[Dict[str, Any]] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith("-"):
            continue
        parts = [part.strip() for part in line.lstrip("- ").split("|")]
        if len(parts) >= 4:
            events.append(
                {
                    "timestamp": parts[0],
                    "agent_id": parts[1],
                    "action": parts[2],
                    "result": " | ".join(parts[3:]),
                }
            )
        else:
            events.append({"timestamp": "", "agent_id": "", "action": "note", "result": line.lstrip("- ")})
    return events


__all__ = ["StateManager", "STATE_DEFAULTS"]
