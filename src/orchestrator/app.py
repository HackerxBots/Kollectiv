"""The Orchestrator: wires every subsystem together and owns the lifecycle.

This is the object the FastAPI app and the MCP server both talk to. It builds
the storage pool, the agent pool, the brain, the planner, the collector and the
sync engine, and exposes the high-level operations the API needs
(:meth:`create_project`, :meth:`run_project`, :meth:`get_status`, ...).

Usage::

    orchestrator = Orchestrator()
    await orchestrator.start()
    project = await orchestrator.create_project("url shortener", "Build ...", 3)
    await orchestrator.run_project(project["project_id"])
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlmodel import col, select

from config.settings import Settings, get_settings
from src.agents.agent_pool import AgentPool
from src.agents.session_manager import SessionManager
from src.connectors.base import ConnectorRegistry
from src.db.models import (
    AgentLinkRecord,
    AgentRecord,
    EventLog,
    PlanRecord,
    Project,
    ProjectFile,
    Task,
    bind_engine,
    get_engine,
    init_db,
    new_id,
    session_scope,
    utcnow,
)
from src.github.github_client import GitHubClient
from src.orchestrator.brain import OrchestratorBrain
from src.orchestrator.budget import BudgetLedger, BudgetPlanner, CostEstimate, daily_cap_check, usd_for_tokens
from src.orchestrator.collector import Collector
from src.orchestrator.dispatcher import Dispatcher
from src.orchestrator.handoff import build_handoff, write_handoff_file
from src.orchestrator.planner import Planner
from src.orchestrator.sync_engine import SyncEngine
from src.sponsors.line import SponsorLineMux
from src.storage.factory import build_storage
from src.storage.state_manager import StateManager
from src.utils.errors import BudgetError, ConfigurationError
from src.utils.logger import get_logger
from src.utils.paths import safe_path_segment
from src.utils.project_config import ProjectConfig, load_project_config
from src.utils.token_store import TokenStore

LOGGER = get_logger(__name__)


class Orchestrator:
    """Owns every Kollektiv subsystem and the operations built on top of them.

    Args:
        settings: Optional settings override.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        notifier: Optional[Any] = None,
    ) -> None:
        self.settings = settings or get_settings()

        self.token_store = TokenStore(self.settings.fernet_secret)
        # R2 (recommended) or pooled TeraBox accounts; see src/storage/factory.py.
        self.pool = build_storage(self.settings)
        # Optional email notifications (Resend); a no-op when unconfigured.
        if notifier is None:
            from src.utils.resend_client import ResendNotifier

            notifier = ResendNotifier(self.settings)
        self.notifier = notifier
        # Optional links to the services the user already uses (GitHub, Google,
        # Notion, webhooks, declarative REST APIs). Built in start() so a
        # failure there never blocks construction.
        self.connectors: Optional[ConnectorRegistry] = None
        self.state = StateManager(self.pool, settings=self.settings)
        self.github = GitHubClient(settings=self.settings)
        self.brain = OrchestratorBrain(settings=self.settings)
        self.agent_pool = AgentPool(self.settings.arena_account_list(), settings=self.settings)
        self.session_manager = SessionManager(self.agent_pool, settings=self.settings)
        self.sync_engine = SyncEngine(
            github=self.github,
            pool=self.pool,
            state=self.state,
            brain=self.brain,
            agent_pool=self.agent_pool,
            settings=self.settings,
        )
        self.planner = Planner(self.brain, settings=self.settings)
        # Cost estimation, caps and the local spend ledger (.kollektiv.yml aware).
        self.budget_ledger = BudgetLedger(self.settings)
        self._project_config: Optional[ProjectConfig] = None

        self.started = False
        self.started_at: Optional[datetime] = None
        self._projects: Dict[str, Dict[str, Any]] = {}
        self._collectors: Dict[str, Collector] = {}
        self._dispatcher: Optional[Dispatcher] = None
        self._sponsor_mux: Optional[SponsorLineMux] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _bind_database(self) -> None:
        """Point the process-wide engine at this instance's ``DATABASE_URL``.

        ``Settings`` may be passed explicitly (tests, embedders, a second
        configuration in one process), so an engine created from the ambient
        environment is replaced whenever it addresses a different database.
        An engine that already matches is kept, which keeps test fixtures and
        pre-seeded in-memory databases working.
        """
        bind_engine(self.settings)

    async def start(self) -> Dict[str, Any]:
        """Initialise storage, agents and (optionally) the cron scheduler.

        Failures in individual subsystems are logged and reported, never fatal:
        Kollektiv should still start and tell you what is degraded.

        Returns:
            A summary dict with the initialisation result of each subsystem.
        """
        if self.started:
            return {"started": True, "already_running": True}

        self._bind_database()
        if self.settings.AUTO_INIT_DB:
            init_db(get_engine())
        LOGGER.info("Starting Kollektiv (%s)", self.settings.ENVIRONMENT)

        report: Dict[str, Any] = {}

        async def guarded(name: str, coro: Any) -> Dict[str, Any]:
            """Run an initialisation step, capturing any failure."""
            try:
                result = await coro
                return result if isinstance(result, dict) else {"ok": True, "result": result}
            except Exception as exc:  # noqa: BLE001 - startup must not crash
                LOGGER.error("Subsystem %s failed to start: %s", name, exc)
                return {"ok": False, "error": str(exc)}

        report["storage"] = await guarded("storage", self.pool.initialize())
        report["agents"] = await guarded("agents", self.agent_pool.initialize())
        if self.github.is_configured():
            report["github"] = await guarded("github", self.github.check_connection())
        else:
            # Nothing to probe: report the gap instead of retrying 404s.
            report["github"] = {
                "ok": False,
                "configured": False,
                "repo": self.github.repo,
                "error": "GitHub is not configured; set GITHUB_TOKEN and GITHUB_REPO.",
            }
        try:
            report["brain"] = self.brain.stats()
        except Exception as exc:  # noqa: BLE001 - stats must never fail startup
            report["brain"] = {"configured": False, "error": str(exc)}

        try:
            self.connectors = ConnectorRegistry.from_settings(self.settings, token_store=self.token_store)
            report["connectors"] = self.connectors.summary()
        except Exception as exc:  # noqa: BLE001 - connectors are optional
            LOGGER.error("Could not build the connector registry: %s", exc)
            self.connectors = None
            report["connectors"] = {"count": 0, "error": str(exc)}

        try:
            await self.session_manager.start()
            report["sessions"] = self.session_manager.status()
        except Exception as exc:  # noqa: BLE001 - optional subsystem
            LOGGER.warning("Could not start the session manager: %s", exc)
            report["sessions"] = {"running": False, "error": str(exc)}

        self.started = True
        self.started_at = datetime.now(UTC)
        report["warnings"] = self.settings.config_warnings()
        LOGGER.info("Kollektiv started with warnings: %s", report["warnings"] or "none")
        for warning in report["warnings"]:
            LOGGER.warning("Configuration: %s", warning)
        return report

    async def stop(self) -> None:
        """Shut every subsystem down cleanly."""
        LOGGER.info("Stopping Kollektiv")
        try:
            await self.session_manager.stop()
        except Exception as exc:  # noqa: BLE001 - shutdown is best effort
            LOGGER.debug("Session manager shutdown raised: %s", exc)
        closers: List[Any] = [
            self.agent_pool.close,
            self.pool.close,
            self.github.close,
            self.brain.close,
        ]
        notifier_close = getattr(self.notifier, "close", None)
        if callable(notifier_close):
            closers.append(notifier_close)
        if self.connectors is not None:
            closers.append(self.connectors.close)
        for closer in closers:
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001 - shutdown is best effort
                LOGGER.debug("Shutdown step raised: %s", exc)
        self.started = False

    def get_sponsor_mux(self) -> SponsorLineMux:
        """Return the sponsor-line mux, creating it on first use."""
        if self._sponsor_mux is None:
            self._sponsor_mux = SponsorLineMux(settings=self.settings)
        return self._sponsor_mux

    async def sponsor_interlude(self, context: str = "waiting") -> Optional[Dict[str, Any]]:
        """Draw an opt-in sponsor line during dead time, and accrue it.

        This is the only place agent work meets advertising. It runs *before* a
        dispatch blocks on the pool, never inside an agent's answer or a file,
        and it is a no-op unless ``SPONSORS_ENABLED`` is set -- the mux owns
        both rules. Failures are logged and swallowed by the mux: an ad must
        never cost a task.

        Args:
            context: Dead-time context; see
                :data:`src.sponsors.line.ALLOWED_CONTEXTS`.

        Returns:
            The rendered line dict, or ``None`` when nothing was drawn.
        """
        return await self.get_sponsor_mux().next_line(context=context)

    def get_dispatcher(self) -> Dispatcher:
        """Return the dispatcher, creating it on first use."""
        if self._dispatcher is None:
            self._dispatcher = Dispatcher(
                pool=self.agent_pool,
                brain=self.brain,
                state=self.state,
                settings=self.settings,
            )
        return self._dispatcher

    def get_collector(self, project_id: str) -> Collector:
        """Return (and cache) the collector for a project."""
        if project_id not in self._collectors:
            self._collectors[project_id] = Collector(
                settings=self.settings, project_id=project_id, write_files=True
            )
        return self._collectors[project_id]

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------
    async def create_project(
        self, name: str, description: str, n_agents: int = 3
    ) -> Dict[str, Any]:
        """Create a project and generate its plan.

        Args:
            name: Project name.
            description: What to build.
            n_agents: Worker count for the plan.

        Returns:
            ``{project_id, name, description, n_agents, plan, status}``.

        Raises:
            ValueError: When the description is empty.
        """
        if not description or not description.strip():
            raise ValueError("description must not be empty")

        count = max(1, min(int(n_agents), int(self.settings.MAX_AGENT_COUNT)))
        plan = await self.planner.create_plan(description, count)

        project = Project(
            name=name or plan.get("project_name") or "Untitled project",
            description=description,
            n_agents=count,
            status="planned",
        )
        with session_scope() as session:
            session.add(project)
            session.add(
                PlanRecord(
                    project_id=project.id,
                    revision=plan.get("revision", 1),
                    plan=_dumps(plan),
                    notes="initial plan",
                )
            )
            for task in plan.get("tasks", []):
                session.add(_task_from_plan(project.id, task))
        project_id = project.id

        # Mirror the plan into the shared state document.
        state = await self.state.read_state(project_id)
        state.update(self.planner.plan_to_state(plan))
        state["project_id"] = project_id
        state["project_name"] = project.name
        state["description"] = description
        state["status"] = "planned"
        await self.state.write_state(state, project_id)
        await self.state.append_event(
            {
                "agent_id": "orchestrator",
                "action": "project_created",
                "result": f"{project.name}: {len(plan.get('tasks', []))} task(s) planned",
            },
            project_id=project_id,
        )

        record = {
            "project_id": project_id,
            "name": project.name,
            "description": description,
            "n_agents": count,
            "plan": plan,
            "status": "planned",
            "created_at": project.created_at.isoformat() if project.created_at else None,
        }
        self._projects[project_id] = record
        # Point the state manager at any project once the first project exists.
        if not self.state.project_id:
            self.state.project_id = project_id
        await self._track_agents(project_id)
        LOGGER.info("Created project %s (%s) with %s task(s)", project_id, project.name, len(plan["tasks"]))
        return record

    async def run_project(
        self,
        project_id: str,
        max_concurrency: Optional[int] = None,
        *,
        allow_over_budget: bool = False,
    ) -> Dict[str, Any]:
        """Dispatch a project's plan to the agent pool.

        Before anything is dispatched, the run is *estimated* and checked against
        the caps in ``.kollektiv.yml`` (``budget.max_usd``), ``BUDGET_MAX_USD``
        and ``BUDGET_DAILY_MAX_USD``. The estimate is logged either way, so the
        cost of a run is never a surprise after the fact.

        Args:
            project_id: The project to run.
            max_concurrency: Cap on simultaneous agents.
            allow_over_budget: Proceed even when a cap is exceeded (the operator's
                explicit override; the refusal is logged as a warning).

        Returns:
            ``{project_id, status, tasks_dispatched, results, artifact, cost}``.

        Raises:
            KeyError: When the project is unknown.
            ConfigurationError: When there are no workers or nothing to do.
            BudgetError: When the estimate exceeds a configured cap.
        """
        record = await self.get_project(project_id)
        if record is None:
            raise KeyError(f"Unknown project: {project_id}")

        if not self.agent_pool.is_configured():
            raise ConfigurationError(
                "No worker agents are configured; set ARENA_ACCOUNTS before running a project."
            )

        plan = record.get("plan") or {}
        tasks = plan.get("tasks") or []
        if not tasks:
            plan = await self.planner.create_plan(record.get("description", ""), record.get("n_agents", 1))
            tasks = plan.get("tasks", [])
            record["plan"] = plan
            await self._persist_plan(project_id, plan)

        estimate = await self._enforce_budget(project_id, allow_over_budget=allow_over_budget)
        usage_before = self.brain.usage()

        await self._set_project_status(project_id, "running")
        await self.state.update_fields(
            project_id, status="running", started_at=datetime.now(UTC).isoformat(timespec="seconds")
        )

        dispatcher = self.get_dispatcher()
        dispatcher.max_concurrency = max_concurrency or dispatcher.max_concurrency
        # Dead time: the pool is about to block for minutes on agent work.
        await self.sponsor_interlude("waiting")
        results = await dispatcher.dispatch(plan, project_id=project_id)

        collector = self.get_collector(project_id)
        merged = await collector.merge_results([result.get("result", {}) for result in results])
        snapshot = collector.build_repo_snapshot(merged)

        state = await self.state.read_state(project_id)
        state["files"] = snapshot or state.get("files", [])
        state["merged_artifact"] = {
            "file_count": merged.get("file_count", 0),
            "total_bytes": merged.get("total_bytes", 0),
            "conflicts": len(merged.get("conflicts", [])),
            "success_rate": merged.get("success_rate", 0.0),
        }
        status = "completed" if merged.get("success_rate", 0) >= 0.5 else "failed"
        state["status"] = status
        await self.state.write_state(state, project_id)
        await self._set_project_status(project_id, status)

        summary = {
            "project_id": project_id,
            "status": status,
            "tasks_dispatched": len(tasks),
            "completed": len([result for result in results if result.get("success")]),
            "failed": len([result for result in results if not result.get("success")]),
            "results": [
                {
                    "task_id": result.get("task_id"),
                    "success": result.get("success"),
                    "agent_id": result.get("agent_id"),
                    "error": result.get("error", ""),
                    "files": (result.get("result") or {}).get("file_paths", []),
                }
                for result in results
            ],
            "artifact": {
                "file_count": merged.get("file_count", 0),
                "total_bytes": merged.get("total_bytes", 0),
                "conflicts": merged.get("conflicts", []),
                "missing_dependencies": merged.get("missing_dependencies", []),
            },
        }
        summary["storage_backend"] = self.settings.storage_backend
        if estimate is not None:
            await self._record_run_spend(project_id, estimate, tasks=len(tasks), before=usage_before)
            summary["cost"] = {
                "estimated_usd": estimate.total_usd,
                "projected_usd": estimate.projected_usd,
                "cap_usd": estimate.max_usd,
                "cap_source": estimate.source,
                "tasks": estimate.tasks,
                "note": "recorded in the local ledger; `kollektiv budget` shows it",
            }
        LOGGER.info(
            "Project %s finished with status %s (%s/%s tasks, storage=%s)",
            project_id,
            status,
            summary["completed"],
            summary["tasks_dispatched"],
            summary["storage_backend"],
        )
        await self._notify_run(project_id, record.get("name", ""), summary)
        return summary

    async def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        """Return a project record (cache first, then SQLite).

        Args:
            project_id: The project identifier.

        Returns:
            The project dict, or ``None`` when it does not exist.
        """
        if project_id in self._projects:
            return self._projects[project_id]
        with session_scope() as session:
            project = session.get(Project, project_id)
            if project is None:
                return None
            plan_records = list(
                session.exec(select(PlanRecord).where(PlanRecord.project_id == project_id)).all()
            )
            plan = plan_records[-1].plan_dict() if plan_records else project.plan_dict()
            tasks = [
                _task_to_dict(task)
                for task in session.exec(select(Task).where(Task.project_id == project_id)).all()
            ]
        if not plan.get("tasks") and tasks:
            plan = dict(plan, tasks=tasks)
        record = {
            "project_id": project.id,
            "name": project.name,
            "description": project.description,
            "n_agents": project.n_agents,
            "plan": plan,
            "status": project.status,
            "created_at": project.created_at.isoformat() if project.created_at else None,
            "updated_at": project.updated_at.isoformat() if project.updated_at else None,
        }
        self._projects[project_id] = record
        return record

    async def list_projects(self) -> List[Dict[str, Any]]:
        """List every known project, newest first."""
        with session_scope() as session:
            projects = list(session.exec(select(Project).order_by(col(Project.created_at).desc())).all())
            records = [
                {
                    "project_id": project.id,
                    "name": project.name,
                    "status": project.status,
                    "n_agents": project.n_agents,
                    "created_at": project.created_at.isoformat() if project.created_at else None,
                }
                for project in projects
            ]
        if not records:
            records = [
                {
                    "project_id": record["project_id"],
                    "name": record["name"],
                    "status": record["status"],
                    "n_agents": record["n_agents"],
                    "created_at": record.get("created_at"),
                }
                for record in self._projects.values()
            ]
        LOGGER.debug("Listing %s project(s)", len(records))
        return records

    async def get_handoff(self, project_id: str, write: bool = True) -> Dict[str, Any]:
        """Build a resume briefing for a project from its shared state.

        This is the answer to "the session ended mid-run": the plan, task
        status, files, history and the next concrete actions are rendered into a
        briefing (and written to ``HANDOFF.md`` in the project workspace) so the
        next agent, chat or colleague continues instead of starting over.

        Args:
            project_id: The project identifier.
            write: Also write ``HANDOFF.md`` into the project workspace.

        Returns:
            The handoff dict, including its rendered ``markdown``.

        Raises:
            KeyError: When the project is unknown.
        """
        state = await self.get_project_status(project_id)
        handoff = build_handoff(state, project_id, settings=self.settings)
        if write:
            directory = self.settings.workspace_path / project_id
            try:
                path = write_handoff_file(handoff, directory)
                handoff["written_to"] = path
            except OSError as exc:  # noqa: BLE001 - the briefing is still returned
                LOGGER.warning("Could not write HANDOFF.md for %s: %s", project_id, exc)
        return handoff

    async def get_project_status(self, project_id: str) -> Dict[str, Any]:
        """Return the shared state document for a project, plus live counters.

        Args:
            project_id: The project identifier.

        Returns:
            The state dict enriched with ``project_id``, ``queued_tasks`` and
            ``pool`` status.

        Raises:
            KeyError: When the project is unknown.
        """
        if await self.get_project(project_id) is None and not (await self.state.read_state(project_id)).get("project_name"):
            raise KeyError(f"Unknown project: {project_id}")
        state = await self.state.read_state(project_id, force=True)
        with session_scope() as session:
            tasks = list(session.exec(select(Task).where(Task.project_id == project_id)).all())
        if tasks:
            state["tasks"] = [_task_to_dict(task) for task in tasks]
        state["queued_tasks"] = len([task for task in state.get("tasks", []) if task.get("status") == "pending"])
        state["pool"] = self.agent_pool.snapshot()
        return state

    async def replan_project(
        self, project_id: str, dispatch: bool = False, max_new_tasks: int = 3
    ) -> Dict[str, Any]:
        """Build a corrective plan for a project's failed tasks.

        Uses :meth:`Planner.replan` so the brain (or the deterministic fallback)
        can propose repair subtasks. With ``dispatch=True`` the corrective
        tasks are executed immediately afterwards.

        Args:
            project_id: The project identifier.
            dispatch: Run the corrective tasks after persisting the plan.
            max_new_tasks: Cap on corrective tasks generated.

        Returns:
            ``{project_id, revision, new_tasks, plan, results}``.

        Raises:
            KeyError: When the project is unknown.
            ConfigurationError: When ``dispatch`` is true and no agents exist.
        """
        record = await self.get_project(project_id)
        if record is None:
            raise KeyError(f"Unknown project: {project_id}")

        status = await self.get_project_status(project_id)
        state = dict(status)
        state.setdefault("description", record.get("description", ""))
        tasks = list(state.get("tasks") or record.get("plan", {}).get("tasks") or [])
        state["tasks"] = tasks
        failed = [task for task in tasks if str(task.get("status")) == "failed"]
        if not failed:
            LOGGER.info("Project %s has no failed tasks; nothing to replan", project_id)
            return {
                "project_id": project_id,
                "revision": (record.get("plan") or {}).get("revision", 1),
                "new_tasks": [],
                "plan": record.get("plan") or {},
                "results": [],
            }

        plan = await self.planner.replan(state, failed, max_new_tasks=max_new_tasks)
        record["plan"] = plan
        await self._persist_plan(project_id, plan)
        await self.record_event(
            project_id,
            {
                "agent_id": "planner",
                "action": "replan",
                "result": f"{len(plan.get('tasks', []))} task(s) after replanning {len(failed)} failure(s)",
            },
        )

        new_ids = {str(task.get("id")) for task in plan.get("tasks", [])} - {
            str(task.get("id")) for task in tasks
        }
        new_tasks = [task for task in plan.get("tasks", []) if str(task.get("id")) in new_ids]
        LOGGER.info("Replanned %s: %s corrective task(s)", project_id, len(new_tasks))

        results: List[Dict[str, Any]] = []
        if dispatch:
            if not self.agent_pool.is_configured():
                raise ConfigurationError(
                    "No worker agents are configured; set ARENA_ACCOUNTS before dispatching."
                )
            corrective_plan = dict(plan, tasks=new_tasks)
            results = await self.get_dispatcher().dispatch(corrective_plan, project_id=project_id)

        return {
            "project_id": project_id,
            "revision": plan.get("revision", 1),
            "new_tasks": new_tasks,
            "plan": plan,
            "results": results,
        }

    async def get_project_files(self, project_id: str) -> List[Dict[str, Any]]:
        """Return every file stored for a project (TeraBox + local index).

        Args:
            project_id: The project identifier.

        Returns:
            A list of file dicts with ``path``, ``size`` and ``source``.
        """
        files: List[Dict[str, Any]] = []
        try:
            files = await self.pool.list_project_files(project_id)
        except Exception as exc:  # noqa: BLE001 - fall back to the local index
            LOGGER.warning("TeraBox listing failed for %s: %s", project_id, exc)
        with session_scope() as session:
            records = list(session.exec(select(ProjectFile).where(ProjectFile.project_id == project_id)).all())
            local = [
                {
                    "path": record.remote_path or record.path,
                    "size": record.size,
                    "source": record.source,
                    "account_id": record.account_id,
                }
                for record in records
            ]
        known = {entry.get("path") for entry in files}
        files.extend(entry for entry in local if entry["path"] not in known)
        return files

    async def upload_project_file(self, project_id: str, local_path: str) -> Dict[str, Any]:
        """Archive a local file into the project's TeraBox folder.

        Args:
            project_id: The project identifier.
            local_path: Path of the file to upload.

        Returns:
            The upload result ``{account_id, path, url, size}``.
        """
        import os

        if not os.path.isfile(local_path):
            raise FileNotFoundError(f"Local file not found: {local_path}")
        project_segment = safe_path_segment(project_id, label="project id")
        remote = (
            f"{self.pool.remote_root.rstrip('/')}/"
            f"{project_segment}/uploads/{os.path.basename(local_path)}"
        )
        result = await self.pool.upload_file(local_path, remote)
        with session_scope() as session:
            session.add(
                ProjectFile(
                    project_id=project_id,
                    path=os.path.basename(local_path),
                    local_path=local_path,
                    remote_path=result.get("path", remote),
                    account_id=result.get("account_id", ""),
                    size=int(result.get("size") or 0),
                    source="upload",
                )
            )
        return result

    # ------------------------------------------------------------------
    # Agent links: who may use which connector
    # ------------------------------------------------------------------
    async def list_links(self) -> List[Dict[str, Any]]:
        """Return every agent-connector link, newest last.

        Returns:
            ``[{link_id, agent_id, agent_name, connector, note, created_by, created_at}]``.
        """
        try:
            with session_scope() as session:
                # ``col()`` makes the column expression explicit for the type checker;
                # without it SQLModel hands the attribute's *value* type to order_by.
                rows = session.exec(select(AgentLinkRecord).order_by(col(AgentLinkRecord.created_at))).all()
                return [self._link_payload(row) for row in rows]
        except Exception as exc:  # noqa: BLE001 - a broken ledger must not break the UI
            LOGGER.error("Could not read agent links: %s", exc)
            return []

    @staticmethod
    def _link_payload(row: AgentLinkRecord) -> Dict[str, Any]:
        """Serialise one link row (and name the agent as it is known now)."""
        return {
            "link_id": row.link_id,
            "agent_id": row.agent_id,
            "agent_name": row.agent_name,
            "connector": row.connector,
            "note": row.note,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else "",
        }

    def agent_name(self, agent_id: str) -> str:
        """Return the display name for an agent id, or ``""`` when unknown."""
        agent = self.agent_pool.get_agent(agent_id)
        if agent is None:
            return ""
        return str(getattr(agent, "name", "") or getattr(agent, "label", ""))

    def ensure_connectors(self) -> Optional[ConnectorRegistry]:
        """Return the connector registry, building it if the app was never started.

        The CLI and the tests link agents without running the whole orchestrator
        lifecycle, and a grant should not depend on that: build the registry
        lazily and keep it.

        Returns:
            The registry, or ``None`` when it could not be built (reported).
        """
        if self.connectors is None:
            try:
                self.connectors = ConnectorRegistry.from_settings(self.settings, token_store=self.token_store)
            except Exception as exc:  # noqa: BLE001 - a broken registry is not fatal here
                LOGGER.error("Could not build the connector registry: %s", exc)
                return None
        return self.connectors

    def connector_names(self) -> List[str]:
        """Return the connectors that exist (configured or not)."""
        registry = self.ensure_connectors()
        return list(registry.names) if registry is not None else []

    async def link_agent(
        self, agent_id: str, connector: str, *, note: str = "", created_by: str = ""
    ) -> Dict[str, Any]:
        """Grant an agent access to a connector.

        Args:
            agent_id: Account id of the worker agent (from ``GET /agents/status``).
            connector: Connector name (from ``GET /connectors``).
            note: Optional human note ("owns the release notes").
            created_by: Who did it, for the audit trail.

        Returns:
            The stored link.

        Raises:
            KeyError: When the agent or the connector does not exist.
            ValueError: When the same pair is already linked.
        """
        if not self.agent_name(agent_id):
            raise KeyError(f"Unknown agent: {agent_id}")
        if connector not in self.connector_names():
            raise KeyError(f"Unknown connector: {connector}")

        link_id = new_id("lnk")
        try:
            with session_scope() as session:
                existing = session.exec(
                    select(AgentLinkRecord).where(
                        AgentLinkRecord.agent_id == agent_id, AgentLinkRecord.connector == connector
                    )
                ).first()
                if existing is not None:
                    raise ValueError(f"{agent_id} is already linked to {connector}")
                row = AgentLinkRecord(
                    link_id=link_id,
                    agent_id=agent_id,
                    agent_name=self.agent_name(agent_id),
                    connector=connector,
                    note=note[:280],
                    created_by=created_by[:120],
                )
                session.add(row)
                session.flush()
                payload = self._link_payload(row)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface storage problems clearly
            LOGGER.error("Could not link %s -> %s: %s", agent_id, connector, exc)
            raise
        LOGGER.info("Linked agent %s to connector %s", agent_id, connector)
        return payload

    async def unlink_agent(self, link_id: str) -> Dict[str, Any]:
        """Remove one link.

        Args:
            link_id: The link to delete (``DELETE /links/{link_id}``).

        Returns:
            ``{removed: True, link_id: ...}``.

        Raises:
            KeyError: When the link does not exist.
        """
        with session_scope() as session:
            row = session.get(AgentLinkRecord, link_id)
            if row is None:
                raise KeyError(f"Unknown link: {link_id}")
            payload = self._link_payload(row)
            session.delete(row)
        LOGGER.info("Unlinked %s from %s", payload["agent_id"], payload["connector"])
        return {"removed": True, **payload}

    async def resolve_link(self, agent_id: str, connector: str) -> Dict[str, Any]:
        """Return the link for a pair, or an empty dict when there is none."""
        with session_scope() as session:
            row = session.exec(
                select(AgentLinkRecord).where(
                    AgentLinkRecord.agent_id == agent_id, AgentLinkRecord.connector == connector
                )
            ).first()
            return self._link_payload(row) if row is not None else {}

    async def access_decision(self, connector: str, agent_id: str = "") -> tuple[bool, str]:
        """Decide whether a connector call is allowed, and say why.

        Args:
            connector: Connector being called.
            agent_id: The calling agent, when the caller names one.

        Returns:
            ``(allowed, reason)`` — the reason is what the API reports, so an
            operator can tell "not linked" from "connector is open".
        """
        links = [link for link in await self.list_links() if link["connector"] == connector]
        if not links:
            return True, "connector has no links: open to every caller"
        if not agent_id:
            return True, "operator call (no agent named); links constrain worker agents only"
        allowed = any(link["agent_id"] == agent_id for link in links)
        if allowed:
            return True, f"agent {agent_id} is linked to {connector}"
        names = ", ".join(sorted({link["agent_name"] or link["agent_id"] for link in links}))
        return False, f"{connector} is linked to {names}; {agent_id} is not one of them"

    async def links_by_connector(self) -> Dict[str, List[Dict[str, Any]]]:
        """Group links by connector, for the connectors payload."""
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for link in await self.list_links():
            grouped.setdefault(link["connector"], []).append(link)
        return grouped

    # ------------------------------------------------------------------
    # Status / sync
    # ------------------------------------------------------------------
    async def get_agents_status(self, probe: bool = False) -> List[Dict[str, Any]]:
        """Return the agent pool status."""
        return await self.agent_pool.get_pool_status(probe=probe)

    async def get_storage_status(self) -> Dict[str, Any]:
        """Return the TeraBox pool quota summary."""
        try:
            return await self.pool.get_total_quota()
        except Exception as exc:  # noqa: BLE001 - report rather than fail
            LOGGER.error("Storage status failed: %s", exc)
            return {
                "used_gb": 0.0,
                "free_gb": 0.0,
                "total_gb": 0.0,
                "accounts": self.pool.account_count,
                "healthy": 0,
                "per_account": self.pool.get_pool_status(),
                "error": str(exc),
            }

    async def trigger_sync(self) -> Dict[str, Any]:
        """Run the cron sync on demand (``POST /sync``)."""
        summary = await self.sync_engine.cron_sync()
        return summary

    async def on_pr_merged(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle a merged pull request (called from the webhook handler)."""
        return await self.sync_engine.on_pr_merged(payload)

    async def health(self) -> Dict[str, Any]:
        """Return a health report covering every subsystem."""
        brain_stats = self.brain.stats()
        return {
            "status": "ok",
            "app": self.settings.APP_NAME,
            "environment": self.settings.ENVIRONMENT,
            "started": self.started,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "subsystems": {
                "storage": {
                    "backend": self.settings.storage_backend,
                    "configured": self.pool.is_configured(),
                    "accounts": self.pool.account_count,
                    "healthy": len([state for state in self.pool.accounts if state.healthy]),
                },
                "agents": {
                    "configured": self.agent_pool.is_configured(),
                    "agents": self.agent_pool.size,
                    "busy": len([agent for agent in self.agent_pool.agents if agent.busy]),
                    "names": list(self.agent_pool.names.values()),
                },
                "brain": brain_stats,
                "github": {"configured": self.github.is_configured(), "repo": self.github.repo},
                "notifications": (
                    self.notifier.stats()
                    if callable(getattr(self.notifier, "stats", None))
                    else {"configured": False}
                ),
                "sessions": self.session_manager.status(),
                "sync": self.sync_engine.status(),
                "connectors": (
                    self.connectors.summary()
                    if self.connectors is not None
                    else {"count": 0, "configured": [], "actions": 0}
                ),
                "gateway": self.gateway_summary(),
                "budget": {
                    "enabled": bool(self.settings.BUDGET_ENABLED),
                    "max_usd": float(self.settings.BUDGET_MAX_USD),
                    "daily_max_usd": float(self.settings.BUDGET_DAILY_MAX_USD),
                    "project_config": (
                        str(self._project_config.path) if self._project_config and self._project_config.path else None
                    ),
                    "ledger": "run `kollektiv budget` for totals (skipped here: /health must not query)",
                },
            },
            "warnings": self.settings.config_warnings(),
        }

    # ------------------------------------------------------------------
    # Project configuration and budget
    # ------------------------------------------------------------------
    def project_config(self, *, reload: bool = False) -> ProjectConfig:
        """Return this deployment's ``.kollektiv.yml`` (loaded once).

        Args:
            reload: Re-read the file instead of using the cached copy.

        Returns:
            The :class:`~src.utils.project_config.ProjectConfig`. A missing file
            yields an empty config, never an error.
        """
        if self._project_config is None or reload:
            explicit = self.settings.PROJECT_CONFIG_PATH or None
            config = load_project_config(explicit)
            if not config.found and not explicit:
                # The orchestrator runs from many places; also look next to the
                # workspace, which is where a deployed instance keeps the repo.
                config = load_project_config(None, start=Path(self.settings.WORKSPACE_DIR))
            self._project_config = config
        return self._project_config

    def budget_planner(self, *, reload: bool = False) -> BudgetPlanner:
        """Return a :class:`~src.orchestrator.budget.BudgetPlanner` for this run.

        Args:
            reload: Re-read the project config first.

        Returns:
            A planner bound to the current settings and project config.
        """
        return BudgetPlanner(self.settings, config=self.project_config(reload=reload))

    async def estimate_project_cost(
        self, project_id: str, *, n_agents: Optional[int] = None
    ) -> CostEstimate:
        """Estimate what running ``project_id`` will cost.

        Args:
            project_id: The project to estimate.
            n_agents: Override the agent count (defaults to the project's own).

        Returns:
            The :class:`~src.orchestrator.budget.CostEstimate`. A project that
            has not been planned yet is estimated as "plan, then this many
            tasks", which is exactly what running it will do.

        Raises:
            KeyError: When the project is unknown.
        """
        record = await self.get_project(project_id)
        if record is None:
            raise KeyError(f"Unknown project: {project_id}")
        plan = record.get("plan") or {}
        wanted = int(n_agents or record.get("n_agents") or self.settings.DEFAULT_AGENT_COUNT)
        if not plan.get("tasks"):
            plan = {
                "tasks": [{"title": f"pending task {index + 1}", "description": ""} for index in range(max(1, wanted))],
                "waves": [[f"t{index + 1}"] for index in range(max(1, wanted))],
                "n_agents": wanted,
            }
        spent = float((await self.budget_ledger.project(project_id)).get("usd") or 0.0)
        return self.budget_planner().estimate(
            plan, project_id=project_id, n_agents=wanted, spent_usd=spent
        )

    async def budget_report(self) -> Dict[str, Any]:
        """Return the ledger plus today's position against the daily cap.

        Returns:
            The ledger summary with a ``daily_cap`` block.
        """
        summary = await self.budget_ledger.summary()
        allowed, reason = daily_cap_check(
            self.settings, self.budget_ledger, float(summary["today"]["usd"])
        )
        summary["daily_cap"] = {"allowed": allowed, "detail": reason}
        config = self.project_config()
        summary["project_config"] = config.to_dict()
        return summary

    async def _enforce_budget(self, project_id: str, *, allow_over_budget: bool) -> Optional[CostEstimate]:
        """Check the caps before a run dispatches anything.

        Args:
            project_id: The project about to run.
            allow_over_budget: The operator's explicit override.

        Returns:
            The estimate (for the ledger and the summary), or ``None`` when
            budgeting is disabled.

        Raises:
            BudgetError: When a cap is exceeded and no override was given.
        """
        if not self.settings.BUDGET_ENABLED:
            return None
        estimate = await self.estimate_project_cost(project_id)
        LOGGER.info("Project %s: %s", project_id, estimate.message)
        today = await self.budget_ledger.today_total()
        allowed, reason = daily_cap_check(self.settings, self.budget_ledger, float(today["usd"]))
        if not allowed and not allow_over_budget:
            raise BudgetError(
                f"Refusing to run {project_id}: {reason}. Raise BUDGET_DAILY_MAX_USD or pass allow_over_budget.",
                spent_today_usd=today["usd"],
                daily_max_usd=float(self.settings.BUDGET_DAILY_MAX_USD),
            )
        self.budget_planner().enforce(estimate, allow_over_budget=allow_over_budget)
        return estimate

    async def _record_run_spend(self, project_id: str, estimate: CostEstimate, *, tasks: int, before: Dict[str, Any]) -> None:
        """Add one finished run to the local ledger.

        Brain tokens come from the provider's ``usage`` block when it reported
        one during the run (delta), otherwise the estimate stands in and the row
        is flagged ``estimated``. Worker tokens are always estimates: the worker
        endpoints are OpenAI-compatible chat calls that do not return usage.

        Args:
            project_id: The project that ran.
            estimate: The pre-run estimate (used as the fallback).
            tasks: How many tasks were dispatched.
            before: :meth:`OrchestratorBrain.usage` sampled before the run.
        """
        after = self.brain.usage()
        reported = after["reported"] - int(before.get("reported", 0))
        measured = reported > 0
        brain_in = after["tokens_in"] - int(before.get("tokens_in", 0)) if measured else estimate.brain_tokens_in
        brain_out = after["tokens_out"] - int(before.get("tokens_out", 0)) if measured else estimate.brain_tokens_out
        prices = estimate.prices or self.budget_planner().prices()
        usd = usd_for_tokens(
            prices,
            brain_tokens_in=max(0, brain_in),
            brain_tokens_out=max(0, brain_out),
            worker_tokens_in=estimate.worker_tokens_in,
            worker_tokens_out=estimate.worker_tokens_out,
        )
        await self.budget_ledger.record(
            project_id,
            tasks=int(tasks),
            brain_calls=max(0, after["calls"] - int(before.get("calls", 0))),
            brain_tokens_in=max(0, brain_in),
            brain_tokens_out=max(0, brain_out),
            worker_tokens_in=estimate.worker_tokens_in,
            worker_tokens_out=estimate.worker_tokens_out,
            usd=usd,
            estimated=not measured,
        )

    def gateway_summary(self) -> Dict[str, Any]:
        """Describe the MCP gateway's state without touching the database.

        ``/health`` must answer instantly and never fail, so this reports the
        configuration only; ``kollektiv gateway status`` adds the client count
        and the audit totals, which need a query.

        Returns:
            ``{enabled, url, mcp_path, require_tokens, policy_file}``.
        """
        settings = self.settings
        return {
            "enabled": bool(settings.GATEWAY_ENABLED),
            "url": f"http://{settings.GATEWAY_HOST}:{settings.GATEWAY_PORT}",
            "mcp_path": settings.GATEWAY_MCP_PATH,
            "require_tokens": bool(settings.GATEWAY_REQUIRE_TOKENS),
            "policy_file": settings.GATEWAY_POLICY_PATH or None,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _persist_plan(self, project_id: str, plan: Dict[str, Any]) -> None:
        """Store a plan revision and its tasks in SQLite."""
        with session_scope() as session:
            session.add(
                PlanRecord(
                    project_id=project_id,
                    revision=plan.get("revision", 1),
                    plan=_dumps(plan),
                    notes="replanned",
                )
            )
            for task in plan.get("tasks", []):
                # SQLModel orders composite keys by column declaration: (id, project_id).
                existing = session.get(Task, (str(task.get("id")), project_id))
                if existing is not None and existing.project_id == project_id:
                    existing.title = task.get("title", existing.title)
                    existing.description = task.get("description", existing.description)
                    existing.dependencies = _dumps(task.get("dependencies", []))
                    existing.priority = int(task.get("priority") or 3)
                    existing.updated_at = utcnow()
                    session.add(existing)
                else:
                    session.add(_task_from_plan(project_id, task))

    async def _notify_run(
        self, project_id: str, project_name: str, summary: Dict[str, Any]
    ) -> None:
        """Email a run summary when notifications are enabled (never fatal)."""
        sender = getattr(self.notifier, "send_run_summary", None)
        if not callable(sender):
            return
        try:
            await sender(project_name or project_id, summary, project_id=project_id)
        except Exception as exc:  # noqa: BLE001 - notifications must not fail a run
            LOGGER.warning("Could not send the run summary email: %s", exc)
        await self._broadcast_run(project_id, project_name, summary)
        try:
            await self.get_handoff(project_id)
        except Exception as exc:  # noqa: BLE001 - a stale briefing is not fatal
            LOGGER.debug("Could not refresh HANDOFF.md for %s: %s", project_id, exc)

    async def _broadcast_run(
        self, project_id: str, project_name: str, summary: Dict[str, Any]
    ) -> None:
        """POST the run summary to EVENT_WEBHOOKS (never fatal)."""
        if self.connectors is None or not self.settings.event_webhook_urls:
            return
        status = summary.get("status", "unknown")
        text = (
            f"{self.settings.APP_NAME}: {project_name or project_id} {status} — "
            f"{summary.get('completed', 0)}/{summary.get('tasks_dispatched', 0)} task(s), "
            f"{len(summary.get('errors') or [])} error(s)"
        )
        try:
            await self.connectors.broadcast(
                {
                    "type": "run.finished",
                    "project_id": project_id,
                    "project_name": project_name,
                    "status": status,
                    "summary": summary,
                    "text": text,
                }
            )
        except Exception as exc:  # noqa: BLE001 - notifications must not fail a run
            LOGGER.warning("Could not broadcast the run event: %s", exc)

    async def _set_project_status(self, project_id: str, status: str) -> None:
        """Update the project status in SQLite and the in-memory cache."""
        with session_scope() as session:
            project = session.get(Project, project_id)
            if project is not None:
                project.status = status
                project.updated_at = utcnow()
                session.add(project)
        if project_id in self._projects:
            self._projects[project_id]["status"] = status

    async def _track_agents(self, project_id: str) -> None:
        """Persist agent statistics so the UI can show historical throughput."""
        try:
            with session_scope() as session:
                for agent in self.agent_pool.agents:
                    record = session.get(AgentRecord, agent.account_id)
                    if record is None:
                        record = AgentRecord(
                            account_id=agent.account_id,
                            email=agent.email,
                            label=agent.label,
                        )
                    record.status = "rate_limited" if agent.is_rate_limited() else ("busy" if agent.busy else "idle")
                    record.tasks_done = agent.tasks_done
                    record.tasks_failed = agent.tasks_failed
                    record.total_latency_ms = agent.total_latency_ms
                    record.last_error = agent.last_error
                    record.last_seen = utcnow()
                    session.add(record)
        except Exception as exc:  # noqa: BLE001 - telemetry must never break a request
            LOGGER.debug("Could not persist agent statistics: %s", exc)

    async def record_event(self, project_id: str, event: Dict[str, Any]) -> None:
        """Record an event in both SQLite and the shared state document."""
        try:
            with session_scope() as session:
                session.add(
                    EventLog(
                        project_id=project_id,
                        agent_id=str(event.get("agent_id", "")),
                        action=str(event.get("action", "")),
                        result=str(event.get("result", ""))[:2000],
                        payload=_dumps(event.get("payload", {})),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - logging must not fail a request
            LOGGER.debug("Could not write the event log row: %s", exc)
        await self.state.append_event(event, project_id=project_id, persist=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Orchestrator storage={self.pool.account_count} agents={self.agent_pool.size} "
            f"brain_configured={self.brain.is_configured}>"
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _dumps(value: Any) -> str:
    """Serialise ``value`` to JSON text (tolerant of datetimes)."""
    import json

    return json.dumps(value, default=str)


def _task_from_plan(project_id: str, task: Dict[str, Any]) -> Task:
    """Build a :class:`Task` row from a plan entry."""
    return Task(
        id=str(task.get("id") or new_id("tsk_")),
        project_id=project_id,
        title=str(task.get("title") or "")[:300],
        description=str(task.get("description") or ""),
        priority=int(task.get("priority") or 3),
        dependencies=_dumps(task.get("dependencies", [])),
        status=str(task.get("status") or "pending"),
    )


def _task_to_dict(task: Task) -> Dict[str, Any]:
    """Serialise a :class:`Task` row into the state document shape."""
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "priority": task.priority,
        "dependencies": task.dependency_list(),
        "assigned_agent": task.assigned_agent,
        "attempts": task.attempts,
        "score": task.score,
        "feedback": task.feedback,
        "error": task.error,
    }


__all__ = ["Orchestrator"]
