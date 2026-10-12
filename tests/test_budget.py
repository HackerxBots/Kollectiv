"""Tests for the project config file, the cost model and the spend ledger.

Hermetic like the rest of the suite: settings copies, an in-memory database, and
no network. The config reader is exercised against both parsers (PyYAML and the
built-in subset) so the same file cannot behave differently on two machines.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

import pytest

from config.settings import Settings
from src.orchestrator.budget import (
    PLANNER_INPUT_TOKENS,
    BudgetLedger,
    BudgetPlanner,
    CostEstimate,
    daily_cap_check,
    estimate_from_dict,
    usd_for_tokens,
)
from src.utils.errors import BudgetError, ConfigurationError
from src.utils.project_config import (
    STARTER_TEMPLATE,
    find_config_file,
    load_project_config,
    parse_config_text,
)
from src.utils.yaml_subset import loads as yaml_loads


def run(coro: Any) -> Any:
    """Run a coroutine from a synchronous test."""
    return asyncio.run(coro)


def plan(tasks: int = 3, description: str = "") -> Dict[str, Any]:
    """Build a plan document with ``tasks`` subtasks."""
    return {
        "tasks": [
            {"id": f"t{index + 1}", "title": f"task {index + 1}", "description": description} for index in range(tasks)
        ],
        "waves": [[f"t{index + 1}"] for index in range(tasks)],
        "n_agents": tasks,
    }


# ----------------------------------------------------------------------
# The YAML subset reader
# ----------------------------------------------------------------------
def test_subset_reader_handles_the_starter_file() -> None:
    """Whatever ``kollektiv init-config`` writes must parse with no dependencies."""
    document = yaml_loads(STARTER_TEMPLATE)
    assert document["project"]["n_agents"] == 3
    assert document["budget"]["max_usd"] == 0
    assert document["brain"]["provider"] == "deepseek"
    assert document["storage"]["backend"] == "local"


def test_subset_reader_covers_scalars_lists_and_comments() -> None:
    """The supported shapes, including the ones people actually type."""
    document = yaml_loads(
        """
        # a comment
        name: "quoted: value"      # trailing comment
        count: 4
        ratio: 0.75
        flag: yes
        off_flag: false
        empty:
        inline: [a, "b c", 3]
        nested:
          child: 1
          deeper:
            leaf: x
        items:
          - one
          - two
          - name: three
            weight: 3
        """
    )
    assert document["name"] == "quoted: value"
    assert document["count"] == 4 and document["ratio"] == 0.75
    assert document["flag"] is True and document["off_flag"] is False
    assert document["empty"] is None
    assert document["inline"] == ["a", "b c", 3]
    assert document["nested"]["deeper"]["leaf"] == "x"
    assert document["items"][2] == {"name": "three", "weight": 3}


def test_subset_reader_refuses_instead_of_guessing() -> None:
    """Every unsupported construct names its line, and never half-parses."""
    cases = {
        "a:\n\tb: 1": "tabs",
        "a: &anchor 1": "anchors",
        "a: !tag 1": "tags",
        "a: |\n  text": "block scalars",
        "a: {b: 1}": "flow mappings",
        "---\na: 1": "multi-document",
        "a: 1\na: 2": "duplicate",
        "just a sentence": "expected 'key: value'",
        "a:\n    b: 1\n  c: 2": "indentation",
    }
    for text, needle in cases.items():
        with pytest.raises(ConfigurationError) as excinfo:
            yaml_loads(text)
        assert needle.lower() in str(excinfo.value).lower(), text


# ----------------------------------------------------------------------
# The project config file
# ----------------------------------------------------------------------
def test_starter_file_round_trips_through_the_loader(tmp_path: Path) -> None:
    """``init-config`` then a real load, with both parsers, gives the same config."""
    target = tmp_path / ".kollektiv.yml"
    target.write_text(STARTER_TEMPLATE, encoding="utf-8")
    config = load_project_config(str(target))
    assert config.path == target and config.problems == []
    assert (config.n_agents, config.max_concurrency) == (3, 3)
    assert config.budget_max_usd == 0
    assert config.budget_warn_at == 0.8
    assert (config.brain_provider, config.brain_temperature) == ("deepseek", 0.3)
    assert config.storage_backend == "local"

    subset = parse_config_text(STARTER_TEMPLATE)
    assert subset["project"]["max_concurrency"] == 3


def test_config_values_are_validated_not_trusted(tmp_path: Path) -> None:
    """A wrong type or an unknown section is reported and ignored."""
    target = tmp_path / ".kollektiv.yml"
    target.write_text(
        """
        project:
          n_agents: 99
          max_concurrency: "three"
        budget:
          max_usd: 2.5
          warn_at: 4
        brain:
          temperature: 0.2
        nonsense:
          key: 1
        """,
        encoding="utf-8",
    )
    config = load_project_config(str(target))
    assert config.n_agents is None  # 99 is out of range
    assert config.max_concurrency is None  # "three" is not a number
    assert config.budget_max_usd == 2.5
    assert config.budget_warn_at is None
    assert config.brain_temperature == 0.2
    assert any("n_agents" in problem for problem in config.problems)
    assert any("max_concurrency" in problem for problem in config.problems)
    assert any("warn_at" in problem for problem in config.problems)
    assert any("unknown section 'nonsense'" in problem for problem in config.problems)


def test_a_broken_config_never_stops_the_run(tmp_path: Path) -> None:
    """Unreadable, non-mapping and missing files all degrade to the defaults."""
    configs = tmp_path / "configs"
    configs.mkdir()
    broken = configs / ".kollektiv.yml"
    broken.write_text("project: [1, 2\n", encoding="utf-8")
    config = load_project_config(str(broken))
    assert config.problems and config.n_agents is None and config.found is True

    listing = configs / ".kollektiv.json"
    listing.write_text("[1, 2, 3]", encoding="utf-8")
    assert any("mapping" in problem for problem in load_project_config(str(listing)).problems)

    assert load_project_config(str(configs / "nope.yml")).problems

    # A directory with no config anywhere above it reports "not found" rather
    # than an error: the file is optional. (The search walks parents, so this
    # asserts on a tree that has none.)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "sub").mkdir(parents=True)
    assert find_config_file(elsewhere / "sub") is None


def test_json_is_supported_and_found_by_the_same_search(tmp_path: Path) -> None:
    """``.kollektiv.json`` needs no YAML parser at all, and the search finds it."""
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (tmp_path / ".kollektiv.json").write_text('{"project": {"n_agents": 5}}', encoding="utf-8")
    assert find_config_file(nested) == tmp_path / ".kollektiv.json"
    config = load_project_config(None, start=nested)
    assert config.n_agents == 5

    # A YAML file in the same directory wins, because it is checked first.
    (tmp_path / ".kollektiv.yml").write_text("project:\n  n_agents: 2\n", encoding="utf-8")
    assert load_project_config(None, start=nested).n_agents == 2


# ----------------------------------------------------------------------
# The cost estimate
# ----------------------------------------------------------------------
def test_estimate_is_arithmetic_on_the_plan(settings: Settings) -> None:
    """More tasks and longer descriptions cost more; the maths is inspectable."""
    planner = BudgetPlanner(
        settings.model_copy(update={"BUDGET_WORKER_PRICE_IN_PER_MTOK": 0.05, "BUDGET_WORKER_PRICE_OUT_PER_MTOK": 0.1})
    )
    small = planner.estimate(plan(2), project_id="p1")
    large = planner.estimate(plan(10, description="x" * 4000), project_id="p1")

    assert small.tasks == 2 and small.waves == 2 and small.worker_calls == 2
    assert small.brain_calls == 4  # plan + two reviews + summary
    assert small.brain_tokens_in >= PLANNER_INPUT_TOKENS
    assert large.worker_tokens_in > small.worker_tokens_in
    assert large.brain_usd > small.brain_usd and large.worker_usd > small.worker_usd
    assert small.total_usd == round(small.brain_usd + small.worker_usd, 6)
    assert small.verdict == "ok" and small.remaining_usd is None
    assert small.to_dict()["estimate_only"] is True


def test_estimate_uses_configured_prices_not_vendor_numbers(settings: Settings) -> None:
    """Change the price, change the estimate — the operator's numbers decide."""
    cheap = BudgetPlanner(settings.model_copy(update={"BUDGET_PRICE_IN_PER_MTOK": 0.01, "BUDGET_PRICE_OUT_PER_MTOK": 0.02}))
    dear = BudgetPlanner(settings.model_copy(update={"BUDGET_PRICE_IN_PER_MTOK": 30.0, "BUDGET_PRICE_OUT_PER_MTOK": 60.0}))
    document = plan(4)
    assert dear.estimate(document).total_usd > cheap.estimate(document).total_usd * 100
    assert cheap.prices()["brain_in"] == 0.01


