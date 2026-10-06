"""Tests for the orchestration layer.

Covers the brain (planning, review, context), the planner (dependency graph,
waves, replanning), the collector (parsing, path safety, merging) and the
dispatcher (ordering, blocked tasks, retries) plus a full project run through
:class:`src.orchestrator.app.Orchestrator` with fake subsystems.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from config.settings import Settings
from src.github.github_client import GitHubClient
from src.orchestrator.app import Orchestrator
from src.orchestrator.brain import OrchestratorBrain
from src.orchestrator.collector import Collector
from src.orchestrator.dispatcher import Dispatcher
from src.orchestrator.planner import Planner
from src.storage.state_manager import StateManager
from tests.conftest import (
    FakeAgent,
    FakeAgentPool,
    FakeOpenAIClient,
    FakeTeraBoxPool,
)


# ----------------------------------------------------------------------
# Brain
# ----------------------------------------------------------------------
def brain_with(settings: Settings, responses: List[str], responder: Any = None) -> OrchestratorBrain:
    """Build a brain whose LLM client returns the scripted responses.

    Args:
        settings: Base settings.
        responses: Scripted answers, consumed in order.
        responder: Optional ``kwargs -> str`` fallback used once the scripted
            answers run out (handy when the same brain also serves tactical
            advice prompts).
    """
    fake = FakeOpenAIClient(responses, responder=responder)
    configured = settings.model_copy(update={"BRAIN_API_KEY": "test-key"}, deep=True)
    return OrchestratorBrain(configured, client_factory=lambda key, url, cfg: fake)


async def test_split_task_parses_llm_plan(settings: Any) -> None:
    """A well-formed LLM answer becomes normalised subtasks."""
    payload = json.dumps(
        {
            "subtasks": [
                {
                    "id": "t1",
                    "title": "Core models",
                    "description": "Define the models",
                    "dependencies": [],
                    "priority": 5,
                    "deliverable": "src/models.py",
                },
                {
                    "id": "t2",
                    "title": "API layer",
                    "description": "Expose the models",
                    "dependencies": ["t1", "ghost"],
                    "priority": 9,
                },
            ]
        }
    )
    brain = brain_with(settings, [payload])
    subtasks = await brain.split_task("Build a service", n_agents=2)
    assert [task["id"] for task in subtasks] == ["t1", "t2"]
    assert subtasks[1]["dependencies"] == ["t1"]  # the invented dependency is dropped
    assert subtasks[1]["priority"] == 5  # clamped to 1..5
    assert brain.calls == 1


async def test_split_task_accepts_fenced_json(settings: Any) -> None:
    """JSON wrapped in a markdown fence is still parsed."""
    fenced = '```json\n{"subtasks": [{"id": "t1", "title": "Only task", "description": "do it"}]}\n```'
    brain = brain_with(settings, [fenced])
    subtasks = await brain.split_task("Build something", n_agents=1)
    assert len(subtasks) == 1 and subtasks[0]["title"] == "Only task"


async def test_split_task_falls_back_to_heuristics(settings: Any) -> None:
    """Unparseable LLM output falls back to the deterministic plan."""
    brain = brain_with(settings, ["I cannot help with that."])
    subtasks = await brain.split_task("Build a CLI tool", n_agents=3)
    assert len(subtasks) == 3
    assert subtasks[0]["id"] == "t1"
    assert "Build a CLI tool" in subtasks[0]["description"]


async def test_split_task_without_brain_uses_heuristics(settings: Any) -> None:
    """An unconfigured brain never raises and still plans."""
    brain = OrchestratorBrain(settings)  # no BRAIN_API_KEY
    assert brain.is_configured is False
    subtasks = await brain.split_task("Build a web app", n_agents=2)
    assert len(subtasks) == 2
    assert all(task["status"] == "pending" for task in subtasks)


async def test_review_output_scores_and_flags_retry(settings: Any) -> None:
    """A structured review answer is returned verbatim (with clamping)."""
    brain = brain_with(
        settings,
        [json.dumps({"score": 0.35, "feedback": "Add error handling", "issues": ["no retries"], "strengths": ["tests"]})],
    )
    review = await brain.review_output({"title": "Write code"}, "```python path=a.py\nx=1\n```")
    assert review["score"] == 0.35
    assert review["needs_retry"] is True
    assert review["feedback"] == "Add error handling"
    assert review["issues"] == ["no retries"]


async def test_review_output_heuristics(settings: Any) -> None:
    """Without a brain the reviewer uses structural signals."""
    brain = OrchestratorBrain(settings)
    good = await brain.review_output(
        {"title": "Write the module"}, "## Result\n```python path=src/app.py\nprint('complete implementation')\n```" + "x" * 700
    )
    assert good["score"] >= 0.6 and good["needs_retry"] is False

    placeholder = await brain.review_output(
        {"title": "Write the module"}, "```python path=src/app.py\n# TODO: implement\n... rest unchanged\n```"
    )
    assert placeholder["needs_retry"] is True
    assert any("placeholder" in issue or "TODO" in issue for issue in placeholder["issues"])

    empty = await brain.review_output({"title": "x"}, "   ")
    assert empty["score"] == 0.0 and empty["needs_retry"] is True


async def test_summarize_state_deterministic_fallback(settings: Any) -> None:
    """The heuristic summary keeps files, tasks and recent activity."""
    brain = OrchestratorBrain(settings)
    summary = await brain.summarize_state(
        {
            "project_name": "demo",
            "tasks": [{"id": "t1", "title": "Core", "status": "completed"}],
            "files": [{"path": "src/app.py"}],
            "history": [{"agent_id": "a1", "action": "task_completed", "result": "Core done"}],
            "last_commit": "abc1234",
        }
    )
    assert "demo" in summary
    assert "src/app.py" in summary
    assert "abc1234" in summary
    assert "Core" in summary


async def test_generate_context_includes_every_section(settings: Any) -> None:
    """Agent context contains the mission, state, task, files and constraints."""
    brain = OrchestratorBrain(settings)
    context = await brain.generate_context_for_agent(
        "agent-1",
        {"id": "t2", "title": "API layer", "description": "Expose endpoints", "dependencies": ["t1"]},
        {
            "project_name": "demo",
            "description": "Build a demo service",
            "tasks": [{"id": "t1", "title": "Core", "status": "completed"}],
            "files": [{"path": "src/core.py"}],
        },
    )
    assert "## Mission" in context
    assert "You are **agent-1**" in context
    assert "## Where the project stands" in context
    assert "## Your assignment" in context
    assert "API layer" in context
    assert "## Files already in the repository" in context
    assert "src/core.py" in context
    assert "## Constraints" in context
    assert "path=src/app.py" in context


async def test_brain_reports_stats_and_closes(settings: Any) -> None:
    """Stats reflect usage and closing releases the client."""
    fake = FakeOpenAIClient(["{}"])
    configured = settings.model_copy(update={"BRAIN_API_KEY": "k"}, deep=True)
    brain = OrchestratorBrain(configured, client_factory=lambda key, url, cfg: fake)
    await brain.complete("hello")
    stats = brain.stats()
    assert stats["calls"] == 1 and stats["configured"] is True
    await brain.close()
    assert fake.closed is True


# ----------------------------------------------------------------------
# Planner
# ----------------------------------------------------------------------
async def test_planner_builds_waves_and_critical_path(settings: Any) -> None:
    """Waves respect dependencies and the critical path is the longest chain."""
    payload = json.dumps(
        {
            "subtasks": [
                {"id": "t1", "title": "Schema", "description": "Define schema", "priority": 5},
                {"id": "t2", "title": "API", "description": "Build API", "dependencies": ["t1"]},
                {"id": "t3", "title": "Tests", "description": "Write tests", "dependencies": ["t2"]},
                {"id": "t4", "title": "Docs", "description": "Write docs"},
            ]
        }
    )
    planner = Planner(brain_with(settings, [payload]), settings=settings)
    plan = await planner.create_plan("Build a service", n_agents=4)
    assert plan["waves"][0] == ["t1", "t4"]
    assert plan["waves"][1] == ["t2"]
    assert plan["waves"][2] == ["t3"]
    assert plan["critical_path"] == ["t1", "t2", "t3"]
    assert plan["revision"] == 1
    assert planner.next_tasks(plan["tasks"])[0]["id"] == "t1"


async def test_planner_breaks_dependency_cycles(settings: Any) -> None:
    """Cyclic dependencies are dropped instead of deadlocking the run."""
    payload = json.dumps(
        {
            "subtasks": [
                {"id": "a", "title": "A", "description": "a", "dependencies": ["b"]},
                {"id": "b", "title": "B", "description": "b", "dependencies": ["a"]},
            ]
        }
    )
    planner = Planner(brain_with(settings, [payload]), settings=settings)
    plan = await planner.create_plan("Cycle test", n_agents=2)
    # Exactly one edge of the cycle is dropped, so every task is schedulable.
    dropped = [task for task in plan["tasks"] if not task["dependencies"]]
    assert len(dropped) == 1
    assert len(plan["waves"]) == 2
    assert sorted(plan["waves"][0] + plan["waves"][1]) == ["a", "b"]


async def test_planner_blocked_and_ready_tasks(settings: Any) -> None:
    """``blocked_tasks`` and ``next_tasks`` reflect the current statuses."""
    planner = Planner(OrchestratorBrain(settings), settings=settings)
    tasks = [
        {"id": "t1", "title": "One", "status": "completed", "dependencies": [], "priority": 3},
        {"id": "t2", "title": "Two", "status": "pending", "dependencies": ["t1"], "priority": 5},
        {"id": "t3", "title": "Three", "status": "pending", "dependencies": ["t2"], "priority": 4},
    ]
    assert planner.blocked_tasks(tasks) == {"t3": ["t2"]}
    assert [task["id"] for task in planner.next_tasks(tasks)] == ["t2"]


async def test_planner_replan_creates_corrective_task(settings: Any) -> None:
    """Replanning keeps existing work and adds a repair task for each failure."""
    planner = Planner(OrchestratorBrain(settings), settings=settings)
    state = {
        "project_name": "demo",
        "description": "Build a demo",
        "tasks": [
            {"id": "t1", "title": "Good", "status": "completed", "dependencies": []},
            {"id": "t2", "title": "Broken", "status": "failed", "dependencies": ["t1"], "error": "syntax error"},
        ],
    }
    plan = await planner.replan(state, [state["tasks"][1]])
    ids = [task["id"] for task in plan["tasks"]]
    assert "t1" in ids and "t2" in ids and "t2-fix" in ids
    repair = next(task for task in plan["tasks"] if task["id"] == "t2-fix")
    assert repair["dependencies"] == ["t1"]
    assert "syntax error" in repair["description"]
    assert plan["revision"] == 1


async def test_planner_names_project(settings: Any) -> None:
    """Project names are derived from the brief."""
    planner = Planner(OrchestratorBrain(settings), settings=settings)
    assert planner.derive_project_name("Build a url shortener. With tests.") == "Build A Url Shortener"
    assert planner.derive_project_name("   ") == "Untitled Project"


# ----------------------------------------------------------------------
# Collector
# ----------------------------------------------------------------------
async def test_collector_extracts_files(tmp_path: Path, settings: Any) -> None:
    """Code blocks tagged with paths become files on disk."""
    collector = Collector(workspace=str(tmp_path / "ws"), settings=settings)
    output = """
