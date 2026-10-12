"""Remote folder layout: one folder per user, then one per project.

    {remote_root}/users/{owner_id}/{project_id}/...

Projects made without an owner (single-user or auth-off use) keep the flat
layout ``{remote_root}/{project_id}/...`` so existing local setups do not move.
Old files stay where they were uploaded; only new writes use the new layout.
"""

from __future__ import annotations

from typing import Optional

from src.utils.paths import safe_path_segment

USERS_DIR = "users"


def project_remote_base(root: str, project_id: str, owner_id: Optional[str] = None) -> str:
    """Return the remote folder that holds one project's files.

    Args:
        root: The remote root (``TERABOX_REMOTE_ROOT``, the R2 root or the pool's root).
        project_id: The project id; ``"global"`` for work outside a project.
        owner_id: The Clerk user id that owns the project, if any.

    Returns:
        ``{root}/users/{owner}/{project}`` when there is an owner, else ``{root}/{project}``.

    Raises:
        ValueError: When an id is empty or contains a path separator.
    """
    base = (root or "").rstrip("/")
    project = safe_path_segment(project_id or "global", label="project id")
    if owner_id:
        owner = safe_path_segment(owner_id, label="owner id")
        return f"{base}/{USERS_DIR}/{owner}/{project}"
    return f"{base}/{project}"


def owner_of_project(project_id: Optional[str]) -> Optional[str]:
    """Return the owner id stored on a project, or ``None`` if it has none or does not exist."""
    if not project_id:
        return None
    from src.db.models import Project, session_scope

    with session_scope() as session:
        row = session.get(Project, project_id)
        return row.owner_id if row is not None else None