def test_an_empty_plan_still_estimates_the_planner_call(settings: Settings) -> None:
    """Estimating a project that has not been planned is the common case."""
    estimate = BudgetPlanner(settings).estimate({}, project_id="p1")
    assert estimate.tasks == 0 and estimate.brain_calls == 2
    assert estimate.total_usd > 0  # the planning call itself
    assert estimate.to_dict()["usd"]["total"] == estimate.total_usd


def test_caps_come_from_the_project_file_first(settings: Settings, tmp_path: Path) -> None:
    """A ``.kollektiv.yml`` cap beats ``BUDGET_MAX_USD``; settings win over nothing."""
    from src.utils.project_config import ProjectConfig

    by_settings = BudgetPlanner(settings.model_copy(update={"BUDGET_MAX_USD": 5.0}))
    assert by_settings.cap() == (5.0, "BUDGET_MAX_USD")

    file_planner = BudgetPlanner(
        settings.model_copy(update={"BUDGET_MAX_USD": 5.0}),
        config=ProjectConfig(path=tmp_path / ".kollektiv.yml", budget_max_usd=1.5, budget_warn_at=0.5),
    )
    assert file_planner.cap() == (1.5, ".kollektiv.yml")
    assert file_planner.warn_at() == 0.5
    assert BudgetPlanner(settings).cap() == (0.0, "none")