Here is the implementation.

```python path=src/app.py
print("hello")
```

And the tests:

```python file=tests/test_app.py
def test_ok():
    assert True
```

A snippet without a path is ignored for file extraction:

```bash
pytest -q
```
"""
    result = await collector.collect_result({"id": "t1", "title": "Implement app"}, output)
    assert sorted(result["file_paths"]) == ["src/app.py", "tests/test_app.py"]
    assert (tmp_path / "ws" / "src" / "app.py").read_text() == 'print("hello")'
    assert result["code_blocks"] == 3
    assert result["success"] is True
    assert result["summary"].startswith("2 file(s)")


async def test_collector_ignores_unsafe_paths(tmp_path: Path, settings: Any) -> None:
    """Traversal and forbidden directories never escape the workspace."""
    collector = Collector(workspace=str(tmp_path / "ws"), settings=settings)
    output = """
```python path=../../etc/passwd
root:x:0:0
```

```python path=.git/hooks/pre-commit
rm -rf /
```

```python path=/absolute/path/app.py
print("ok")
```
"""
    result = await collector.collect_result({"id": "t2"}, output)
    written = {entry["path"] for entry in result["files"]}
    assert "etc/passwd" in written  # traversal stripped, stays inside the workspace
    assert all(".." not in path for path in result["file_paths"])
    assert not any(path.startswith(".git") for path in result["file_paths"])
    assert "absolute/path/app.py" in result["file_paths"]
    assert not (tmp_path / "etc").exists()


async def test_collector_reports_errors_and_commits(tmp_path: Path, settings: Any) -> None:
    """Error lines and commit messages are extracted from agent output."""
    collector = Collector(workspace=str(tmp_path / "ws"), settings=settings, write_files=False)
    output = """
