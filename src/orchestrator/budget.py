"""Cost estimation, budget caps and the local spend ledger.

Three questions, three answers:

1. **What will this run cost?** :meth:`BudgetPlanner.estimate` turns a plan into
   tokens and money. It is arithmetic on the plan (task count and description
   length), your configured prices, and nothing else — which is why it is honest
   and why it is an *estimate*: it never pretends to know what a model will
   actually generate.
2. **May it run?** :meth:`BudgetPlanner.check` compares the estimate with the
   cap from ``.kollektiv.yml`` (per project) or ``BUDGET_MAX_USD`` and
   ``BUDGET_DAILY_MAX_USD`` (deployment-wide). A refusal is a
   :class:`~src.utils.errors.BudgetError` carrying the numbers, never a silent
   stop.
3. **What did it cost?** :class:`BudgetLedger` records tokens per project per
   day in the same SQLite database as everything else: no analytics, no upload,
   and ``estimated`` says whether the numbers came from a provider's ``usage``
   block or from the model above.

Token heuristics live in settings (``BUDGET_PROMPT_OVERHEAD_TOKENS``,
``BUDGET_OUTPUT_TOKENS_PER_TASK``) so the estimate can be tuned to a workload
instead of trusted blindly. Prices default to a cheap DeepSeek-class model and
are meant to be overridden: they are the operator's numbers, not our quote.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import select

from config.settings import Settings, get_settings
from src.db.models import BudgetRecord, session_scope
from src.utils.errors import BudgetError
from src.utils.logger import get_logger
from src.utils.project_config import ProjectConfig

LOGGER = get_logger(__name__)

#: Rough characters per token for the length-based part of the estimate. English
#: prose and source code both land near four; the estimate rounds up.
CHARS_PER_TOKEN = 4

#: Planner and summary prompts are large and fixed; these are the estimate's
#: floor for brain input, on top of the per-task review.
PLANNER_INPUT_TOKENS = 1800
SUMMARY_INPUT_TOKENS = 1200
SUMMARY_OUTPUT_TOKENS = 500
#: A review reads the task's answer and replies with a short verdict.
REVIEW_INPUT_OVERHEAD = 700
REVIEW_OUTPUT_TOKENS = 200


@dataclass(frozen=True)
class CostEstimate:
    """The estimated cost of running one plan.

    Attributes:
        project_id: The project the estimate is for.
        tasks: Number of subtasks in the plan.
        waves: Number of dependency waves (the dispatcher runs them in order).
        agents: Worker agents expected to be used.
        brain_calls: Brain calls (plan, reviews, summary).
        worker_calls: Worker calls (one per task).
        brain_tokens_in: Estimated brain input tokens.
        brain_tokens_out: Estimated brain output tokens.
        worker_tokens_in: Estimated worker input tokens.
        worker_tokens_out: Estimated worker output tokens.
        brain_usd: Estimated brain cost.
        worker_usd: Estimated worker cost.
        spent_usd: Already-recorded spend for this project, when known.
        max_usd: The cap in force (0 = uncapped).
        warn_at: The fraction of the cap that triggers a warning.
        source: Where the cap came from (``.kollektiv.yml``, settings, none).
        prices: The per-million-token prices used.
    """

    project_id: str = ""
    tasks: int = 0
    waves: int = 0
    agents: int = 0
    brain_calls: int = 0
    worker_calls: int = 0
    brain_tokens_in: int = 0
    brain_tokens_out: int = 0
    worker_tokens_in: int = 0
    worker_tokens_out: int = 0
    brain_usd: float = 0.0
    worker_usd: float = 0.0
    spent_usd: float = 0.0
    max_usd: float = 0.0
    warn_at: float = 0.8
    source: str = "none"
    prices: Dict[str, float] = field(default_factory=dict)

    @property
    def total_usd(self) -> float:
        """Estimated cost of the whole run, in US dollars."""
        return round(self.brain_usd + self.worker_usd, 6)

    @property
    def total_tokens(self) -> int:
        """Estimated tokens across brain and workers."""
        return self.brain_tokens_in + self.brain_tokens_out + self.worker_tokens_in + self.worker_tokens_out

    @property
    def projected_usd(self) -> float:
        """Estimated cost including what this project has already spent."""
        return round(self.total_usd + self.spent_usd, 6)

    @property
    def remaining_usd(self) -> Optional[float]:
        """Headroom under the cap, or ``None`` when uncapped."""
        if self.max_usd <= 0:
            return None
        return round(self.max_usd - self.projected_usd, 6)

    @property
    def verdict(self) -> str:
        """``ok``, ``warn`` (near the cap) or ``over`` (refused)."""
        if self.max_usd <= 0:
            return "ok"
        if self.projected_usd > self.max_usd:
            return "over"
        if self.max_usd > 0 and self.projected_usd >= self.max_usd * self.warn_at:
            return "warn"
        return "ok"

    @property
    def message(self) -> str:
        """One sentence an operator can act on."""
        if self.max_usd <= 0:
            return f"estimated ${self.total_usd:.4f} for {self.tasks} task(s); no cap configured"
        return (
            f"estimated ${self.total_usd:.4f} for {self.tasks} task(s)"
            f" (${self.projected_usd:.4f} including ${self.spent_usd:.4f} already spent)"
            f" against a ${self.max_usd:.2f} cap from {self.source}"
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the estimate as JSON."""
        return {
            "project_id": self.project_id,
            "tasks": self.tasks,
            "waves": self.waves,
            "agents": self.agents,
            "calls": {"brain": self.brain_calls, "worker": self.worker_calls},
            "tokens": {
                "brain_in": self.brain_tokens_in,
                "brain_out": self.brain_tokens_out,
                "worker_in": self.worker_tokens_in,
                "worker_out": self.worker_tokens_out,
                "total": self.total_tokens,
            },
            "usd": {
                "brain": round(self.brain_usd, 6),
                "worker": round(self.worker_usd, 6),
                "total": self.total_usd,
                "spent": round(self.spent_usd, 6),
                "projected": self.projected_usd,
                "remaining": self.remaining_usd,
                "max": self.max_usd,
            },
            "prices_per_mtok": dict(self.prices),
            "cap_source": self.source,
            "verdict": self.verdict,
            "message": self.message,
            "estimate_only": True,
        }