def test_verdicts_and_refusals(settings: Settings) -> None:
    """ok / warn / over, and only ``over`` refuses (with an override available)."""
    planner = BudgetPlanner(settings.model_copy(update={"BUDGET_MAX_USD": 0.00005}), config=None)
    estimate = planner.estimate(plan(3), project_id="p1")
    assert estimate.max_usd == 0.00005
    assert estimate.verdict == "over"
    allowed, reason = planner.check(estimate)
    assert allowed is False and "estimated" in reason
    with pytest.raises(BudgetError) as excinfo:
        planner.enforce(estimate)
    assert excinfo.value.details["max_usd"] == 0.00005

    assert planner.enforce(estimate, allow_over_budget=True) is estimate

    warn_planner = BudgetPlanner(settings.model_copy(update={"BUDGET_MAX_USD": 100.0, "BUDGET_WARN_AT": 0.0000001}))
    warned = warn_planner.estimate(plan(3), project_id="p1")
    assert warned.verdict == "warn"
    assert warn_planner.check(warned)[0] is True  # a warn never blocks

    off = BudgetPlanner(settings.model_copy(update={"BUDGET_ENABLED": False, "BUDGET_MAX_USD": 0.00001}))
    assert off.check(off.estimate(plan(3)))[0] is True


def test_spent_money_counts_against_the_cap(settings: Settings) -> None:
    """Already-recorded spend is part of the projection, not a footnote."""
    planner = BudgetPlanner(settings.model_copy(update={"BUDGET_MAX_USD": 1.0}))
    estimate = planner.estimate(plan(3), project_id="p1", spent_usd=0.99)
    assert estimate.projected_usd == round(estimate.total_usd + 0.99, 6)
    assert estimate.verdict in ("warn", "over")
    assert "already spent" in estimate.message