ERROR: ModuleNotFoundError: No module named 'requests'
Traceback (most recent call last):
  File "app.py", line 3

commit: fix(parser): handle empty input
```
"""
    result = await collector.collect_result({"id": "t3"}, output)
    assert any("ModuleNotFoundError" in error for error in result["errors"])
    assert "fix(parser): handle empty input" in result["commit_messages"]


async def test_collector_merges_results_and_detects_conflicts(tmp_path: Path, settings: Any) -> None:
    """Merging keeps the larger version and reports the conflict."""
    collector = Collector(workspace=str(tmp_path / "ws"), settings=settings, write_files=False)
    first = await collector.collect_result(
        {"id": "t1", "title": "A"}, '```python path=src/shared.py\nprint("short")\n```'
    )
    second = await collector.collect_result(
        {"id": "t2", "title": "B"}, '```python path=src/shared.py\nprint("a much longer version of the file")\n```'
    )
    third = await collector.collect_result({"id": "t3", "title": "C"}, "No code produced, sorry.")

    assert third["success"] is False  # a one-line apology is not a result
    merged = await collector.merge_results([first, second, third])
    assert merged["file_count"] == 1
    assert len(merged["conflicts"]) == 1
    assert merged["conflicts"][0]["path"] == "src/shared.py"
    assert merged["files"][0]["producers"] == ["t1", "t2"]
    assert "much longer" in merged["files"][0]["content"]
    assert merged["success_rate"] == pytest.approx(2 / 3, rel=0.01)


async def test_collector_detects_missing_dependencies(tmp_path: Path, settings: Any) -> None:
    """Imports of files nobody produced are reported."""
    collector = Collector(workspace=str(tmp_path / "ws"), settings=settings, write_files=False)
    result = await collector.collect_result(
        {"id": "t1"},
        "```python path=src/app.py\nfrom src.helpers import thing\n```",
    )
    merged = await collector.merge_results([result])
    assert "src/helpers.py" in merged["missing_dependencies"]


async def test_collector_language_only_blocks_are_not_files(tmp_path: Path, settings: Any) -> None:
    """A block tagged only with a language produces no file."""
    collector = Collector(workspace=str(tmp_path / "ws"), settings=settings, write_files=False)
    result = await collector.collect_result({"id": "t1"}, '```python\nprint("snippet")\n```')
    assert result["file_paths"] == []
    assert result["code_blocks"] == 1


async def test_collector_refuses_oversized_files(tmp_path: Path, settings: Any) -> None:
    """Files above the size cap are rejected rather than written."""
    collector = Collector(workspace=str(tmp_path / "ws"), settings=settings, write_files=False)
    with pytest.raises(ValueError):
        await collector.write_file("big.py", "x" * (3 * 1024 * 1024))


# ----------------------------------------------------------------------
# Dispatcher
# ----------------------------------------------------------------------
async def test_dispatcher_respects_dependencies_and_blocks(settings: Any) -> None:
    """A failed task blocks its dependants instead of dispatching them."""
    pool = FakeAgentPool([FakeAgent("agent-1", ["broken output", "should never run"])])
    brain = OrchestratorBrain(settings)
    state = StateManager(FakeTeraBoxPool(), settings=settings, project_id="prj_test")
    dispatcher = Dispatcher(pool, brain, state, settings=settings)
    dispatcher.max_concurrency = 2

    plan = {
        "tasks": [
            {"id": "t1", "title": "Foundation", "description": "core", "dependencies": [], "priority": 5},
            {"id": "t2", "title": "Dependent", "description": "uses t1", "dependencies": ["t1"]},
        ],
        "waves": [["t1"], ["t2"]],
    }
    results = await dispatcher.dispatch(plan, project_id="prj_test")
    assert [result["task_id"] for result in results] == ["t1", "t2"]
    assert results[0]["success"] is False  # the agent produced an unusable answer
    assert results[1]["success"] is False
    assert results[1]["blocked_by"] == ["t1"]
    # The blocked task was never sent to an agent.
    assert pool.agents[0].prompts[-1].count("Dependent") == 0

    state_doc = await state.read_state(project_id="prj_test")
    statuses = {task["id"]: task["status"] for task in state_doc["tasks"]}
    assert statuses == {"t1": "failed", "t2": "blocked"}
    assert any(event["action"] == "task_failed" for event in state_doc["history"])


async def test_dispatcher_retries_when_review_fails(settings: Any) -> None:
    """A low review score triggers one retry with the reviewer feedback."""
    reviews = [
        json.dumps({"score": 0.2, "feedback": "Missing error handling", "issues": ["no try/except"]}),
        json.dumps({"score": 0.9, "feedback": "Good now", "issues": []}),
    ]

    def responder(kwargs: Dict[str, Any]) -> str:
        """Answer review prompts from the script, everything else generically."""
        system = " ".join(
            str(message.get("content", "")) for message in kwargs.get("messages", []) if message.get("role") == "system"
        )
        if "reviewer" in system.lower():
            return reviews.pop(0)
        return "Keep the public interfaces stable."

    brain = brain_with(settings, [], responder=responder)
    outputs = [
        "```python path=src/app.py\nprint('v1')\n```",
        "```python path=src/app.py\ntry:\n    print('v2')\nexcept Exception:\n    pass\n```",
    ]
    pool = FakeAgentPool([FakeAgent("agent-1", outputs)])
    state = StateManager(FakeTeraBoxPool(), settings=settings, project_id="prj_retry")
    dispatcher = Dispatcher(pool, brain, state, settings=settings)

    results = await dispatcher.dispatch(
        {"tasks": [{"id": "t1", "title": "Implement", "description": "write it", "dependencies": []}],
         "waves": [["t1"]]},
        project_id="prj_retry",
    )
    assert results[0]["success"] is True
    assert results[0]["attempts"] == 2
    assert results[0]["review"]["score"] == 0.9
    # The retry prompt carries the reviewer feedback.
    assert len(pool.agents[0].prompts) == 2
    assert "Missing error handling" in pool.agents[0].prompts[1]


async def test_dispatcher_requires_agents(settings: Any) -> None:
    """Dispatching without agents is a configuration error."""

    class EmptyPool(FakeAgentPool):
        def is_configured(self) -> bool:
            return False

    pool = EmptyPool([])
    pool._agents = []  # type: ignore[attr-defined]
    brain = OrchestratorBrain(settings)
    state = StateManager(FakeTeraBoxPool(), settings=settings, project_id="prj_none")
    dispatcher = Dispatcher(pool, brain, state, settings=settings)
    from src.utils.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        await dispatcher.dispatch({"tasks": [{"id": "t1", "title": "x", "dependencies": []}]}, project_id="prj_none")


async def test_dispatch_parallel_marks_blocked_tasks(settings: Any) -> None:
    """``dispatch_parallel`` reports blockers when a dependency failed."""
    pool = FakeAgentPool([FakeAgent("agent-1")])
    brain = OrchestratorBrain(settings)
    state = StateManager(FakeTeraBoxPool(), settings=settings, project_id="prj_par")
    dispatcher = Dispatcher(pool, brain, state, settings=settings)
    results = await dispatcher.dispatch_parallel(
        [{"id": "t2", "title": "Needs t1", "dependencies": ["t1"]}],
        project_id="prj_par",
        completed={"t1": {"success": False}},
    )
    assert results[0]["blocked_by"] == ["t1"]
    assert results[0]["success"] is False


# ----------------------------------------------------------------------
# Orchestrator (end to end with fakes)
# ----------------------------------------------------------------------
def _fake_github_client(settings: Settings) -> Any:
    """A GitHubClient wired to an in-memory transport with an empty repo."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/commits"):
            return httpx.Response(200, json=[])
        if path.endswith("/pulls"):
            return httpx.Response(200, json=[])
        if "/contents/" in path:
            return httpx.Response(404, json={"message": "Not Found"})
        if path.endswith("/git/trees/main"):
            return httpx.Response(200, json={"tree": []})
        if path.endswith("/branches/main"):
            return httpx.Response(200, json={"commit": {"sha": "0" * 40}})
        return httpx.Response(200, json={})

    client = GitHubClient(settings=settings)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=settings.GITHUB_API_URL
    )
    return client


def build_fake_orchestrator(settings: Settings) -> Orchestrator:
    """Build an Orchestrator whose subsystems are all in-memory fakes."""
    orchestrator = Orchestrator(settings)
    pool = FakeTeraBoxPool()
    orchestrator.pool = pool  # type: ignore[assignment]
    orchestrator.state = StateManager(pool, settings=settings)  # type: ignore[arg-type]
    orchestrator.agent_pool = FakeAgentPool(  # type: ignore[assignment]
        [FakeAgent("agent-1", outputs=['```python path=src/app.py\nprint("built")\n```', '```python path=tests/test_app.py\nassert True\n```'])]
    )
    orchestrator.brain = OrchestratorBrain(settings)
    orchestrator.planner = Planner(orchestrator.brain, settings=settings)
    # Hermetic GitHub: tests must never reach api.github.com (CI has network and
    # would answer 401 for the fake token in the settings fixture).
    orchestrator.github = _fake_github_client(settings)
    orchestrator.sync_engine.github = orchestrator.github
    orchestrator.sync_engine.agent_pool = orchestrator.agent_pool
    return orchestrator


async def test_orchestrator_create_and_run_project(settings: Any, tmp_path: Path) -> None:
    """A project can be created, planned and executed end to end."""
    orchestrator = build_fake_orchestrator(settings)

    record = await orchestrator.create_project("demo", "Build a tiny service with tests", 2)
    assert record["project_id"].startswith("prj_")
    assert len(record["plan"]["tasks"]) == 2

    summary = await orchestrator.run_project(record["project_id"])
    assert summary["tasks_dispatched"] == 2
    assert summary["completed"] == 2
    assert summary["status"] == "completed"
    assert summary["artifact"]["file_count"] >= 1

    status = await orchestrator.get_project_status(record["project_id"])
    assert status["project_name"] == "demo"
    assert all(task["status"] == "completed" for task in status["tasks"])

    files = await orchestrator.get_project_files(record["project_id"])
    assert files

    projects = await orchestrator.list_projects()
    assert any(project["project_id"] == record["project_id"] for project in projects)

    health = await orchestrator.health()
    assert health["status"] == "ok"
    assert health["subsystems"]["agents"]["agents"] == 1


async def test_orchestrator_rejects_empty_description(settings: Any) -> None:
    """An empty brief is rejected before any planning happens."""
    orchestrator = build_fake_orchestrator(settings)
    with pytest.raises(ValueError):
        await orchestrator.create_project("demo", "   ", 1)


async def test_orchestrator_upload_and_sync(settings: Any, tmp_path: Path) -> None:
    """Files can be archived and the sync pass runs without errors."""
    orchestrator = build_fake_orchestrator(settings)
    local = tmp_path / "artifact.txt"
    local.write_text("payload", encoding="utf-8")

    result = await orchestrator.upload_project_file("prj_x", str(local))
    assert result["path"].endswith("artifact.txt")

    summary = await orchestrator.trigger_sync()
    assert "started_at" in summary
    assert orchestrator.sync_engine.status()["runs"] == 1


async def test_plans_are_scoped_per_project(settings: Any) -> None:
    """Plan task ids (t1, t2, ...) are project-local and must not collide.

    Two projects planned in the same database both use ids like ``t1``; the
    ``tasks`` table is keyed by ``(id, project_id)`` so both persist.
    """
    from sqlmodel import select

    from src.db.models import Task, session_scope

    orchestrator = build_fake_orchestrator(settings)
    first = await orchestrator.create_project("one", "Build a URL shortener with tests", 2)
    second = await orchestrator.create_project("two", "Build a pastebin clone with tests", 2)

    assert [task["id"] for task in first["plan"]["tasks"]] == ["t1", "t2"]
    assert [task["id"] for task in second["plan"]["tasks"]] == ["t1", "t2"]

    with session_scope() as session:
        rows = list(session.exec(select(Task)).all())
        assert len(rows) == 4
        assert {row.project_id for row in rows} == {first["project_id"], second["project_id"]}
        assert session.get(Task, ("t1", first["project_id"])) is not None

    status = await orchestrator.get_project_status(first["project_id"])
    assert [task["id"] for task in status["tasks"]] == ["t1", "t2"]


async def test_completed_tasks_are_persisted_to_sqlite(settings: Any) -> None:
    """Dispatcher outcomes reach SQLite, not just the state document."""
    from sqlmodel import select

    from src.db.models import Task, session_scope

    orchestrator = build_fake_orchestrator(settings)
    record = await orchestrator.create_project("demo", "Build a tiny service with tests", 2)
    await orchestrator.run_project(record["project_id"])

    with session_scope() as session:
        rows = list(
            session.exec(select(Task).where(Task.project_id == record["project_id"])).all()
        )
    assert rows and all(row.status == "completed" for row in rows)
    assert all(row.attempts >= 1 for row in rows)
    assert all(row.assigned_agent for row in rows)


async def test_replan_project_adds_corrective_tasks(settings: Any) -> None:
    """Failed tasks can be replanned into corrective subtasks."""
    orchestrator = build_fake_orchestrator(settings)
    record = await orchestrator.create_project("demo", "Build a tiny service with tests", 2)

    # Force a failure, then replan it.
    orchestrator.agent_pool.agents[0].outputs = ["broken output"]
    await orchestrator.run_project(record["project_id"])
    status = await orchestrator.get_project_status(record["project_id"])
    failed = [task for task in status["tasks"] if task["status"] == "failed"]
    assert failed, "expected at least one failed task"

    outcome = await orchestrator.replan_project(record["project_id"])
    assert outcome["project_id"] == record["project_id"]
    assert outcome["revision"] >= 2
    new_ids = {str(task["id"]) for task in outcome["new_tasks"]}
    assert new_ids, "replanning must produce corrective tasks"
    assert not (new_ids & {"t1", "t2"}), "corrective tasks must use fresh ids"

    # Replanning a healthy project is a no-op.
    healthy = await orchestrator.create_project("ok", "Build a tiny service with tests", 2)
    noop = await orchestrator.replan_project(healthy["project_id"])
    assert noop["new_tasks"] == []


async def test_start_reports_degraded_subsystems_without_probing(settings: Any) -> None:
    """An unconfigured GitHub must not be probed (no retries, no 404s)."""
    degraded = settings.model_copy(
        update={"GITHUB_TOKEN": "", "GITHUB_REPO": "owner/repo", "CRON_ENABLED": False}
    )
    orchestrator = build_fake_orchestrator(degraded)
    report = await orchestrator.start()
    assert report["github"]["configured"] is False
    assert report["github"]["ok"] is False
    assert "not configured" in report["github"]["error"]
    assert report["warnings"], "degraded configuration must be surfaced"
    health = await orchestrator.health()
    assert health["status"] == "ok"
    assert health["subsystems"]["github"]["configured"] is False
