"""Per-user remote folders: one folder per owner, then one per project."""

from __future__ import annotations

import pytest

from src.storage.user_paths import project_remote_base


def test_owner_gets_its_own_folder() -> None:
    assert project_remote_base("/Kollektiv", "prj_1", "user_abc") == "/Kollektiv/users/user_abc/prj_1"


def test_two_owners_never_share_a_folder() -> None:
    a = project_remote_base("/Kollektiv", "prj_1", "user_a")
    b = project_remote_base("/Kollektiv", "prj_1", "user_b")
    assert a != b
    assert not a.startswith(b + "/") and not b.startswith(a + "/")


def test_no_owner_keeps_the_flat_layout() -> None:
    assert project_remote_base("/Kollektiv/", "prj_1", None) == "/Kollektiv/prj_1"
    assert project_remote_base("/Kollektiv", "prj_1", "") == "/Kollektiv/prj_1"


def test_missing_project_falls_back_to_global() -> None:
    assert project_remote_base("/Kollektiv", "", "user_abc") == "/Kollektiv/users/user_abc/global"


@pytest.mark.parametrize("bad_owner", ["../other", "a/b", ".."])
def test_owner_ids_cannot_escape_the_user_folder(bad_owner: str) -> None:
    with pytest.raises(ValueError):
        project_remote_base("/Kollektiv", "prj_1", bad_owner)


def test_project_ids_cannot_escape_the_root() -> None:
    with pytest.raises(ValueError):
        project_remote_base("/Kollektiv", "../../etc", "user_abc")


class _RecordingPool:
    """Stands in for a storage pool: only the remote root matters here."""

    remote_root = "/Kollektiv"


def test_state_document_lands_in_the_owners_folder(tmp_path: object) -> None:
    from src.db.models import Project, session_scope
    from src.storage.state_manager import StateManager

    with session_scope() as session:
        session.add(Project(id="prj_owned", name="owned", owner_id="user_owner"))
        session.add(Project(id="prj_local", name="local", owner_id=None))

    manager = StateManager(_RecordingPool(), project_id="prj_owned", local_fallback_dir=str(tmp_path))
    assert manager.remote_path() == "/Kollektiv/users/user_owner/prj_owned/PROJECT_STATE.md"
    assert manager.project_folder() == "/Kollektiv/users/user_owner/prj_owned"

    flat = StateManager(_RecordingPool(), project_id="prj_local", local_fallback_dir=str(tmp_path))
    assert flat.remote_path() == "/Kollektiv/prj_local/PROJECT_STATE.md"
