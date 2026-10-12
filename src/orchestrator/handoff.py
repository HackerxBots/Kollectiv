"""Session handoff: turn a project's shared state into a briefing you can resume from.

The problem this solves: an agent session (an Arena chat, a colleague, a CI run)
ends, and the next one starts from nothing — exactly when the interesting work
is half done. ``PROJECT_STATE.md`` already holds the plan, task status, files and
history, so this module renders that state as a compact, pasteable briefing with
the next concrete actions at the top.

Three shapes are produced from the same data:

* :func:`build_handoff` — a structured dict (used by the HTTP API and MCP).
* :func:`render_markdown` — the briefing (``HANDOFF.md``, printed by the CLI).
* the ``next_actions`` list — literally what to do next, ordered by the
  dependency graph so a fresh session can pick up without re-planning.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

#: How many history entries, files and commits a briefing carries.
HISTORY_LIMIT = 15
FILE_LIMIT = 40
COMMIT_LIMIT = 8
BLOCKER_LIMIT = 10

_TERMINAL = {"completed", "cancelled"}


def _task_list(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the state's tasks as a list of dicts."""
    return [task for task in (state.get("tasks") or []) if isinstance(task, dict)]


def _dependencies(task: Dict[str, Any]) -> List[str]:
    """Return a task's dependencies as a list of ids."""
    deps = task.get("depends_on") or task.get("dependencies") or []
    if isinstance(deps, str):
        return [part.strip() for part in deps.replace(",", " ").split() if part.strip()]
    return [str(dep) for dep in deps]


def next_actions(state: Dict[str, Any], limit: int = 8) -> List[Dict[str, Any]]:
    """Return the tasks a fresh session should pick up first.

    Ordering is dependency-aware: a task whose dependencies are all completed
    comes first, and blocked tasks are reported with the dependency that blocks
    them instead of being suggested.

    Args:
        state: The shared state dict (``tasks`` is what matters here).
        limit: Maximum number of actions to return.

    Returns:
        ``[{id, title, status, reason, assigned_agent, depends_on}]``.
    """
    tasks = _task_list(state)
    status = {str(task.get("id", "")): str(task.get("status") or "pending") for task in tasks}
    actions: List[Dict[str, Any]] = []

    def blocked_by(task: Dict[str, Any]) -> List[str]:
        return [dep for dep in _dependencies(task) if status.get(dep, "completed") not in _TERMINAL]

    # 1. Work already in flight (or failed) comes first: resuming beats starting.
    for wanted, reason in ((("in_progress",), "already in progress"), (("failed",), "failed — retry or replan")):
        for task in tasks:
            if task.get("status") in wanted:
                actions.append(
                    {
                        "id": task.get("id"),
                        "title": task.get("title"),
                        "status": task.get("status"),
                        "reason": reason,
                        "assigned_agent": task.get("assigned_agent"),
                        "depends_on": _dependencies(task),
                    }
                )
    # 2. Pending tasks whose dependencies are satisfied.
    for task in tasks:
        if task.get("status") not in (None, "pending"):
            continue
        blockers = blocked_by(task)
        if blockers:
            continue
        actions.append(
            {
                "id": task.get("id"),
                "title": task.get("title"),
                "status": task.get("status") or "pending",
                "reason": "ready to start",
                "assigned_agent": task.get("assigned_agent"),
                "depends_on": _dependencies(task),
            }
        )
    # 3. Blocked work, so the reader knows what is waiting on what.
    for task in tasks:
        if task.get("status") not in (None, "pending"):
            continue
        blockers = blocked_by(task)
        if not blockers:
            continue
        actions.append(
            {
                "id": task.get("id"),
                "title": task.get("title"),
                "status": "blocked",
                "reason": f"waiting on {', '.join(blockers)}",
                "assigned_agent": task.get("assigned_agent"),
                "depends_on": _dependencies(task),
            }
        )
    return actions[:limit]


def blockers(state: Dict[str, Any]) -> List[str]:
    """Return the human-readable list of things stopping the project finishing."""
    tasks = _task_list(state)
    found: List[str] = []
    for task in tasks:
        status = str(task.get("status") or "pending")
        if status == "failed":
            found.append(f"task {task.get('id')} ({task.get('title')}) failed: {task.get('error') or 'no error recorded'}")
        elif status == "blocked":
            found.append(f"task {task.get('id')} ({task.get('title')}) is blocked")
    if not tasks:
        found.append("no plan recorded for this project yet")
    return found[:BLOCKER_LIMIT]


