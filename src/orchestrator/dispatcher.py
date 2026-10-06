"""Execute a plan: schedule tasks onto agents, retry, review and record.

The dispatcher walks the plan's execution waves. Within a wave, independent
tasks run in parallel; a task whose dependencies failed is skipped with a
``blocked`` result instead of being sent to a worker.

For every task it:

1. generates context with the brain,
2. sends the task to an agent (:class:`~src.agents.agent_pool.AgentPool`),
3. collects and parses the output (:class:`~src.orchestrator.collector.Collector`),
4. asks the brain to review it and retries once when the review fails,
5. writes the outcome into SQLite and ``PROJECT_STATE.md``.

Usage::

    dispatcher = Dispatcher(pool, brain, state)
    results = await dispatcher.dispatch(plan, project_id="prj_123")
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config.settings import Settings, get_settings
from src.agents.agent_pool import AgentPool, AgentTaskError
from src.db.models import Task, session_scope, utcnow
from src.orchestrator.brain import OrchestratorBrain
from src.orchestrator.collector import Collector
from src.storage.state_manager import StateManager
from src.utils.errors import ConfigurationError, KollektivError
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)


class Dispatcher:
    """Schedule plan tasks onto the agent pool.

    Args:
        pool: The agent pool.
        brain: The orchestration brain (context + review).
        state: The shared project state.
        collector: Optional collector; one is created per project on demand.
        settings: Optional settings override.
    """

    def __init__(
        self,
        pool: AgentPool,
        brain: OrchestratorBrain,
        state: StateManager,
        collector: Optional[Collector] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self.pool = pool
        self.brain = brain
        self.state = state
        self.settings = settings or get_settings()
        self._collector = collector
        self.max_concurrency = max(1, int(self.settings.DEFAULT_AGENT_COUNT))
        self.max_task_attempts = 2
        self.review_threshold = 0.6
        self.dispatch_count = 0
        self.last_error: str = ""

    # ------------------------------------------------------------------
    # Collector access
    # ------------------------------------------------------------------
    def collector_for(self, project_id: str) -> Collector:
        """Return the collector for ``project_id`` (cached when injected)."""
        if self._collector is not None:
            return self._collector
        self._collector = Collector(settings=self.settings, project_id=project_id, write_files=True)
        return self._collector

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    async def dispatch(self, plan: Dict[str, Any], project_id: str = "") -> List[Dict[str, Any]]:
        """Run every task in the plan, respecting dependencies.

        Args:
            plan: The plan dict from :class:`~src.orchestrator.planner.Planner`.
            project_id: Project identifier used for state and task rows.

        Returns:
            One result dict per task::

                {task_id, title, success, agent_id, output, result,
                 review, attempts, error, blocked_by}
        """
        tasks = [dict(task) for task in (plan.get("tasks") or [])]
        if not tasks:
            LOGGER.warning("Nothing to dispatch: the plan has no tasks")
            return []

        if not self.pool.is_configured():
            raise ConfigurationError(
                "Cannot dispatch: no worker agents are configured (ARENA_ACCOUNTS is empty)."
            )

        waves = plan.get("waves") or self._waves(tasks)
        LOGGER.info(
            "Dispatching %s task(s) for project %s in %s wave(s)",
            len(tasks),
            project_id or "-",
            len(waves),
        )
        await self._update_state(project_id, {"status": "running"})

        results: Dict[str, Dict[str, Any]] = {}
        for index, wave in enumerate(waves, start=1):
            LOGGER.info("Wave %s/%s: %s", index, len(waves), ", ".join(wave))
            wave_tasks = [task for task in tasks if str(task.get("id")) in set(wave)]
            wave_results = await self.dispatch_parallel(wave_tasks, project_id=project_id, completed=results)
            for result in wave_results:
                results[str(result.get("task_id"))] = result

        # Preserve the plan order in the returned list.
        ordered = [results[str(task.get("id"))] for task in tasks if str(task.get("id")) in results]
        completed = len([result for result in ordered if result.get("success")])
        await self._update_state(
            project_id,
            {
                "status": "completed" if completed == len(ordered) else "partially_completed",
                "dispatch_summary": {
                    "total": len(ordered),
                    "completed": completed,
                    "failed": len(ordered) - completed,
                    "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
                },
            },
        )
        self.dispatch_count += 1
        return ordered

    async def dispatch_parallel(
        self,
        tasks: Sequence[Dict[str, Any]],
        project_id: str = "",
        completed: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Run the given tasks concurrently (bounded by ``max_concurrency``).

        Args:
            tasks: Task dicts that are safe to run together.
            project_id: Project identifier.
            completed: Results of already finished tasks, used to detect blocked
                dependencies.

        Returns:
            One result dict per task.
        """
        if not tasks:
            return []
        done = completed or {}
        limit = max(1, min(self.max_concurrency, self.pool.size or 1))
        semaphore = asyncio.Semaphore(limit)

        async def guarded(task: Dict[str, Any]) -> Dict[str, Any]:
            """Run one task under the concurrency limit."""
            async with semaphore:
                blockers = self._blocking_dependencies(task, done)
                if blockers:
                    LOGGER.warning("Task %s is blocked by %s", task.get("id"), ", ".join(blockers))
                    await self._record_task_status(
                        project_id,
                        task,
                        status="blocked",
                        error=f"blocked by {', '.join(blockers)}",
                    )
                    return {
                        "task_id": task.get("id"),
                        "title": task.get("title", ""),
                        "success": False,
                        "agent_id": "",
                        "output": "",
                        "result": {},
                        "review": {},
                        "attempts": 0,
                        "error": f"blocked by {', '.join(blockers)}",
                        "blocked_by": blockers,
                    }
                return await self._dispatch_task(task, project_id)

        return list(await asyncio.gather(*(guarded(task) for task in tasks)))

    # ------------------------------------------------------------------
    # Single task
    # ------------------------------------------------------------------
    async def _dispatch_task(self, task: Dict[str, Any], project_id: str) -> Dict[str, Any]:
        """Run one task end to end: context, agent, collect, review, retry."""
        task_id = str(task.get("id"))
        collector = self.collector_for(project_id)
        attempts = 0
        last_error = ""
        last_output = ""
        last_result: Dict[str, Any] = {}
        last_review: Dict[str, Any] = {}
        agent_id = ""

        for attempt in range(1, self.max_task_attempts + 1):
            attempts = attempt
            await self._record_task_status(project_id, task, status="in_progress", attempts=attempt)

            state = await self.state.read_state(project_id)
            state = self._merge_task_view(state, task, status="in_progress")
            context = await self.brain.generate_context_for_agent(
                agent_id=f"agent-{task_id}-{attempt}", task=task, state=state
            )

            try:
                agent = await self.pool.get_available_agent()
                output = await self.pool.assign_task(task, context=context, agent=agent)
                agent_id = agent.account_id if agent is not None else ""
            except AgentTaskError as exc:
                last_error = str(exc)
                LOGGER.error("Task %s failed on every agent: %s", task_id, exc)
                break
            except KollektivError as exc:
                last_error = str(exc)
                LOGGER.error("Task %s failed: %s", task_id, exc)
                break
            except Exception as exc:  # noqa: BLE001 - never let a task kill the run
                last_error = str(exc)
                LOGGER.error("Task %s raised unexpectedly: %s", task_id, exc, exc_info=True)
                break

            last_output = output
            collected = await collector.collect_result(task, output)
            last_result = collected
            review = await self.brain.review_output(task, output)
            last_review = review

            if review.get("needs_retry") and attempt < self.max_task_attempts:
                LOGGER.warning(
                    "Task %s scored %.2f; retrying with reviewer feedback",
                    task_id,
                    float(review.get("score") or 0.0),
                )
                task = dict(task)
                task["description"] = (
                    f"{task.get('description', '')}\n\n"
                    f"## Reviewer feedback from the previous attempt (fix this)\n{review.get('feedback', '')}"
                )
                task["feedback"] = str(review.get("feedback", ""))
                continue

            success = bool(collected.get("success")) and float(review.get("score") or 0.0) >= 0.0
            await self._record_task_status(
                project_id,
                task,
                status="completed" if success else "failed",
                attempts=attempt,
                agent_id=agent_id,
                score=float(review.get("score") or 0.0),
                feedback=str(review.get("feedback", "")),
                result=collected,
                error="" if success else last_error or "review failed",
            )
            await self._record_event(
                project_id,
                {
                    "agent_id": agent_id or task_id,
                    "action": "task_completed" if success else "task_failed",
                    "result": f"{task.get('title', '')}: {collected.get('summary', '')}",
                    "score": review.get("score"),
                },
            )
            return {
                "task_id": task_id,
                "title": task.get("title", ""),
                "success": success,
                "agent_id": agent_id,
                "output": last_output,
                "result": collected,
                "review": review,
                "attempts": attempts,
                "error": "" if success else last_error or "review failed",
                "blocked_by": [],
            }

        # Every attempt failed (or was never sent).
        await self._record_task_status(
            project_id,
            task,
            status="failed",
            attempts=attempts,
            agent_id=agent_id,
            score=float(last_review.get("score") or 0.0) if last_review else None,
            feedback=str(last_review.get("feedback", "")),
            result=last_result,
            error=last_error or "no agent completed the task",
        )
        await self._record_event(
            project_id,
            {
                "agent_id": agent_id or task_id,
                "action": "task_failed",
                "result": f"{task.get('title', '')}: {last_error or 'unknown error'}",
            },
        )
        return {
            "task_id": task_id,
            "title": task.get("title", ""),
            "success": False,
            "agent_id": agent_id,
            "output": last_output,
            "result": last_result,
            "review": last_review,
            "attempts": attempts,
            "error": last_error or "no agent completed the task",
            "blocked_by": [],
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _blocking_dependencies(task: Dict[str, Any], done: Dict[str, Dict[str, Any]]) -> List[str]:
        """Return dependency ids that did not complete successfully."""
        blockers: List[str] = []
        for dep in task.get("dependencies") or []:
            result = done.get(str(dep))
            if result is None or not result.get("success"):
                blockers.append(str(dep))
        return blockers

    @staticmethod
    def _waves(tasks: Sequence[Dict[str, Any]]) -> List[List[str]]:
        """Compute execution waves when the plan does not carry them."""
        pending = {str(task.get("id")): set(task.get("dependencies") or []) for task in tasks}
        placed: set[str] = set()
        waves: List[List[str]] = []
        while pending:
            ready = sorted(task_id for task_id, deps in pending.items() if deps <= placed)
            if not ready:
                waves.append(sorted(pending))
                break
            waves.append(ready)
            placed.update(ready)
            for task_id in ready:
                pending.pop(task_id, None)
        return waves

    @staticmethod
    def _merge_task_view(state: Dict[str, Any], task: Dict[str, Any], status: str) -> Dict[str, Any]:
        """Return the state document with ``task`` shown in the given status."""
        merged = dict(state)
        tasks = [entry for entry in (state.get("tasks") or []) if isinstance(entry, dict)]
        replaced = False
        for index, entry in enumerate(tasks):
            if str(entry.get("id")) == str(task.get("id")):
                merged_entry = {**entry, **task}
                merged_entry["status"] = status
                tasks[index] = merged_entry
                replaced = True
                break
        if not replaced:
            new_entry = dict(task)
            new_entry["status"] = status
            tasks.append(new_entry)
        merged["tasks"] = tasks
        return merged

    async def _record_task_status(
        self,
        project_id: str,
        task: Dict[str, Any],
        status: str,
        attempts: Optional[int] = None,
        agent_id: Optional[str] = None,
        score: Optional[float] = None,
        feedback: str = "",
        result: Optional[Dict[str, Any]] = None,
        error: str = "",
    ) -> None:
        """Persist the task outcome in SQLite and the shared state."""
        import json

        task_id = str(task.get("id"))
        try:
            with session_scope() as session:
                # Composite primary key: (task id, project id) -- see db.models.Task.
                row = session.get(Task, (task_id, project_id))
                if row is None:
                    row = Task(
                        id=task_id,
                        project_id=project_id,
                        title=str(task.get("title") or "")[:300],
                        description=str(task.get("description") or ""),
                        dependencies=json.dumps(task.get("dependencies", [])),
                        priority=int(task.get("priority") or 3),
                    )
                row.status = status
                if attempts is not None:
                    row.attempts = attempts
                if agent_id:
                    row.assigned_agent = agent_id
                if score is not None:
                    row.score = score
                row.feedback = feedback[:4000]
                row.error = error[:2000]
                if result is not None:
                    row.result = json.dumps(
                        {key: value for key, value in result.items() if key != "files"},
                        default=str,
                    )[:20000]
                row.updated_at = utcnow()
                session.add(row)
        except Exception as exc:  # noqa: BLE001 - persistence must not break dispatch
            # Local SQLite writes should not fail; warn so a schema/engine
            # mismatch is visible instead of silently losing task history.
            LOGGER.warning("Could not persist task %s: %s", task_id, exc)

        update = {
            "status": status,
            "last_task": {
                "id": task_id,
                "title": task.get("title", ""),
                "status": status,
                "agent_id": agent_id or "",
                "score": score,
                "error": error[:500],
            },
        }
        await self._update_state(project_id, update, task_view=(task, status))

    async def _update_state(
        self,
        project_id: str,
        fields: Dict[str, Any],
        task_view: Optional[Tuple[Dict[str, Any], str]] = None,
    ) -> None:
        """Merge ``fields`` into the shared state (best effort, never raises)."""
        if not project_id:
            return
        try:
            state = await self.state.read_state(project_id)
            state.update(fields)
            if task_view is not None:
                task, status = task_view
                state = self._merge_task_view(state, task, status)
            await self.state.write_state(state, project_id)
        except Exception as exc:  # noqa: BLE001 - state writes are best effort
            LOGGER.debug("Could not update the state for %s: %s", project_id, exc)

    async def _record_event(self, project_id: str, event: Dict[str, Any]) -> None:
        """Append an event to the shared history and persist the document.

        Events are written through (rather than cached) so a crash mid-run
        still leaves an accurate record of what finished.
        """
        if not project_id:
            return
        try:
            await self.state.append_event(event, project_id=project_id, persist=True)
        except Exception as exc:  # noqa: BLE001 - history is best effort
            LOGGER.debug("Could not record the event: %s", exc)

    def status(self) -> Dict[str, Any]:
        """Return dispatcher status."""
        return {
            "dispatches": self.dispatch_count,
            "max_concurrency": self.max_concurrency,
            "pool_size": self.pool.size,
            "last_error": self.last_error,
        }


__all__ = ["Dispatcher"]
