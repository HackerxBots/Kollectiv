"""Background session hygiene for the worker agent pool.

Worker sessions expire, get rate limited and occasionally die mid-task. This
module owns the periodic pass that detects those conditions and repairs them,
so no other component has to poll.

It uses APScheduler's ``AsyncIOScheduler`` when a running event loop is
available and falls back to a plain asyncio task otherwise (useful in tests).

Usage::

    manager = SessionManager(pool, interval_seconds=1800)
    await manager.start()
    ...
    await manager.stop()
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

from config.settings import Settings, get_settings
from src.agents.agent_pool import AgentPool
from src.utils.errors import KollektivError
from src.utils.logger import get_logger
from src.utils.retry import async_retry

LOGGER = get_logger(__name__)


class SessionManager:
    """Keep worker sessions alive and report their health.

    Args:
        pool: The agent pool to manage.
        settings: Optional settings override.
        interval_seconds: How often to run the maintenance pass. Defaults to
            ``SESSION_REFRESH_INTERVAL`` (1800s).
        on_refresh: Optional callback invoked with the pass summary, so the
            orchestrator can log it into the project state.
    """

    def __init__(
        self,
        pool: AgentPool,
        settings: Optional[Settings] = None,
        interval_seconds: Optional[int] = None,
        on_refresh: Optional[Any] = None,
    ) -> None:
        self.pool = pool
        self.settings = settings or get_settings()
        self.interval_seconds = int(interval_seconds or self.settings.SESSION_REFRESH_INTERVAL or 1800)
        self.on_refresh = on_refresh
        self.last_run_at: Optional[datetime] = None
        self.last_summary: Dict[str, Any] = {}
        self.run_count = 0

        self._task: Optional[asyncio.Task] = None
        self._scheduler: Optional[Any] = None
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        """Return ``True`` when a background loop is active."""
        if self._scheduler is not None:
            return bool(getattr(self._scheduler, "running", False))
        return self._task is not None and not self._task.done()

    async def start(self, use_scheduler: bool = True) -> bool:
        """Start the periodic session maintenance loop.

        Args:
            use_scheduler: Prefer APScheduler when it can be created.

        Returns:
            ``True`` when a loop was started (or was already running).
        """
        if self.is_running:
            LOGGER.debug("SessionManager already running")
            return True

        self._stopping.clear()
        if use_scheduler:
            try:
                from apscheduler.schedulers.asyncio import AsyncIOScheduler

                scheduler = AsyncIOScheduler(timezone="UTC")
                scheduler.add_job(
                    self._scheduled_run,
                    "interval",
                    seconds=self.interval_seconds,
                    id="kollektiv-session-refresh",
                    replace_existing=True,
                    max_instances=1,
                    coalesce=True,
                )
                scheduler.start()
                self._scheduler = scheduler
                LOGGER.info("SessionManager started (APScheduler, every %ss)", self.interval_seconds)
                return True
            except Exception as exc:  # noqa: BLE001 - fall back to asyncio
                LOGGER.warning("APScheduler unavailable (%s); using an asyncio loop", exc)
                self._scheduler = None

        self._task = asyncio.create_task(self._loop(), name="kollektiv-session-manager")
        LOGGER.info("SessionManager started (asyncio, every %ss)", self.interval_seconds)
        return True

    async def stop(self) -> None:
        """Stop the background loop and release resources."""
        self._stopping.set()
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception as exc:  # noqa: BLE001 - shutdown is best effort
                LOGGER.debug("Scheduler shutdown raised: %s", exc)
            self._scheduler = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        LOGGER.info("SessionManager stopped")

    async def _loop(self) -> None:
        """Sleep/refresh loop used when APScheduler is unavailable."""
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.interval_seconds)
                return  # stop() was called
            except TimeoutError:
                await self._scheduled_run()

    async def _scheduled_run(self) -> None:
        """Wrapper that never lets a failure escape into the scheduler."""
        try:
            await self.check_and_refresh()
        except Exception as exc:  # noqa: BLE001 - background jobs must not die
            LOGGER.error("Session maintenance pass failed: %s", exc)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    @async_retry(max_retries=2, base_delay=2.0, max_delay=10.0)
    async def check_and_refresh(self, force: bool = False) -> Dict[str, Any]:
        """Health check every agent and repair the broken ones.

        Args:
            force: Reset all sessions regardless of state.

        Returns:
            ``{timestamp, agents, healthy, unhealthy, refreshed, details}``.
        """
        statuses = await self.pool.get_pool_status(probe=True)
        unhealthy: List[Dict[str, Any]] = [
            status
            for status in statuses
            if not status.get("alive", False) or not status.get("token_valid", True)
        ]
        details: List[Dict[str, Any]] = []
        refreshed = 0
        for status in unhealthy:
            agent = self.pool.get_agent(status["account_id"])
            if agent is None:
                continue
            try:
                if await agent.reset_session():
                    refreshed += 1
                    details.append({"account_id": agent.account_id, "label": agent.label, "refreshed": True})
                else:
                    details.append({"account_id": agent.account_id, "label": agent.label, "refreshed": False})
            except KollektivError as exc:
                details.append(
                    {"account_id": agent.account_id, "label": agent.label, "refreshed": False, "error": str(exc)}
                )

        if force:
            outcome = await self.pool.refresh_all_sessions(force=True)
            refreshed = max(refreshed, int(outcome.get("refreshed", 0)))
            details.extend(outcome.get("details", []))

        self.last_run_at = datetime.now(UTC)
        self.run_count += 1
        self.last_summary = {
            "timestamp": self.last_run_at.isoformat(),
            "agents": len(statuses),
            "healthy": len(statuses) - len(unhealthy),
            "unhealthy": len(unhealthy),
            "refreshed": refreshed,
            "details": details,
        }
        LOGGER.info(
            "Session maintenance: %s/%s healthy, %s refreshed",
            self.last_summary["healthy"],
            self.last_summary["agents"],
            refreshed,
        )
        if self.on_refresh is not None:
            try:
                result = self.on_refresh(self.last_summary)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - callbacks must not break the loop
                LOGGER.warning("on_refresh callback failed: %s", exc)
        return self.last_summary

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        """Return the manager's own status."""
        return {
            "running": self.is_running,
            "interval_seconds": self.interval_seconds,
            "runs": self.run_count,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_summary": self.last_summary,
            "backend": "apscheduler" if self._scheduler is not None else "asyncio",
        }

    async def ensure_ready_sessions(self, minimum: int = 1) -> int:
        """Make sure at least ``minimum`` agents are usable.

        Args:
            minimum: Number of ready agents desired.

        Returns:
            The number of agents that are ready after the repair pass.
        """
        ready = len([agent for agent in self.pool.agents if await agent.is_ready()])
        if ready >= minimum:
            return ready
        LOGGER.warning("Only %s/%s agents ready; forcing a session refresh", ready, minimum)
        await self.check_and_refresh(force=True)
        return len([agent for agent in self.pool.agents if await agent.is_ready()])


__all__ = ["SessionManager"]
