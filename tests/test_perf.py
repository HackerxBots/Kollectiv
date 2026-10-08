"""Performance tripwires: the Python side of Kollektiv must stay cheap.

The orchestrator is network-bound — a run's wall clock is model latency, not
interpreter time — so these tests do not chase microseconds. They catch the
regressions that actually hurt and are easy to introduce by accident:

* a quadratic loop in the cost estimator (it must stay linear in task count);
* the ledger or the database creeping into ``/health``, which must answer
  instantly;
* a synchronous call smuggled into an async path (planning, catalogue building);
* the config parser becoming slow enough to be felt on every CLI command.

Every ceiling here is deliberately generous — tens to hundreds of times the
measured value on a developer laptop — because CI machines are slow and a flaky
performance test is worse than none. For the real numbers, run
``python scripts/benchmark.py``; see ``docs/performance.md``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from config.settings import Settings

ROOT = Path(__file__).resolve().parents[1]

#: Ceilings, in seconds. See the module docstring for why they are loose.
ESTIMATE_5000_CEILING = 0.1
CONFIG_PARSE_CEILING = 1.5
HEALTH_CEILING = 0.5
CREATE_PROJECT_CEILING = 2.0
CATALOGUE_CEILING = 1.0


def run(coro: Any) -> Any:
    """Run an awaitable to completion (the suite has no async plugin)."""
    return asyncio.run(coro)


def measure(work: Callable[[], Any]) -> float:
    """Return how long ``work`` took, in seconds."""
    started = time.perf_counter()
    work()
    return time.perf_counter() - started


def _plan(tasks: int) -> Dict[str, Any]:
    """A synthetic plan with ``tasks`` tasks, in one chain."""
    items: List[Dict[str, Any]] = [
        {
            "id": f"t{index + 1}",
            "title": f"Task {index + 1}",
            "description": "Implement one module with tests.",
            "dependencies": [f"t{index}"] if index else [],
            "priority": 3,
        }
        for index in range(tasks)
    ]
    return {"tasks": items, "waves": [[item["id"]] for item in items], "n_agents": 2}


def test_estimating_a_huge_plan_stays_linear(settings: Settings) -> None:
    """5 000 tasks must estimate in milliseconds — no quadratic surprise."""
    from src.orchestrator.budget import BudgetPlanner

    planner = BudgetPlanner(settings)
    small, large = _plan(100), _plan(5_000)

    # Warm up, then measure; the first call pays for imports inside the planner.
    planner.estimate(small, project_id="prj_small")
    small_seconds = measure(lambda: planner.estimate(small, project_id="prj_small"))
    large_seconds = measure(lambda: planner.estimate(large, project_id="prj_large"))

    assert large_seconds < ESTIMATE_5000_CEILING, f"5 000 tasks took {large_seconds:.3f}s"
    # 50x the tasks must not cost more than 100x the time: linear, with slack.
    per_small = max(small_seconds, 1e-6) / 100
    per_large = large_seconds / 5_000
    assert per_large < per_small * 100, f"per-task cost grew {per_large / per_small:.1f}x with the plan size"


def test_config_parsing_is_fast_enough_to_run_on_every_command(settings: Settings) -> None:
    """``.kollektiv.yml`` is parsed per command; it must not be felt."""
    from src.utils.project_config import STARTER_TEMPLATE, parse_config_text

    parse_config_text(STARTER_TEMPLATE)  # warm up
    seconds = measure(lambda: [parse_config_text(STARTER_TEMPLATE) for _ in range(200)])
    assert seconds < CONFIG_PARSE_CEILING, f"200 parses took {seconds:.3f}s"


def test_the_builtin_subset_reader_is_not_slower_than_pyyaml(settings: Settings) -> None:
    """The dependency-free fallback earns its place: it must not be the slow path."""
    pytest.importorskip("yaml")
    from src.utils.project_config import STARTER_TEMPLATE, parse_config_text
    from src.utils.yaml_subset import loads as subset_loads

    parse_config_text(STARTER_TEMPLATE)
    subset_loads(STARTER_TEMPLATE)

    pyyaml_seconds = measure(lambda: [parse_config_text(STARTER_TEMPLATE) for _ in range(50)])
    subset_seconds = measure(lambda: [subset_loads(STARTER_TEMPLATE) for _ in range(50)])
    assert subset_seconds <= pyyaml_seconds, (
        f"the strict reader ({subset_seconds:.4f}s) is slower than PyYAML "
        f"({pyyaml_seconds:.4f}s) — that is backwards"
    )


def test_health_answers_instantly_and_never_queries_the_ledger(settings: Settings) -> None:
    """``/health`` reports configuration; it must not touch the database."""
    from src.orchestrator.app import Orchestrator

    orchestrator = Orchestrator(settings)
    seconds = measure(lambda: run(orchestrator.health()))
    assert seconds < HEALTH_CEILING, f"health took {seconds:.3f}s"

    payload = run(orchestrator.health())
    budget = payload["subsystems"]["budget"]
    assert budget["enabled"] is True
    assert "ledger" in budget and "must not query" in budget["ledger"]


def test_planning_and_dispatch_overhead_is_small(settings: Settings) -> None:
    """A whole offline project create — plan plus database writes — under 2 s."""
    from src.orchestrator.app import Orchestrator

    orchestrator = Orchestrator(settings)

    async def _create() -> Dict[str, Any]:
        """Plan one small project with the heuristic brain."""
        return await orchestrator.create_project("perf", "Build a tiny notes API with tests", 2)

    seconds = measure(lambda: run(_create()))
    assert seconds < CREATE_PROJECT_CEILING, f"create_project took {seconds:.3f}s"
    assert run(_create())["plan"]["tasks"], "the plan should have tasks"


def test_the_gateway_catalogue_builds_cheaply(settings: Settings) -> None:
    """Descriptor assembly for all 57 tools is pure dict work, not I/O."""
    from src.gateway.tools import build_catalogue, toolkit_view
    from src.orchestrator.app import Orchestrator

    orchestrator = Orchestrator(settings)

    async def _build() -> Dict[str, Any]:
        """Build the catalogue against a started orchestrator."""
        await orchestrator.start()
        try:
            return build_catalogue(orchestrator, settings=settings)
        finally:
            await orchestrator.stop()

    catalogue = run(_build())
    assert len(catalogue) >= 57

    async def _one_more() -> None:
        """One more build, with the orchestrator warm."""
        await orchestrator.start()
        try:
            build_catalogue(orchestrator, settings=settings)
        finally:
            await orchestrator.stop()

    seconds = measure(lambda: run(_one_more()))
    assert seconds < CATALOGUE_CEILING, f"building the catalogue took {seconds:.3f}s"
    assert toolkit_view(catalogue), "the toolkit view should not be empty"


def test_the_benchmark_script_runs_and_reports() -> None:
    """``scripts/benchmark.py --quick`` is the documented way to get numbers."""
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "benchmark.py"), "--quick", "--repeat", "1", "--json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    payload = json.loads(completed.stdout)
    assert payload["environment"]["python"]
    labels = {row["label"] for row in payload["results"]}
    assert "estimate.json@200" in labels and "health" in labels
    assert all(row["ok"] for row in payload["results"]), [
        row for row in payload["results"] if not row["ok"]
    ]
