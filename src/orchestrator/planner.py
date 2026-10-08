"""Turn a project brief into an executable, dependency-aware plan.

The planner is a thin, testable wrapper around
:meth:`OrchestratorBrain.split_task`. It owns the plan *shape* (ids,
priorities, dependency graph, execution waves) and the replanning logic used
when tasks fail.

Usage::

    planner = Planner(brain)
    plan = await planner.create_plan("Build a URL shortener", n_agents=3)
    for wave in plan["waves"]:
        ...
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set

from config.settings import Settings, get_settings
from src.orchestrator.brain import OrchestratorBrain
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)


class Planner:
    """Create and maintain task plans.

    Args:
        brain: The orchestration brain used for task decomposition.
        settings: Optional settings override.
    """

    def __init__(self, brain: OrchestratorBrain, settings: Optional[Settings] = None) -> None:
        self.brain = brain
        self.settings = settings or get_settings()
        self.revision = 0

    # ------------------------------------------------------------------
    # Plan creation
    # ------------------------------------------------------------------
    async def create_plan(self, project_description: str, n_agents: int = 3) -> Dict[str, Any]:
        """Split a brief into subtasks and build the full plan.

        Args:
            project_description: What to build.
            n_agents: How many worker agents will execute the plan.

        Returns:
            A plan dict::

                {
                  "project_name": str,
                  "description": str,
                  "n_agents": int,
                  "revision": int,
                  "tasks": [...],
                  "waves": [[task_id, ...], ...],
                  "critical_path": [task_id, ...],
                  "status": "planned"
                }

        Raises:
            ValueError: When the description is empty.
        """
        if not project_description or not project_description.strip():
            raise ValueError("project_description must not be empty")

        count = max(1, min(int(n_agents), int(self.settings.MAX_AGENT_COUNT)))
        subtasks = await self.brain.split_task(project_description, count)
        self.revision += 1

        tasks = [self._normalise_task(task, index) for index, task in enumerate(subtasks, start=1)]
        self._resolve_dependencies(tasks)
        waves = self.execution_waves(tasks)

        plan = {
            "project_name": self.derive_project_name(project_description),
            "description": project_description.strip(),
            "n_agents": count,
            "revision": self.revision,
            "tasks": tasks,
            "waves": waves,
            "critical_path": self.critical_path(tasks),
            "status": "planned",
            "created_tasks": len(tasks),
        }
        LOGGER.info(
            "Created plan revision %s: %s tasks in %s wave(s)",
            self.revision,
            len(tasks),
            len(waves),
        )
        return plan

    async def replan(
        self,
        state: Dict[str, Any],
        failed_tasks: Sequence[Dict[str, Any]],
        max_new_tasks: int = 3,
    ) -> Dict[str, Any]:
        """Adjust the plan after failures.

        The strategy is deliberately conservative:

        1. Keep every task that is not failed or blocked.
        2. Unblock dependants of *completed* tasks.
        3. For each failed task, ask the brain for a corrective subtask
           (falling back to a deterministic repair task).

        Args:
            state: Current project state (``tasks`` are merged in).
            failed_tasks: Tasks whose status is ``failed``.
            max_new_tasks: Cap on corrective tasks generated per replan.

        Returns:
            The updated plan dict (same shape as :meth:`create_plan`).
        """
        existing = self._tasks_from_state(state)
        known_ids = {str(task.get("id")) for task in existing}
        completed = {str(task.get("id")) for task in existing if str(task.get("status")) == "completed"}

        new_tasks: List[Dict[str, Any]] = []
        brief = state.get("description") or state.get("project_name") or "the project"

        for index, failed in enumerate(failed_tasks[: max(0, max_new_tasks)], start=1):
            task_id = str(failed.get("id") or f"failed-{index}")
            corrective = await self._corrective_task(brief, failed, state)
            corrective["id"] = self._unique_id(f"{task_id}-fix", known_ids)
            known_ids.add(str(corrective["id"]))
            # A corrective task waits on whatever the failed task depended on.
            corrective["dependencies"] = [
                str(dep) for dep in (failed.get("dependencies") or []) if str(dep) in known_ids or dep in completed
            ]
            new_tasks.append(corrective)

        merged = list(existing) + new_tasks
        # Drop dangling dependencies now that new ids exist.
        valid_ids = {str(task.get("id")) for task in merged}
        for task in merged:
            deps = task.get("dependencies") or []
            if isinstance(deps, str):
                deps = [dep.strip() for dep in deps.split(",") if dep.strip()]
            task["dependencies"] = [str(dep) for dep in deps if str(dep) in valid_ids and str(dep) != str(task.get("id"))]

        self.revision += 1
        plan = {
            "project_name": state.get("project_name") or "replanned project",
            "description": brief,
            "n_agents": int(state.get("n_agents") or len(existing) or 1),
            "revision": self.revision,
            "tasks": merged,
            "waves": self.execution_waves(merged),
            "critical_path": self.critical_path(merged),
            "status": "replanned",
            "created_tasks": len(new_tasks),
            "resolved_tasks": len(completed),
        }
        LOGGER.info(
            "Replanned (revision %s): +%s corrective task(s), %s already complete",
            self.revision,
            len(new_tasks),
            len(completed),
        )
        return plan

    async def _corrective_task(
        self, brief: str, failed: Dict[str, Any], state: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Ask the brain for a task that repairs ``failed``."""
        error = str(failed.get("error") or "")[:800]
        feedback = str(failed.get("feedback") or "")[:800]
        if self.brain.is_configured:
            prompt = (
                f"A worker failed this task in project '{brief}':\n"
                f"Title: {failed.get('title', '')}\n"
                f"Description: {str(failed.get('description', ''))[:1200]}\n"
                f"Error reported: {error or 'unknown'}\n"
                f"Reviewer feedback: {feedback or 'none'}\n\n"
                'Reply with JSON only: {"title": "...", "description": "concrete corrective '
                'instructions including file paths", "deliverable": "..."}'
            )
            try:
                from src.orchestrator.brain import _loads_lenient

                raw = await self.brain.complete(prompt, json_mode=True, temperature=0.2)
                data = _loads_lenient(raw)
                if isinstance(data, dict) and data.get("title"):
                    return {
                        "title": str(data["title"])[:300],
                        "description": str(data.get("description") or "")[:4000],
                        "deliverable": str(data.get("deliverable") or ""),
                        "dependencies": [],
                        "priority": 5,
                        "status": "pending",
                        "repair_of": failed.get("id"),
                    }
            except Exception as exc:  # noqa: BLE001 - fall back to the template
                LOGGER.warning("Could not generate a corrective task (%s); using the template", exc)

        return {
            "title": f"Repair: {str(failed.get('title') or 'failed task')[:200]}",
            "description": (
                "The previous attempt failed. Re-implement this task from scratch, fixing the "
                f"reported problem.\n\nOriginal task:\n{str(failed.get('description') or '')[:2000]}\n\n"
                f"Reported error:\n{error or 'unknown'}\n\n"
                f"Reviewer feedback:\n{feedback or 'none'}\n\n"
                "Explain in one line what you changed, then emit the complete files."
            ),
            "deliverable": failed.get("deliverable") or "corrected files",
            "dependencies": [],
            "priority": 5,
            "status": "pending",
            "repair_of": failed.get("id"),
        }

    # ------------------------------------------------------------------
    # Graph helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalise_task(task: Dict[str, Any], index: int) -> Dict[str, Any]:
        """Ensure a task dict has every field the dispatcher relies on."""
        dependencies = task.get("dependencies") or []
        if isinstance(dependencies, str):
            dependencies = [dep.strip() for dep in dependencies.split(",") if dep.strip()]
        return {
            "id": str(task.get("id") or f"t{index}"),
            "title": str(task.get("title") or f"Subtask {index}")[:300],
            "description": str(task.get("description") or ""),
            "dependencies": [str(dep) for dep in dependencies],
            "priority": int(task.get("priority") or 3),
            "deliverable": str(task.get("deliverable") or ""),
            "status": str(task.get("status") or "pending"),
            "attempts": int(task.get("attempts") or 0),
            "assigned_agent": task.get("assigned_agent"),
            "score": task.get("score"),
            "feedback": str(task.get("feedback") or ""),
            "error": str(task.get("error") or ""),
        }

    @staticmethod
    def _unique_id(candidate: str, taken: Set[str]) -> str:
        """Return ``candidate`` (or a numbered variant) that is not in ``taken``."""
        if candidate not in taken:
            return candidate
        suffix = 2
        while f"{candidate}-{suffix}" in taken:
            suffix += 1
        return f"{candidate}-{suffix}"

    def _resolve_dependencies(self, tasks: List[Dict[str, Any]]) -> None:
        """Drop self-references and dangling ids, and break dependency cycles."""
        valid = {str(task["id"]) for task in tasks}
        for task in tasks:
            task["dependencies"] = [
                dep for dep in task.get("dependencies", []) if dep in valid and dep != task["id"]
            ]
        # Remove edges that close a cycle.
        graph = {str(task["id"]): set(task.get("dependencies", [])) for task in tasks}
        safe: Dict[str, Set[str]] = {}
        for task_id in graph:
            if self._creates_cycle(task_id, graph, safe, set()):
                LOGGER.warning("Dropping cyclic dependency for task %s", task_id)
                graph[task_id] = set()
            safe[task_id] = set(graph[task_id])
        for task in tasks:
            task["dependencies"] = sorted(graph.get(str(task["id"]), set()))

    @staticmethod
    def _creates_cycle(node: str, graph: Dict[str, Set[str]], done: Dict[str, Set[str]], stack: Set[str]) -> bool:
        """Return ``True`` when following ``node``'s dependencies loops back."""
        if node in stack:
            return True
        stack = stack | {node}
        for dep in graph.get(node, set()):
            if dep in done.get(node, set()):
                continue
            if dep in stack:
                return True
            if Planner._creates_cycle(dep, graph, done, stack):
                return True
        return False

    def execution_waves(self, tasks: Sequence[Dict[str, Any]], max_waves: int = 50) -> List[List[str]]:
        """Group tasks into waves that can run in parallel.

        Wave *n* contains every task whose dependencies all appear in earlier
        waves. Tasks stuck in a dependency cycle are placed in the final wave
        so they still run.

        Args:
            tasks: The task dicts.
            max_waves: Safety bound on wave count.

        Returns:
            A list of waves, each a list of task ids.
        """
        pending = {str(task.get("id")): set(task.get("dependencies") or []) for task in tasks}
        waves: List[List[str]] = []
        placed: Set[str] = set()

        while pending and len(waves) < max_waves:
            ready = sorted(task_id for task_id, deps in pending.items() if deps <= placed)
            if not ready:
                # Cycles (or ids referencing removed tasks): flush the rest.
                LOGGER.warning("Unresolvable dependencies for %s; scheduling them last", sorted(pending))
                waves.append(sorted(pending))
                break
            waves.append(ready)
            placed.update(ready)
            for task_id in ready:
                pending.pop(task_id, None)
        return waves

    def critical_path(self, tasks: Sequence[Dict[str, Any]]) -> List[str]:
        """Return the longest dependency chain (its length drives the ETA).

        Args:
            tasks: The task dicts.

        Returns:
            Task ids from the start of the chain to its end.
        """
        graph: Dict[str, List[str]] = {
            str(task.get("id")): [str(dep) for dep in (task.get("dependencies") or [])] for task in tasks
        }
        best: List[str] = []
        memo: Dict[str, List[str]] = {}

        def longest(node: str, seen: Set[str]) -> List[str]:
            """Longest chain ending at ``node`` (memoised)."""
            if node in memo:
                return memo[node]
            if node in seen:
                return []
            path: List[str] = []
            for dep in graph.get(node, []):
                candidate = longest(dep, seen | {node})
                if len(candidate) > len(path):
                    path = candidate
            result = path + [node]
            memo[node] = result
            return result

        for node in graph:
            chain = longest(node, set())
            if len(chain) > len(best):
                best = chain
        return best

    def blocked_tasks(self, tasks: Sequence[Dict[str, Any]]) -> Dict[str, List[str]]:
        """Map each unfinished task id to the dependencies blocking it.

        Args:
            tasks: The task dicts.

        Returns:
            ``{task_id: [unfinished dependency ids]}`` for blocked tasks only.
        """
        by_id = {str(task.get("id")): task for task in tasks}
        blocking: Dict[str, List[str]] = {}
        for task_id, task in by_id.items():
            if str(task.get("status")) in {"completed", "cancelled"}:
                continue
            waiting = [
                dep
                for dep in (task.get("dependencies") or [])
                if str(by_id.get(dep, {}).get("status", "pending")) != "completed"
            ]
            if waiting:
                blocking[task_id] = waiting
        return blocking

    def next_tasks(self, tasks: Sequence[Dict[str, Any]], limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return the tasks that are ready to run now, in priority order.

        Args:
            tasks: The task dicts.
            limit: Optional cap on the number of tasks returned.

        Returns:
            Ready task dicts sorted by priority (5 = highest) then id.
        """
        by_id = {str(task.get("id")): task for task in tasks}
        ready = []
        for task in tasks:
            if str(task.get("status")) not in {"pending", "failed"}:
                continue
            deps = [str(dep) for dep in (task.get("dependencies") or [])]
            if all(str(by_id.get(dep, {}).get("status")) == "completed" for dep in deps):
                ready.append(task)
        ready.sort(key=lambda task: (-int(task.get("priority") or 3), str(task.get("id"))))
        return ready[:limit] if limit else ready

    # ------------------------------------------------------------------
    # State interop
    # ------------------------------------------------------------------
    @staticmethod
    def _tasks_from_state(state: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Return the task list from a state document, normalised to dicts."""
        raw = state.get("tasks") or []
        tasks: List[Dict[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            tasks.append(Planner._normalise_task(item, index + 1))
        return tasks

    def plan_to_state(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a plan into the fields the state document expects.

        Returns:
            ``{tasks, plan_revision, waves, n_agents}`` ready to merge into the
            state dict passed to :meth:`StateManager.write_state`.
        """
        return {
            "tasks": plan.get("tasks", []),
            "plan_revision": plan.get("revision", 1),
            "waves": plan.get("waves", []),
            "n_agents": plan.get("n_agents", 0),
        }

    def derive_project_name(self, description: str, max_words: int = 6) -> str:
        """Derive a short project name from a brief.

        Args:
            description: The project brief.
            max_words: Maximum words in the generated name.

        Returns:
            A title-cased slug, e.g. ``"Url Shortener Service"``.
        """
        cleaned = " ".join(description.strip().split())
        if not cleaned:
            return "Untitled Project"
        # Prefer the first sentence/line; it usually states the subject.
        first = cleaned.split(".")[0].split("\n")[0]
        words = first.split()[:max_words]
        name = " ".join(words).strip(" .,:;-")
        return name.title() if name else "Untitled Project"


__all__ = ["Planner"]