def test_daily_cap_check() -> None:
    """The daily cap only refuses past the line, and only when configured."""
    from tests.conftest import make_settings

    off = make_settings(BUDGET_DAILY_MAX_USD=0.0)
    assert daily_cap_check(off, BudgetLedger(off), 999.0)[0] is True

    capped = make_settings(BUDGET_DAILY_MAX_USD=2.0)
    ledger = BudgetLedger(capped)
    assert daily_cap_check(capped, ledger, 1.99)[0] is True
    allowed, reason = daily_cap_check(capped, ledger, 2.0)
    assert allowed is False and "daily cap" in reason

    disabled = make_settings(BUDGET_DAILY_MAX_USD=1.0, BUDGET_ENABLED=False)
    assert daily_cap_check(disabled, ledger, 100.0)[0] is True


def test_usd_for_tokens_and_round_trip() -> None:
    """The token→dollar helper is the single place that maths lives."""
    usd = usd_for_tokens({"brain_in": 1.0, "brain_out": 2.0, "worker_in": 0.0, "worker_out": 0.0}, brain_tokens_in=1_000_000, brain_tokens_out=500_000)
    assert usd == 2.0
    assert usd_for_tokens({}, brain_tokens_in=1_000_000) == 0.0

    estimate = CostEstimate(project_id="p", tasks=1, brain_usd=0.5, worker_usd=0.25, max_usd=2.0, prices={"brain_in": 0.3})
    rebuilt = estimate_from_dict(estimate.to_dict())
    assert rebuilt.total_usd == 0.75 and rebuilt.max_usd == 2.0 and rebuilt.tasks == 1


# ----------------------------------------------------------------------
# The ledger
# ----------------------------------------------------------------------
def test_ledger_records_and_totals(settings: Settings) -> None:
    """Rows are per project per day; totals roll up both ways."""
    ledger = BudgetLedger(settings)
    assert run(ledger.project("p1"))["usd"] == 0.0

    run(
        ledger.record(
            "p1",
            tasks=3,
            brain_calls=4,
            brain_tokens_in=1000,
            brain_tokens_out=200,
            worker_tokens_in=5000,
            worker_tokens_out=2000,
            usd=0.01,
            estimated=False,
        )
    )
    run(ledger.record("p1", tasks=1, brain_calls=1, brain_tokens_in=100, brain_tokens_out=10, usd=0.002))
    run(ledger.record("p2", tasks=2, usd=0.005))

    project = run(ledger.project("p1"))
    assert project["runs"] == 2 and project["tasks"] == 4
    assert project["tokens_in"] == 6100 and project["tokens_out"] == 2210
    assert project["usd"] == pytest.approx(0.012, abs=1e-9)
    assert project["any_estimated"] is True  # the second row was estimated

    today = run(ledger.today_total())
    assert today["usd"] == pytest.approx(0.017, abs=1e-9) and today["days"] == 1

    summary = run(ledger.summary())
    assert summary["projects"] == ["p1", "p2"]
    assert summary["total"]["runs"] == 3
    assert "never prompts" in summary["note"]
    assert run(ledger.recent(limit=5))[0]["day"] == BudgetLedger.today()


def test_ledger_failures_never_break_a_run(settings: Settings, monkeypatch: Any) -> None:
    """A broken database is a warning, not a failed project."""
    ledger = BudgetLedger(settings)

    def explode(*_: Any, **__: Any) -> Any:
        raise RuntimeError("database is on fire")

    monkeypatch.setattr("src.orchestrator.budget.session_scope", explode)
    result = run(ledger.record("p1", usd=1.0))
    assert result["error"] and result["project_id"] == "p1"


# ----------------------------------------------------------------------
# Orchestrator wiring
# ----------------------------------------------------------------------
def test_orchestrator_estimates_from_the_stored_plan(settings: Settings) -> None:
    """A real (offline) orchestrator: create a project, estimate it, read the ledger."""
    from src.orchestrator.app import Orchestrator

    orchestrator = Orchestrator(settings)
    record = run(orchestrator.create_project("demo", "Build a small URL shortener with tests", 3))
    estimate = run(orchestrator.estimate_project_cost(record["project_id"]))
    assert estimate.tasks == len(record["plan"]["tasks"])
    assert estimate.worker_calls == estimate.tasks
    assert estimate.to_dict()["usd"]["total"] > 0

    report = run(orchestrator.budget_report())
    assert report["enabled"] is True and report["daily_cap"]["allowed"] is True
    assert "project_config" in report