def build_handoff(
    state: Dict[str, Any],
    project_id: str,
    settings: Optional[Any] = None,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the structured handoff for a project.

    Args:
        state: The shared state dict (from ``StateManager.read_state``).
        project_id: The project identifier.
        settings: Optional settings, used for the app name and dashboard URL.
        generated_at: Optional ISO timestamp override (tests, deterministic output).

    Returns:
        A JSON-serialisable briefing: counters, next actions, blockers, recent
        history, files and commits, plus the rendered markdown.
    """
    tasks = _task_list(state)
    completed = [task for task in tasks if str(task.get("status")) == "completed"]
    failed = [task for task in tasks if str(task.get("status")) == "failed"]
    pending = [task for task in tasks if str(task.get("status") or "pending") == "pending"]
    in_progress = [task for task in tasks if str(task.get("status")) == "in_progress"]
    history = list(state.get("history") or [])[-HISTORY_LIMIT:]
    files = [str(name) for name in (state.get("files") or [])][:FILE_LIMIT]
    commits = list(state.get("recent_commits") or [])[:COMMIT_LIMIT]

    total = len(tasks)
    percent = round(100 * len(completed) / total) if total else 0
    app_name = getattr(settings, "APP_NAME", "Kollektiv")
    base_url = (getattr(settings, "APP_BASE_URL", "") or "").rstrip("/")

    handoff: Dict[str, Any] = {
        "project_id": project_id,
        "project_name": state.get("project_name") or project_id,
        "description": state.get("description", ""),
        "status": state.get("status", "planned"),
        "revision": state.get("revision"),
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "progress": {
            "total": total,
            "completed": len(completed),
            "failed": len(failed),
            "in_progress": len(in_progress),
            "pending": len(pending),
            "percent": percent,
        },
        "next_actions": next_actions(state),
        "blockers": blockers(state),
        "last_commit": state.get("last_commit", ""),
        "recent_commits": commits,
        "files": files,
        "history": history,
        "queued_tasks": len(pending),
        "dashboard": {
            "app": app_name,
            "url": f"{base_url}/ui" if base_url else "/ui",
            "api": f"{base_url}/projects/{project_id}/status" if base_url else None,
        },
    }
    handoff["markdown"] = render_markdown(handoff)
    return handoff


def render_markdown(handoff: Dict[str, Any]) -> str:
    """Render a handoff dict as the briefing (Markdown) a session can paste."""
    progress = handoff.get("progress") or {}
    lines: List[str] = [
        f"# Handoff — {handoff.get('project_name')}",
        "",
        f"> {handoff.get('description') or 'No description recorded.'}",
        "",
        f"- Project: `{handoff.get('project_id')}`",
        f"- Status: **{handoff.get('status')}** · {progress.get('completed', 0)}/{progress.get('total', 0)} "
        f"tasks completed ({progress.get('percent', 0)}%)",
        f"- Generated: {handoff.get('generated_at')}",
    ]
    if handoff.get("revision"):
        lines.append(f"- Plan revision: {handoff.get('revision')}")
    if handoff.get("last_commit"):
        lines.append(f"- Last commit: `{handoff.get('last_commit')}`")
    lines.append("")

    lines.append("## Next actions")
    actions = handoff.get("next_actions") or []
    if actions:
        for index, action in enumerate(actions, start=1):
            lines.append(
                f"{index}. **{action.get('id')} — {action.get('title')}** "
                f"({action.get('reason')}; assigned: {action.get('assigned_agent') or 'unassigned'})"
            )
    else:
        lines.append("_Nothing actionable: every task is completed or cancelled._")
    lines.append("")

    if handoff.get("blockers"):
        lines.append("## Blockers")
        lines.extend(f"- {item}" for item in handoff["blockers"])
        lines.append("")

    if handoff.get("recent_commits"):
        lines.append("## Recent commits")
        for commit in handoff["recent_commits"]:
            lines.append(f"- `{commit.get('sha')}` {commit.get('message')} — {commit.get('author', '')}")
        lines.append("")

    if handoff.get("files"):
        lines.append("## Files in the project")
        for name in handoff["files"]:
            lines.append(f"- `{name}`")
        lines.append("")

    if handoff.get("history"):
        lines.append("## Recent history")
        for event in handoff["history"]:
            lines.append(
                f"- {event.get('timestamp', '')} · {event.get('agent_id', '?')} · "
                f"{event.get('action', '')} → {event.get('result', '')}"
            )
        lines.append("")

    lines.append("## How to continue")
    lines.append("")
    lines.append("```bash")
    lines.append(f"kollektiv status --project-id {handoff.get('project_id')}   # full state document")
    lines.append(f"kollektiv run --project-id {handoff.get('project_id')}      # continue / resume the run")
    lines.append(f"kollektiv resume --project-id {handoff.get('project_id')}   # reprint this briefing")
    lines.append("```")
    lines.append("")
    lines.append(
        "_This briefing is generated from the shared state document "
        "(`PROJECT_STATE.md`), so a new session never has to start from scratch._"
    )
    return "\n".join(lines)


def write_handoff_file(handoff: Dict[str, Any], directory: Any) -> str:
    """Write ``HANDOFF.md`` next to the project's workspace and return its path.

    Args:
        handoff: The result of :func:`build_handoff`.
        directory: Directory to write into (created when missing).

    Returns:
        The absolute path of the written file.
    """
    from pathlib import Path

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / "HANDOFF.md"
    path.write_text(str(handoff.get("markdown") or ""), encoding="utf-8")
    return str(path)


def age_seconds(handoff: Dict[str, Any]) -> Optional[float]:
    """Return how long ago the briefing was generated (tests/UI freshness)."""
    stamp = handoff.get("generated_at")
    if not stamp:
        return None
    try:
        generated = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return round(time.time() - generated.timestamp(), 3)


__all__ = [
    "HANDOFF_FILENAME",
    "age_seconds",
    "blockers",
    "build_handoff",
    "next_actions",
    "render_markdown",
    "write_handoff_file",
]

#: Filename written into a project workspace.
HANDOFF_FILENAME = "HANDOFF.md"
