#!/usr/bin/env python3
"""Measure the parts of Kollektiv that run in Python, and say what they cost.

The orchestrator is a network-bound program: the wall-clock time of a real run
is dominated by waiting for the brain and the workers, not by the interpreter.
That is a claim, not a feeling, so this script puts numbers behind it and keeps
them reproducible on any machine:

```bash
python scripts/benchmark.py                 # the table
python scripts/benchmark.py --quick         # smaller N (used by the test suite)
python scripts/benchmark.py --json          # machine-readable
```

Every benchmark is offline and hermetic: an in-memory/temporary SQLite database,
the heuristic brain (no API key), no workers, no network. Nothing is written
outside a temporary directory that is removed on exit.

What is measured, and why each one:

* ``estimate.json@N`` — the cost estimator is pure arithmetic over a plan, so it
  must be linear in the task count and effectively free.
* ``config.parse@N`` — ``.kollektiv.yml`` parsing, which runs once per command.
* ``config.subset@N`` — the fallback YAML reader, used when PyYAML is absent.
* ``connectors.build`` / ``connectors.catalog`` — registry construction and the
  41-action catalogue a client reads.
* ``gateway.catalogue`` — building all 57 gateway tools.
* ``project.create`` — planning plus database writes for a small project, i.e.
  the CPU cost behind ``POST /projects``.
* ``budget.record@N`` — one ledger row per project per day, so this is a
  once-a-day write path: 200 of them is a stress test, not a workload.
* ``run.empty_pool`` — a full ``run_project`` with no workers configured. It is a
  no-op, which is the point: with nothing to dispatch, orchestration costs
  microseconds. Add workers and the time becomes model latency, not Python.
* ``cli.help`` — process start to argument parsing, the number a user feels.

Exit codes: ``0`` always succeeded, ``1`` when a benchmark raised (the error is
printed). Timings are medians of ``--repeat`` runs unless stated otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # running the file directly, not as a module
    sys.path.insert(0, str(ROOT))

LOGGER = logging.getLogger("kollektiv.benchmark")

Result = Dict[str, Any]


def _timed(label: str, work: Callable[[], Any], repeat: int = 3) -> Result:
    """Run ``work`` ``repeat`` times and return the median, in milliseconds.

    Args:
        label: Name shown in the table.
        work: Zero-argument callable; its return value is ignored.
        repeat: Number of runs (median is reported, so one slow run is ignored).

    Returns:
        ``{"label", "ms", "ms_min", "ms_max", "ok", "error"}`` — a failure is
        recorded rather than raised, so one broken benchmark does not hide the
        rest of the table.
    """
    samples: List[float] = []
    error = ""
    for _ in range(max(1, repeat)):
        started = time.perf_counter()
        try:
            work()
        except Exception as exc:  # noqa: BLE001 - report, never crash the run
            LOGGER.exception("Benchmark %s failed", label)
            error = f"{type(exc).__name__}: {exc}"
            break
        samples.append((time.perf_counter() - started) * 1000.0)
    if not samples:
        return {"label": label, "ms": None, "ms_min": None, "ms_max": None, "ok": False, "error": error}
    return {
        "label": label,
        "ms": round(statistics.median(samples), 3),
        "ms_min": round(min(samples), 3),
        "ms_max": round(max(samples), 3),
        "ok": True,
        "error": "",
    }


def _interpreter() -> Dict[str, Any]:
    """Describe the interpreter: version, free-threading, and key libraries."""
    info: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "implementation": sys.implementation.name,
        "platform": sys.platform,
        "executable": sys.executable,
    }
    gil_enabled = getattr(sys, "_is_gil_enabled", None)
    info["free_threaded"] = not (bool(gil_enabled()) if callable(gil_enabled) else True)
    try:
        import yaml  # noqa: PLC0415 - optional dependency, probed on purpose

        info["pyyaml"] = getattr(yaml, "__version__", "unknown")
    except Exception:  # noqa: BLE001 - absence is a valid answer
        info["pyyaml"] = None
    return info


def _plan(tasks: int) -> Dict[str, Any]:
    """Build a synthetic plan with ``tasks`` tasks in one long chain."""
    items = [
        {
            "id": f"t{index + 1}",
            "title": f"Task {index + 1}",
            "description": "Implement one module of the notes API with tests.",
            "dependencies": [f"t{index}"] if index else [],
            "priority": 3,
        }
        for index in range(tasks)
    ]
    return {
        "tasks": items,
        "waves": [[item["id"]] for item in items],
        "critical_path": [item["id"] for item in items],
        "n_agents": 2,
    }


def benchmark(quick: bool = False, repeat: int = 3) -> Tuple[List[Result], Dict[str, Any]]:
    """Run every benchmark and return ``(results, environment)``.

    Args:
        quick: Use smaller task counts (the test-suite path).
        repeat: Runs per benchmark, for the median.

    Returns:
        The result rows in display order, plus interpreter/environment details.
    """
    from config.settings import Settings
    from src.connectors import ConnectorRegistry
    from src.gateway.tools import build_catalogue
    from src.orchestrator.app import Orchestrator
    from src.orchestrator.budget import BudgetPlanner
    from src.utils.project_config import STARTER_TEMPLATE, parse_config_text
    from src.utils.yaml_subset import loads as subset_loads

    sizes = (50, 200) if quick else (100, 1_000, 5_000)
    parses = 200 if quick else 2_000
    results: List[Result] = []

    with tempfile.TemporaryDirectory(prefix="kollektiv-bench-") as tmp:
        workdir = Path(tmp)
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            SECRET_KEY="benchmark-secret",
            ENVIRONMENT="development",
            DATABASE_URL=f"sqlite:///{workdir / 'bench.db'}",
            WORKSPACE_DIR=str(workdir / "workspace"),
            LOG_LEVEL="ERROR",
            AUTO_INIT_DB=True,
            BRAIN_API_KEY="",
            GITHUB_TOKEN="",
            GITHUB_REPO="",
            ARENA_ACCOUNTS="[]",
            TERABOX_ACCOUNTS="[]",
            SPONSORS_ENABLED=False,
            CRON_ENABLED=False,
        )
        planner = BudgetPlanner(settings)

        # -- pure CPU paths ------------------------------------------------
        for size in sizes:
            plan = _plan(size)

            def _estimate(current: Dict[str, Any] = plan) -> None:
                """Estimate one synthetic plan (bound as a default for the loop)."""
                planner.estimate(current, project_id="prj_bench")

            results.append(_timed(f"estimate.json@{size}", _estimate, repeat))

        def _parse_many(reader: Callable[[str], Any]) -> None:
            """Parse the starter config ``parses`` times with one reader."""
            for _ in range(parses):
                reader(STARTER_TEMPLATE)

        results.append(_timed(f"config.parse@{parses}", lambda: _parse_many(parse_config_text), repeat))
        results.append(_timed(f"config.subset@{parses}", lambda: _parse_many(subset_loads), repeat))

        results.append(
            _timed(
                "connectors.build",
                lambda: ConnectorRegistry.from_settings(settings),
                repeat,
            )
        )
        registry = ConnectorRegistry.from_settings(settings)
        results.append(_timed("connectors.catalog", registry.catalog, repeat))

        # -- orchestrator paths --------------------------------------------
        orchestrator = Orchestrator(settings)

        async def _run() -> None:
            """Exercise the async surface that cannot be timed synchronously."""
            try:
                await orchestrator.start()
                started = time.perf_counter()
                build_catalogue(orchestrator, registry=registry, settings=settings)
                catalogue_ms = (time.perf_counter() - started) * 1000.0

                created = await orchestrator.create_project(
                    "benchmark", "Build a tiny notes API with tests and a README.", 2
                )
                project_id = created["project_id"]
                created_ms = (time.perf_counter() - started) * 1000.0 - catalogue_ms

                await orchestrator.estimate_project_cost(project_id)

                ledger_started = time.perf_counter()
                for _ in range(parses):
                    await orchestrator.budget_ledger.record(
                        "prj_bench",
                        runs=1,
                        tasks=1,
                        usd=0.0,
                        brain_calls=1,
                        brain_tokens_in=10,
                        brain_tokens_out=10,
                        estimated=True,
                    )
                ledger_ms = (time.perf_counter() - ledger_started) * 1000.0

                run_started = time.perf_counter()
                try:
                    await orchestrator.run_project(project_id)
                except Exception as exc:  # noqa: BLE001 - empty pool is expected
                    LOGGER.debug("run_project with an empty pool raised %s", exc)
                run_ms = (time.perf_counter() - run_started) * 1000.0

                health_started = time.perf_counter()
                await orchestrator.health()
                health_ms = (time.perf_counter() - health_started) * 1000.0

                # A second, larger project: planning cost scales with the plan.
                big = await orchestrator.create_project(
                    "benchmark-large",
                    "Build a full task manager with auth, tests, docs and CI.",
                    4,
                )
                await orchestrator.estimate_project_cost(big["project_id"])

                results.extend(
                    [
                        {"label": "gateway.catalogue", "ms": round(catalogue_ms, 3), "ms_min": None, "ms_max": None, "ok": True, "error": ""},
                        {"label": "project.create", "ms": round(created_ms, 3), "ms_min": None, "ms_max": None, "ok": True, "error": ""},
                        {"label": f"budget.record@{parses}", "ms": round(ledger_ms, 3), "ms_min": None, "ms_max": None, "ok": True, "error": ""},
                        {"label": "run.empty_pool", "ms": round(run_ms, 3), "ms_min": None, "ms_max": None, "ok": True, "error": ""},
                        {"label": "health", "ms": round(health_ms, 3), "ms_min": None, "ms_max": None, "ok": True, "error": ""},
                    ]
                )
            finally:
                await orchestrator.stop()

        try:
            asyncio.run(_run())
        except Exception as exc:  # noqa: BLE001 - keep the CPU rows visible
            LOGGER.exception("Async benchmarks failed")
            results.append({"label": "async suite", "ms": None, "ms_min": None, "ms_max": None, "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    # -- process start -----------------------------------------------------
    env = dict(os.environ)
    env.setdefault("SECRET_KEY", "benchmark-secret")
    started = time.perf_counter()
    subprocess.run(
        [sys.executable, "-m", "src.api.cli", "--help"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    results.append(
        {
            "label": "cli.help (cold start)",
            "ms": round((time.perf_counter() - started) * 1000.0, 3),
            "ms_min": None,
            "ms_max": None,
            "ok": True,
            "error": "",
        }
    )
    return results, _interpreter()


def _print_table(results: List[Result], environment: Dict[str, Any]) -> None:
    """Print the results as a plain table, plus the environment header."""
    print("Kollektiv Python benchmark")
    print(
        "  interpreter: {python} ({implementation}, {platform})  "
        "free-threaded build: {free_threaded}  PyYAML: {pyyaml}".format(**environment)
    )
    print()
    print(f"{'benchmark':<26}{'median':>12}{'min':>12}{'max':>12}  status")
    print("-" * 76)
    for row in results:
        median = "-" if row["ms"] is None else f"{row['ms']:.3f} ms"
        low = "-" if row["ms_min"] is None else f"{row['ms_min']:.3f} ms"
        high = "-" if row["ms_max"] is None else f"{row['ms_max']:.3f} ms"
        status = "ok" if row["ok"] else f"FAILED {row['error']}"
        print(f"{row['label']:<26}{median:>12}{low:>12}{high:>12}  {status}")
    print()
    print("The orchestrator is network-bound: 'run.empty_pool' is the Python-side")
    print("overhead of a run, so compare it with the seconds a model takes to answer.")


def main(argv: Optional[List[str]] = None) -> int:
    """Parse arguments, run the benchmarks and print them.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` when every benchmark ran, ``1`` when one failed.
    """
    parser = argparse.ArgumentParser(description="Measure Kollektiv's Python-side cost.")
    parser.add_argument("--quick", action="store_true", help="smaller N, for CI")
    parser.add_argument("--repeat", type=int, default=3, help="runs per benchmark (median reported)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    parser.add_argument("--verbose", action="store_true", help="show warnings from the app itself")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.CRITICAL,
        format="%(levelname)s %(name)s: %(message)s",
    )
    results, environment = benchmark(quick=args.quick, repeat=args.repeat)
    if args.json:
        print(json.dumps({"environment": environment, "results": results}, indent=2))
    else:
        _print_table(results, environment)
    return 0 if all(row["ok"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
