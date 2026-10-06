"""Tests for :mod:`src.storage.state_manager`.

``PROJECT_STATE.md`` is the shared memory every agent reads and writes, so the
round trip (dict -> markdown -> dict), the local fallback when TeraBox is
unreachable and the agent context builder all need to be reliable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from src.storage.state_manager import StateManager
from tests.conftest import FakeTeraBoxPool


@pytest.fixture()
def pool() -> FakeTeraBoxPool:
    """An in-memory storage pool."""
    return FakeTeraBoxPool()


@pytest.fixture()
def state(pool: FakeTeraBoxPool, settings: Any, tmp_path: Path) -> StateManager:
    """A state manager bound to the fake pool and a temp cache directory."""
    return StateManager(pool, settings=settings, project_id="prj_1", local_fallback_dir=str(tmp_path / "state"))


async def test_blank_state_read_from_missing_document(state: StateManager) -> None:
    """A missing document yields a valid empty state."""
    document = await state.read_state()
    assert document["project_id"] == "prj_1"
    assert document["tasks"] == [] and document["files"] == [] and document["history"] == []


async def test_write_then_read_roundtrip(state: StateManager, pool: FakeTeraBoxPool) -> None:
    """Everything written survives a render/parse cycle."""
    document = await state.read_state()
    document.update(
        {
            "project_name": "shortener",
            "description": "Build a URL shortener",
            "last_commit": "abc1234",
            "tasks": [
                {"id": "t1", "title": "Core | tables", "status": "completed", "assigned_agent": "agent-1", "score": 0.9},
                {"id": "t2", "title": "API", "status": "pending", "dependencies": ["t1"]},
            ],
            "agents": [{"account_id": "agent-1", "status": "idle", "tasks_done": 3}],
            "files": [{"path": "src/app.py", "name": "app.py", "size": 120, "account_id": "acct-1"}],
        }
    )
    assert await state.write_state(document) is True
    assert "/Kollektiv/prj_1/PROJECT_STATE.md" in pool.files
    assert "PROJECT_STATE -- shortener" in pool.files["/Kollektiv/prj_1/PROJECT_STATE.md"]

    state.invalidate_cache()
    reloaded = await state.read_state(force=True)
    assert reloaded["project_name"] == "shortener"
    assert reloaded["last_commit"] == "abc1234"
    assert [task["id"] for task in reloaded["tasks"]] == ["t1", "t2"]
    assert reloaded["tasks"][0]["status"] == "completed"
    # A pipe inside a title survives the markdown round trip.
    assert reloaded["tasks"][0]["title"] == "Core | tables"
    assert reloaded["files"][0]["path"] == "src/app.py"
    assert reloaded["files"][0]["size"] == 120
    assert reloaded["agents"][0]["account_id"] == "agent-1"


async def test_append_event_trims_history(state: StateManager, settings: Any) -> None:
    """History is capped at ``STATE_HISTORY_LIMIT`` entries."""
    small = settings.model_copy(update={"STATE_HISTORY_LIMIT": 10}, deep=True)
    manager = StateManager(
        state.pool, settings=small, project_id="prj_1", local_fallback_dir=state._local_dir  # noqa: SLF001
    )
    for index in range(15):
        await manager.append_event({"agent_id": f"agent-{index}", "action": "task_completed", "result": f"task {index}"})
    document = await manager.read_state()
    assert len(document["history"]) == 10
    assert document["history"][-1]["agent_id"] == "agent-14"
    assert document["history"][-1]["timestamp"]


async def test_append_event_without_persist(state: StateManager) -> None:
    """``persist=False`` keeps the event in memory only."""
    assert await state.append_event({"action": "note"}, persist=False) is False
    document = await state.read_state()
    assert document["history"][-1]["action"] == "note"


async def test_state_survives_storage_outage(pool: FakeTeraBoxPool, settings: Any, tmp_path: Path) -> None:
    """A TeraBox outage is survived using the local cache."""
    manager = StateManager(pool, settings=settings, project_id="prj_2", local_fallback_dir=str(tmp_path / "cache"))
    document = await manager.read_state()
    document["project_name"] = "resilient"
    assert await manager.write_state(document) is True

    async def broken_read(remote_path: str) -> str:
        raise RuntimeError("storage down")

    async def broken_write(remote_path: str, content: str) -> Dict[str, Any]:
        raise RuntimeError("storage down")

    pool.read_text = broken_read  # type: ignore[assignment]
    pool.write_text = broken_write  # type: ignore[assignment]

    manager.invalidate_cache()
    recovered = await manager.read_state(force=True)
    assert recovered["project_name"] == "resilient"
    assert await manager.update_fields(project_name="still resilient") is False  # degraded, not crashed
    assert (await manager.read_state(force=True))["project_name"] == "still resilient"


async def test_get_latest_context_includes_everything(state: StateManager) -> None:
    """The agent context summarises tasks, files, commits and history."""
    document = await state.read_state()
    document.update(
        {
            "project_name": "demo",
            "description": "Build a demo",
            "last_commit": "deadbee",
            "tasks": [
                {"id": "t1", "title": "Core", "status": "completed", "assigned_agent": "agent-1"},
                {"id": "t2", "title": "API", "status": "pending"},
            ],
            "files": [{"path": "src/core.py", "size": 10}],
        }
    )
    await state.write_state(document)
    await state.append_event({"agent_id": "agent-1", "action": "task_completed", "result": "Core done"})

    context = await state.get_latest_context("agent-2")
    assert "## Shared project context" in context
    assert "- Project: demo" in context
    assert "Last commit: deadbee" in context
    assert "You are: agent-2" in context
    assert "### Already completed (do not redo)" in context
    assert "### Still outstanding" in context
    assert "`src/core.py`" in context
    assert "task_completed" in context


async def test_archive_file_registers_in_state(state: StateManager, tmp_path: Path) -> None:
    """Archiving a file uploads it and adds it to the file inventory."""
    local = tmp_path / "artifact.zip"
    local.write_text("zip-bytes", encoding="utf-8")
    result = await state.archive_file(str(local))
    assert result["path"].endswith("artifacts/artifact.zip")

    document = await state.read_state()
    assert any(entry["path"].endswith("artifact.zip") for entry in document["files"])


async def test_export_to_path_writes_markdown(state: StateManager, tmp_path: Path) -> None:
    """The rendered document can be exported for humans."""
    document = await state.read_state()
    document["project_name"] = "exported"
    await state.write_state(document)

    target = tmp_path / "out" / "PROJECT_STATE.md"
    written = await state.export_to_path(str(target))
    text = Path(written).read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "# PROJECT_STATE -- exported" in text
    assert "## Tasks" in text


async def test_parse_handles_hand_edits(state: StateManager) -> None:
    """A hand-edited document still parses, and problems are reported."""
    document = await state.read_state()
    document["project_name"] = "hand edited"
    await state.write_state(document)
    raw = "# PROJECT_STATE -- hand edited\n\n## Tasks\n\n| id | title | status |\n| --- | --- | --- |\n| t9 | Manual | pending |\n\n## Unknown\n\n- something\n"
    parsed = state.parse_markdown(raw)
    assert parsed["tasks"][0]["id"] == "t9"
    assert state.last_parse_errors  # the missing front matter is reported
    # Unknown sections are preserved rather than silently dropped.
    assert "unknown" in parsed["metadata"]["unparsed_sections"]