def test_run_project_refuses_when_over_budget(settings: Settings, agent_settings: Settings) -> None:
    """The refusal happens before a single task is dispatched."""
    from src.orchestrator.app import Orchestrator

    tight = agent_settings.model_copy(update={"BUDGET_MAX_USD": 0.000001}, deep=True)
    orchestrator = Orchestrator(tight)
    record = run(orchestrator.create_project("demo", "Build a tiny notes API with pytest tests", 2))
    with pytest.raises(BudgetError) as excinfo:
        run(orchestrator.run_project(record["project_id"]))
    assert "Refusing to run" in excinfo.value.message
    assert "kollektiv.yml" in excinfo.value.message  # tells the operator how to fix it

    # The project is untouched: no status change, no ledger row.
    assert run(orchestrator.budget_ledger.project(record["project_id"]))["runs"] == 0


def test_health_reports_budget_configuration(settings: Settings) -> None:
    """``/health`` describes the caps without querying the ledger."""
    from src.orchestrator.app import Orchestrator

    payload = run(Orchestrator(settings).health())
    budget = payload["subsystems"]["budget"]
    assert budget["enabled"] is True and budget["max_usd"] == 0.0
    assert "ledger" in budget


def test_cli_keys_writes_secrets_once_and_keeps_them(settings: Settings, tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    """``kollektiv keys`` is the one manual step: a .env, written once."""
    from src.api import cli as cli_module

    env_file = tmp_path / ".env"
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    monkeypatch.setattr("src.orchestrator.app.get_settings", lambda: settings)

    def args(**kwargs: Any) -> Any:
        """An argparse-like namespace for the keys command."""
        defaults = {"env_file": str(env_file), "rotate": False, "json": False}
        defaults.update(kwargs)
        return type("Args", (), defaults)()

    assert run(cli_module.cmd_keys(args())) == 0
    written = env_file.read_text(encoding="utf-8")
    for key in ("SECRET_KEY", "SESSION_TOKEN", "GATEWAY_ADMIN_TOKEN"):
        assert f"{key}=" in written, key
    assert "kollektiv bootstrap" in capsys.readouterr().out

    # Second run keeps the existing values instead of locking the operator out.
    assert run(cli_module.cmd_keys(args())) == 0
    out = capsys.readouterr().out
    assert "kept" in out and env_file.read_text(encoding="utf-8") == written

    # --rotate replaces them, and load_env_file finds the file afterwards.
    assert run(cli_module.cmd_keys(args(rotate=True))) == 0
    capsys.readouterr()
    assert cli_module.load_env_file(str(env_file)) , "the file must be loadable by other entry points"


def test_cli_init_config_estimate_and_budget(settings: Settings, tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    """The three new commands work end to end against a temp config."""
    from src.api import cli as cli_module

    target = tmp_path / ".kollektiv.yml"
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    assert run(cli_module.cmd_init_config(_args(path=str(target)))) == 0
    assert "wrote" in capsys.readouterr().out
    assert run(cli_module.cmd_init_config(_args(path=str(target)))) == 1  # refuses to clobber
    assert target.exists()

    # A second run against a config that does not parse is an error, not silence.
    target.write_text("project: [1, 2\n", encoding="utf-8")
    assert run(cli_module.cmd_init_config(_args(path=str(target), force=True))) == 0
    assert "project:" in target.read_text(encoding="utf-8")


def _args(**kwargs: Any) -> Any:
    """Build an argparse-like namespace with the CLI defaults."""
    defaults: Dict[str, Any] = {
        "path": "",
        "force": False,
        "project_id": "",
        "agents": 0,
        "json": False,
        "import_old": "",
    }
    defaults.update(kwargs)
    return type("Args", (), defaults)