class BudgetPlanner:
    """Turn plans into cost estimates, and estimates into yes/no decisions."""

    def __init__(self, settings: Optional[Settings] = None, config: Optional[ProjectConfig] = None) -> None:
        """Store the settings and the project config.

        Args:
            settings: Optional settings override.
            config: Optional ``.kollektiv.yml`` (used for the per-project cap).
        """
        self.settings = settings or get_settings()
        self.config = config

    def prices(self) -> Dict[str, float]:
        """Return the per-million-token prices used by the estimate."""
        return {
            "brain_in": float(self.settings.BUDGET_PRICE_IN_PER_MTOK),
            "brain_out": float(self.settings.BUDGET_PRICE_OUT_PER_MTOK),
            "worker_in": float(self.settings.BUDGET_WORKER_PRICE_IN_PER_MTOK),
            "worker_out": float(self.settings.BUDGET_WORKER_PRICE_OUT_PER_MTOK),
        }

    def cap(self) -> Tuple[float, str]:
        """Return the per-project cap and where it came from.

        Returns:
            ``(usd, source)``. A ``.kollektiv.yml`` cap wins over the environment,
            because the project's own file is the most specific statement of
            intent; ``0`` and ``"none"`` mean uncapped.
        """
        if self.config is not None and self.config.budget_max_usd is not None:
            return float(self.config.budget_max_usd), str(self.config.path.name if self.config.path else ".kollektiv.yml")
        if float(self.settings.BUDGET_MAX_USD) > 0:
            return float(self.settings.BUDGET_MAX_USD), "BUDGET_MAX_USD"
        return 0.0, "none"

    def warn_at(self) -> float:
        """Return the warning fraction, from the project config or settings."""
        if self.config is not None and self.config.budget_warn_at is not None:
            return float(self.config.budget_warn_at)
        return float(self.settings.BUDGET_WARN_AT)

    def estimate(
        self,
        plan: Dict[str, Any],
        *,
        project_id: str = "",
        n_agents: int = 0,
        spent_usd: float = 0.0,
    ) -> CostEstimate:
        """Estimate what running ``plan`` will cost.

        Args:
            plan: A plan document (``tasks``, ``waves``, …).
            project_id: Project identifier, for the result and the log line.
            n_agents: Worker agents expected (defaults to the plan's own count).
            spent_usd: Already-recorded spend for this project.

        Returns:
            The :class:`CostEstimate`. It is always computable: an empty plan
            estimates the planner call alone.
        """
        tasks = [task for task in (plan.get("tasks") or []) if isinstance(task, dict)]
        waves = len(plan.get("waves") or []) or (1 if tasks else 0)
        agents = int(n_agents or plan.get("n_agents") or len(tasks) or 1)

        overhead = int(self.settings.BUDGET_PROMPT_OVERHEAD_TOKENS)
        per_task_out = int(self.settings.BUDGET_OUTPUT_TOKENS_PER_TASK)

        worker_in = 0
        for task in tasks:
            text = f"{task.get('title', '')} {task.get('description', '')}"
            worker_in += overhead + max(0, len(text) // CHARS_PER_TOKEN)
        worker_out = len(tasks) * per_task_out

        brain_in = PLANNER_INPUT_TOKENS + SUMMARY_INPUT_TOKENS + len(tasks) * REVIEW_INPUT_OVERHEAD
        # Every review reads the worker's answer.
        brain_in += worker_out // 2
        brain_out = SUMMARY_OUTPUT_TOKENS + len(tasks) * REVIEW_OUTPUT_TOKENS

        prices = self.prices()
        brain_usd = (brain_in / 1_000_000) * prices["brain_in"] + (brain_out / 1_000_000) * prices["brain_out"]
        worker_usd = (worker_in / 1_000_000) * prices["worker_in"] + (worker_out / 1_000_000) * prices["worker_out"]
        cap, source = self.cap()

        estimate = CostEstimate(
            project_id=project_id,
            tasks=len(tasks),
            waves=waves,
            agents=agents,
            brain_calls=2 + len(tasks),
            worker_calls=len(tasks),
            brain_tokens_in=brain_in,
            brain_tokens_out=brain_out,
            worker_tokens_in=worker_in,
            worker_tokens_out=worker_out,
            brain_usd=brain_usd,
            worker_usd=worker_usd,
            spent_usd=round(float(spent_usd), 6),
            max_usd=cap,
            warn_at=self.warn_at(),
            source=source,
            prices=prices,
        )
        LOGGER.debug("Cost estimate for %s: %s", project_id or "<new>", estimate.message)
        return estimate

    def check(self, estimate: CostEstimate) -> Tuple[bool, str]:
        """Decide whether the estimate may run.

        Args:
            estimate: The estimate to judge.

        Returns:
            ``(allowed, reason)``. Only the caps refuse; a ``warn`` verdict is
            allowed and logged, because a warn that blocks would be a cap.
        """
        if not self.settings.BUDGET_ENABLED:
            return True, "budget enforcement is disabled (BUDGET_ENABLED=false)"
        if estimate.verdict == "over":
            return False, estimate.message
        if estimate.verdict == "warn":
            LOGGER.warning("Project %s is close to its budget cap: %s", estimate.project_id, estimate.message)
        return True, estimate.message

    def enforce(self, estimate: CostEstimate, *, allow_over_budget: bool = False) -> CostEstimate:
        """Raise when a run must not start.

        Args:
            estimate: The estimate to judge.
            allow_over_budget: The operator's explicit override.

        Returns:
            The estimate when the run may proceed.

        Raises:
            BudgetError: When a cap is exceeded and the override was not given.
        """
        allowed, reason = self.check(estimate)
        if allowed or allow_over_budget:
            if not allowed:
                LOGGER.warning("Budget cap overridden for %s: %s", estimate.project_id, reason)
            return estimate
        raise BudgetError(
            f"Refusing to run {estimate.project_id or 'this project'}: {reason}. "
            "Raise the cap in .kollektiv.yml (budget.max_usd), set BUDGET_MAX_USD, "
            "or pass allow_over_budget to proceed anyway.",
            estimated_usd=estimate.total_usd,
            projected_usd=estimate.projected_usd,
            max_usd=estimate.max_usd,
            cap_source=estimate.source,
        )


class BudgetLedger:
    """The local spend ledger: what each project used, per day, in SQLite."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        """Store the settings; no I/O happens here.

        Args:
            settings: Optional settings override.
        """
        self.settings = settings or get_settings()

    @staticmethod
    def today() -> str:
        """Return today's UTC date as ``YYYY-MM-DD``."""
        return datetime.now(UTC).strftime("%Y-%m-%d")

    async def record(
        self,
        project_id: str,
        *,
        runs: int = 1,
        tasks: int = 0,
        brain_calls: int = 0,
        brain_tokens_in: int = 0,
        brain_tokens_out: int = 0,
        worker_tokens_in: int = 0,
        worker_tokens_out: int = 0,
        usd: float = 0.0,
        estimated: bool = True,
    ) -> Dict[str, Any]:
        """Add one run's usage to the ledger.

        Args:
            project_id: The project.
            runs: Runs to add (usually 1).
            tasks: Tasks dispatched by this run.
            brain_calls: Brain calls made.
            brain_tokens_in: Brain input tokens (measured or estimated).
            brain_tokens_out: Brain output tokens.
            worker_tokens_in: Worker input tokens (estimated unless the endpoint
                reports usage).
            worker_tokens_out: Worker output tokens.
            usd: Cost in dollars.
            estimated: Whether the token counts are estimates.

        Returns:
            The updated row as a dict. A ledger failure is logged, never raised:
            a run must not fail because bookkeeping did.
        """
        day = self.today()
        try:
            with session_scope() as session:
                row = session.get(BudgetRecord, (project_id, day))
                if row is None:
                    row = BudgetRecord(project_id=project_id, day=day)
                row.runs += int(runs)
                row.tasks += int(tasks)
                row.brain_calls += int(brain_calls)
                row.brain_tokens_in += int(brain_tokens_in)
                row.brain_tokens_out += int(brain_tokens_out)
                row.worker_tokens_in += int(worker_tokens_in)
                row.worker_tokens_out += int(worker_tokens_out)
                row.usd = round(float(row.usd) + float(usd), 8)
                # OR, not AND: the flag means "this row contains at least one
                # estimate", so nobody reads a mixed row as measured.
                row.estimated = bool(row.estimated or estimated)
                row.updated_at = datetime.now(UTC)
                session.add(row)
                session.commit()
                session.refresh(row)
            return self._row(row)
        except Exception as exc:  # noqa: BLE001 - bookkeeping is best effort
            LOGGER.warning("Could not write the budget ledger for %s: %s", project_id, exc)
            return {"project_id": project_id, "day": day, "error": str(exc)}

    async def project(self, project_id: str) -> Dict[str, Any]:
        """Return the totals recorded for one project.

        Args:
            project_id: The project.

        Returns:
            ``{project_id, runs, tasks, tokens, usd, days}`` (zeroed when unknown).
        """
        with session_scope() as session:
            rows = list(session.exec(select(BudgetRecord).where(BudgetRecord.project_id == project_id)).all())
        return self._totals(project_id, rows)

    async def today_total(self) -> Dict[str, Any]:
        """Return the totals recorded for today across every project."""
        with session_scope() as session:
            rows = list(session.exec(select(BudgetRecord).where(BudgetRecord.day == self.today())).all())
        return self._totals("<today>", rows)

    async def recent(self, limit: int = 30) -> List[Dict[str, Any]]:
        """Return recent ledger rows, newest day first.

        Args:
            limit: Maximum rows.

        Returns:
            The rows as dicts.
        """
        with session_scope() as session:
            rows = list(session.exec(select(BudgetRecord)).all())
        rows.sort(key=lambda row: (row.day, row.updated_at or datetime.min.replace(tzinfo=UTC)), reverse=True)
        return [self._row(row) for row in rows[: max(1, int(limit))]]

    async def summary(self) -> Dict[str, Any]:
        """Return the whole ledger as one payload (for ``/health`` and the CLI)."""
        with session_scope() as session:
            rows = list(session.exec(select(BudgetRecord)).all())
        total = self._totals("<all>", rows)
        today = [row for row in rows if row.day == self.today()]
        return {
            "enabled": bool(self.settings.BUDGET_ENABLED),
            "max_usd": float(self.settings.BUDGET_MAX_USD),
            "daily_max_usd": float(self.settings.BUDGET_DAILY_MAX_USD),
            "prices_per_mtok": {
                "brain_in": float(self.settings.BUDGET_PRICE_IN_PER_MTOK),
                "brain_out": float(self.settings.BUDGET_PRICE_OUT_PER_MTOK),
                "worker_in": float(self.settings.BUDGET_WORKER_PRICE_IN_PER_MTOK),
                "worker_out": float(self.settings.BUDGET_WORKER_PRICE_OUT_PER_MTOK),
            },
            "total": total,
            "today": self._totals("<today>", today),
            "projects": sorted({row.project_id for row in rows}),
            "note": "local ledger only: tokens and dollars, never prompts, files or identifiers",
        }

    @staticmethod
    def _row(row: BudgetRecord) -> Dict[str, Any]:
        """Render one ledger row as JSON."""
        return {
            "project_id": row.project_id,
            "day": row.day,
            "runs": row.runs,
            "tasks": row.tasks,
            "brain_calls": row.brain_calls,
            "tokens": {
                "brain_in": row.brain_tokens_in,
                "brain_out": row.brain_tokens_out,
                "worker_in": row.worker_tokens_in,
                "worker_out": row.worker_tokens_out,
            },
            "usd": round(row.usd, 6),
            "estimated": row.estimated,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    @staticmethod
    def _totals(label: str, rows: List[BudgetRecord]) -> Dict[str, Any]:
        """Sum a set of ledger rows.

        Args:
            label: What the totals describe (a project id or a marker).
            rows: The rows.

        Returns:
            The summed totals.
        """
        return {
            "scope": label,
            "runs": sum(row.runs for row in rows),
            "tasks": sum(row.tasks for row in rows),
            "brain_calls": sum(row.brain_calls for row in rows),
            "tokens_in": sum(row.brain_tokens_in + row.worker_tokens_in for row in rows),
            "tokens_out": sum(row.brain_tokens_out + row.worker_tokens_out for row in rows),
            "usd": round(sum(row.usd for row in rows), 6),
            "any_estimated": any(row.estimated for row in rows),
            "days": len({row.day for row in rows}),
        }


def usd_for_tokens(
    prices: Dict[str, float],
    *,
    brain_tokens_in: int = 0,
    brain_tokens_out: int = 0,
    worker_tokens_in: int = 0,
    worker_tokens_out: int = 0,
) -> float:
    """Convert token counts to dollars with a price table.

    Args:
        prices: Per-million-token prices (see :meth:`BudgetPlanner.prices`).
        brain_tokens_in: Brain input tokens.
        brain_tokens_out: Brain output tokens.
        worker_tokens_in: Worker input tokens.
        worker_tokens_out: Worker output tokens.

    Returns:
        The cost in US dollars, rounded to eight decimals.
    """
    total = (
        brain_tokens_in * prices.get("brain_in", 0.0)
        + brain_tokens_out * prices.get("brain_out", 0.0)
        + worker_tokens_in * prices.get("worker_in", 0.0)
        + worker_tokens_out * prices.get("worker_out", 0.0)
    ) / 1_000_000
    return round(total, 8)


def daily_cap_check(settings: Settings, ledger: BudgetLedger, todays_usd: float) -> Tuple[bool, str]:
    """Decide whether today's spend already forbids another run.

    Args:
        settings: The settings carrying ``BUDGET_DAILY_MAX_USD``.
        ledger: The ledger (kept in the signature so callers read as one story).
        todays_usd: Already-recorded spend for today.

    Returns:
        ``(allowed, reason)``.
    """
    cap = float(settings.BUDGET_DAILY_MAX_USD)
    if not settings.BUDGET_ENABLED or cap <= 0:
        return True, "no daily cap configured"
    if todays_usd >= cap:
        return False, f"today's recorded spend ${todays_usd:.4f} has reached the daily cap ${cap:.2f}"
    _ = ledger  # the ledger produced the number; kept for a single call site
    return True, f"today: ${todays_usd:.4f} of ${cap:.2f}"


def estimate_from_dict(payload: Dict[str, Any]) -> CostEstimate:
    """Rebuild an estimate from :meth:`CostEstimate.to_dict` output.

    Used by the API and the gateway, which pass estimates around as JSON.

    Args:
        payload: The serialised estimate.

    Returns:
        The estimate (missing sections default to zero).
    """
    tokens = payload.get("tokens") or {}
    usd = payload.get("usd") or {}
    calls = payload.get("calls") or {}
    return CostEstimate(
        project_id=str(payload.get("project_id") or ""),
        tasks=int(payload.get("tasks") or 0),
        waves=int(payload.get("waves") or 0),
        agents=int(payload.get("agents") or 0),
        brain_calls=int(calls.get("brain") or 0),
        worker_calls=int(calls.get("worker") or 0),
        brain_tokens_in=int(tokens.get("brain_in") or 0),
        brain_tokens_out=int(tokens.get("brain_out") or 0),
        worker_tokens_in=int(tokens.get("worker_in") or 0),
        worker_tokens_out=int(tokens.get("worker_out") or 0),
        brain_usd=float(usd.get("brain") or 0.0),
        worker_usd=float(usd.get("worker") or 0.0),
        spent_usd=float(usd.get("spent") or 0.0),
        max_usd=float(usd.get("max") or 0.0),
        warn_at=float(payload.get("warn_at") or 0.8),
        source=str(payload.get("cap_source") or "none"),
        prices=dict(payload.get("prices_per_mtok") or {}),
    )


def dumps(payload: Any) -> str:
    """Serialise a payload with the error types this module raises.

    Args:
        payload: Anything JSON-able.

    Returns:
        The JSON string.
    """
    return json.dumps(payload, default=str, indent=2)


__all__ = [
    "CHARS_PER_TOKEN",
    "BudgetLedger",
    "BudgetPlanner",
    "CostEstimate",
    "daily_cap_check",
    "dumps",
    "estimate_from_dict",
    "usd_for_tokens",
]
