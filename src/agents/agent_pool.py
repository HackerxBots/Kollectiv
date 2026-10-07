"""Pool of worker agents with scheduling, retries and health tracking.

The pool is the only place that decides *which* account runs a task. It keeps
a fairness-ordered queue (least recently used first), skips busy or
rate-limited agents, retries a task on a different agent when one fails, and
records statistics into both memory and SQLite.

Usage::

    pool = AgentPool(settings.arena_account_list())
    await pool.initialize()
    result = await pool.assign_task(
        {"id": "t1", "title": "Write the parser", "description": "..."},
        context="## Shared project context ...",
    )
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from config.settings import Settings, get_settings
from src.agents.arena_client import ArenaClient
from src.utils.errors import ArenaError, AuthenticationError, ConfigurationError, RateLimitError
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)

#: System prompt prepended to every worker task.
WORKER_SYSTEM_PROMPT = (
    "You are one worker in a coordinated team of AI engineers building a shared "
    "codebase. Answer with concrete, complete artifacts (code blocks with file "
    "paths), never with placeholders or pseudocode. Respect the interfaces and "
    "files described in the shared context so other workers can integrate your "
    "output. When you finish, list the files you created or modified."
)


class AgentTaskError(ArenaError):
    """Raised when every agent in the pool failed to complete a task."""


class AgentPool:
    """Schedule work across a set of worker agents.

    Args:
        accounts: Account dicts (or settings account models).
        settings: Optional settings override.
        client_factory: Injection point used by tests to supply fake clients.
    """

    def __init__(
        self,
        accounts: Optional[List[Any]] = None,
        settings: Optional[Settings] = None,
        client_factory: Optional[Any] = None,
    ) -> None:
        self.settings = settings or get_settings()
        raw_accounts = accounts if accounts is not None else self.settings.arena_account_list()
        self._client_factory = client_factory
        self._agents: Dict[str, ArenaClient] = {}
        self._initialized = False
        self._lock = asyncio.Lock()

        for account in raw_accounts:
            agent = self._make_client(account)
            self._agents[agent.account_id] = agent

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _make_client(self, account: Any) -> ArenaClient:
        """Instantiate an agent for ``account`` (honouring the factory hook)."""
        if self._client_factory is not None:
            return self._client_factory(account, self.settings)
        return ArenaClient.from_config(account, settings=self.settings)

    @property
    def agents(self) -> List[ArenaClient]:
        """All agents in the pool."""
        return list(self._agents.values())

    @property
    def size(self) -> int:
        """Number of agents in the pool."""
        return len(self._agents)

    def is_configured(self) -> bool:
        """Return ``True`` when at least one agent is configured."""
        return bool(self._agents)

    def get_agent(self, account_id: str) -> Optional[ArenaClient]:
        """Return the agent with ``account_id`` when present."""
        return self._agents.get(account_id)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def initialize(self) -> Dict[str, Any]:
        """Authenticate and health check every agent in parallel.

        Returns:
            ``{initialized, agents, ready, failed, details}``.
        """
        if not self._agents:
            LOGGER.warning("Agent pool is empty: configure ARENA_ACCOUNTS to add workers")
            self._initialized = True
            return {"initialized": True, "agents": 0, "ready": 0, "failed": 0, "details": []}

        async def prepare(agent: ArenaClient) -> Dict[str, Any]:
            """Authenticate and probe a single agent."""
            try:
                await agent.authenticate()
                ready = await agent.is_ready()
                return {"account_id": agent.account_id, "label": agent.label, "ready": ready, "error": ""}
            except Exception as exc:  # noqa: BLE001 - reported in the summary
                agent.last_error = str(exc)
                LOGGER.error("Agent %s failed to initialise: %s", agent.label, exc)
                return {"account_id": agent.account_id, "label": agent.label, "ready": False, "error": str(exc)}

        details = await asyncio.gather(*(prepare(agent) for agent in self._agents.values()))
        self._initialized = True
        ready = sum(1 for detail in details if detail["ready"])
        LOGGER.info("Agent pool initialised: %s/%s agents ready", ready, len(details))
        return {
            "initialized": True,
            "agents": len(details),
            "ready": ready,
            "failed": len(details) - ready,
            "details": list(details),
        }

    async def close(self) -> None:
        """Close every agent's HTTP client."""
        await asyncio.gather(*(agent.close() for agent in self._agents.values()), return_exceptions=True)

    async def ensure_initialized(self) -> None:
        """Initialise the pool on first use."""
        if self._initialized:
            return
        async with self._lock:
            if not self._initialized:
                await self.initialize()

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    async def get_available_agent(self, exclude: Optional[List[str]] = None) -> Optional[ArenaClient]:
        """Return the best agent able to take work right now.

        Selection order:

        1. Ready agents (not busy, not rate limited), least recently used first.
        2. When none are ready, ``None`` -- callers decide whether to wait.

        Args:
            exclude: Account ids to skip (already failed this task).

        Returns:
            An :class:`ArenaClient`, or ``None`` when the pool is saturated.
        """
        await self.ensure_initialized()
        skipped = set(exclude or [])
        ready: List[ArenaClient] = []
        for agent in self._agents.values():
            if agent.account_id in skipped:
                continue
            if await agent.is_ready():
                ready.append(agent)
        if not ready:
            LOGGER.debug("No agent available (pool size %s, excluded %s)", self.size, len(skipped))
            return None
        ready.sort(key=lambda agent: (agent.last_used_at, agent.tasks_done))
        return ready[0]

    async def wait_for_agent(self, timeout: Optional[float] = None) -> Optional[ArenaClient]:
        """Wait until an agent becomes available (or the timeout expires).

        Args:
            timeout: Maximum seconds to wait; ``None`` waits forever.

        Returns:
            An available agent, or ``None`` on timeout.
        """
        deadline = None if timeout is None else time.time() + timeout
        delay = 1.0
        while True:
            agent = await self.get_available_agent()
            if agent is not None:
                return agent
            if deadline is not None and time.time() >= deadline:
                return None
            await asyncio.sleep(min(delay, 10.0))
            delay = min(delay * 2, 10.0)

    # ------------------------------------------------------------------
    # Task execution
    # ------------------------------------------------------------------
    def build_prompt(self, task: Dict[str, Any], context: str) -> str:
        """Combine a task and the shared context into one prompt.

        Args:
            task: Task dict with at least ``title``/``description``.
            context: Shared context block (from the brain or the state manager).

        Returns:
            The full prompt sent to the agent.
        """
        task_id = task.get("id") or task.get("task_id") or "task"
        title = task.get("title") or "Untitled task"
        description = task.get("description") or ""
        dependencies = task.get("dependencies") or []
        deliverable = task.get("deliverable") or ""

        parts: List[str] = []
        if context.strip():
            parts.append(context.strip())
            parts.append("")
        parts.append("## Your task")
        parts.append(f"**ID:** {task_id}")
        parts.append(f"**Title:** {title}")
        parts.append("")
        parts.append(description.strip() or "No further description was provided.")
        if deliverable:
            parts.append("")
            parts.append(f"**Expected deliverable:** {deliverable}")
        if dependencies:
            parts.append("")
            parts.append(
                "**Depends on:** "
                + ", ".join(str(dep) for dep in dependencies)
                + " (assume those pieces already exist)"
            )
        parts.append("")
        parts.append("## Output contract")
        parts.append("")
        parts.append(
            "Reply in markdown. For every file you create, emit a fenced code block whose "
            "info string is the file path, for example:\n\n"
            "```python path=src/example.py\n"
            "# complete file contents\n"
            "```\n\n"
            "Do not truncate files and do not use `...` placeholders."
        )
        return "\n".join(parts)

    async def assign_task(
        self,
        task: Dict[str, Any],
        context: str = "",
        agent: Optional[ArenaClient] = None,
        max_attempts: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """Run one task on the best available agent, with retries and fallback.

        Args:
            task: Task dict (``id``, ``title``, ``description``, ...).
            context: Shared context injected into the prompt.
            agent: Force a specific agent.
            max_attempts: How many different agents to try (default
                ``AGENT_MAX_RETRIES + 1``).
            timeout: Per-attempt timeout in seconds (default
                ``TASK_TIMEOUT_SECONDS``).

        Returns:
            The agent's raw response text.

        Raises:
            ConfigurationError: When the pool has no agents at all.
            AgentTaskError: When every attempt failed.
        """
        await self.ensure_initialized()
        if not self._agents:
            raise ConfigurationError(
                "No worker agents configured. Set ARENA_ACCOUNTS in .env to a JSON list of "
                "accounts (email + session_token or base_url + key)."
            )

        attempts = max_attempts or (int(self.settings.AGENT_MAX_RETRIES) + 1)
        attempt_timeout = timeout or float(self.settings.TASK_TIMEOUT_SECONDS)
        prompt = self.build_prompt(task, context)
        tried: List[str] = []
        errors: List[str] = []

        for attempt in range(1, attempts + 1):
            candidate = agent if (agent is not None and attempt == 1) else await self.get_available_agent(exclude=tried)
            if candidate is None:
                waited = await self.wait_for_agent(timeout=min(attempt_timeout / 2, 120.0))
                candidate = waited
            if candidate is None:
                errors.append(f"attempt {attempt}: no agent became available")
                LOGGER.warning("Task %s: no agent available on attempt %s", task.get("id"), attempt)
                continue

            tried.append(candidate.account_id)
            try:
                LOGGER.info(
                    "Dispatching task %s to agent %s (attempt %s/%s)",
                    task.get("id"),
                    candidate.label,
                    attempt,
                    attempts,
                )
                return await asyncio.wait_for(
                    candidate.send_prompt(prompt, use_agent_mode=True, system_prompt=WORKER_SYSTEM_PROMPT),
                    timeout=attempt_timeout,
                )
            except TimeoutError:
                candidate.last_error = f"timed out after {attempt_timeout:.0f}s"
                errors.append(f"{candidate.label}: {candidate.last_error}")
                LOGGER.error("Task %s timed out on agent %s", task.get("id"), candidate.label)
            except RateLimitError as exc:
                candidate.apply_rate_limit(exc.retry_after)
                errors.append(f"{candidate.label}: rate limited ({exc})")
                LOGGER.warning("Agent %s rate limited; cooling down", candidate.label)
            except AuthenticationError as exc:
                errors.append(f"{candidate.label}: auth failed ({exc})")
                LOGGER.error("Agent %s authentication failed: %s", candidate.label, exc)
                await self._try_refresh(candidate)
            except Exception as exc:  # noqa: BLE001 - try the next agent
                errors.append(f"{candidate.label}: {exc}")
                LOGGER.error("Task %s failed on agent %s: %s", task.get("id"), candidate.label, exc)

        raise AgentTaskError(
            f"Task {task.get('id')} failed on every agent after {len(tried)} attempt(s)",
            task_id=task.get("id"),
            errors=errors,
        )

    async def _try_refresh(self, agent: ArenaClient) -> None:
        """Attempt to refresh an agent's session after an auth failure."""
        try:
            refreshed = await agent.reset_session()
            LOGGER.info("Session refresh for %s: %s", agent.label, "ok" if refreshed else "failed")
        except Exception as exc:  # noqa: BLE001 - best effort only
            LOGGER.debug("Session refresh for %s raised: %s", agent.label, exc)

    async def assign_many(
        self,
        jobs: List[Dict[str, Any]],
        max_concurrency: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Run several ``{task, context}`` jobs concurrently.

        Args:
            jobs: List of ``{"task": dict, "context": str}`` entries.
            max_concurrency: Cap on simultaneous prompts (defaults to the
                pool size).

        Returns:
            One result dict per job: ``{task_id, success, output, error, agent_id}``.
        """
        if not jobs:
            return []
        limit = max(1, max_concurrency or self.size or 1)

        async def run(job: Dict[str, Any]) -> Dict[str, Any]:
            task = job.get("task") or {}
            try:
                output = await self.assign_task(task, job.get("context", ""))
                return {
                    "task_id": task.get("id"),
                    "success": True,
                    "output": output,
                    "error": "",
                    "agent_id": job.get("agent_id", ""),
                }
            except Exception as exc:  # noqa: BLE001 - reported per job
                return {
                    "task_id": task.get("id"),
                    "success": False,
                    "output": "",
                    "error": str(exc),
                    "agent_id": job.get("agent_id", ""),
                }

        semaphore = asyncio.Semaphore(limit)

        async def guarded(job: Dict[str, Any]) -> Dict[str, Any]:
            async with semaphore:
                return await run(job)

        return list(await asyncio.gather(*(guarded(job) for job in jobs)))

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    async def get_pool_status(self, probe: bool = False) -> List[Dict[str, Any]]:
        """Return a status entry for every agent.

        Args:
            probe: Include a lightweight liveness HTTP probe per agent.

        Returns:
            ``[{account_id, label, status, tasks_done, tasks_failed, ...}]``.
        """
        statuses = []
        for agent in self._agents.values():
            if probe:
                statuses.append(await agent.get_session_status(probe=True))
            else:
                statuses.append(agent.stats())
        return statuses

    async def refresh_all_sessions(self, force: bool = False) -> Dict[str, Any]:
        """Re-authenticate agents with expired or missing sessions.

        Args:
            force: Reset every session, even healthy ones.

        Returns:
            ``{checked, refreshed, failed, details}``.
        """
        checked = 0
        refreshed = 0
        details: List[Dict[str, Any]] = []
        for agent in self._agents.values():
            checked += 1
            needs_refresh = force or not agent.is_authenticated() or agent.is_rate_limited()
            if not needs_refresh:
                details.append({"account_id": agent.account_id, "label": agent.label, "refreshed": False})
                continue
            try:
                ok = await agent.reset_session()
                refreshed += 1 if ok else 0
                details.append({"account_id": agent.account_id, "label": agent.label, "refreshed": ok})
            except Exception as exc:  # noqa: BLE001 - reported per agent
                details.append(
                    {"account_id": agent.account_id, "label": agent.label, "refreshed": False, "error": str(exc)}
                )
        LOGGER.info("Session refresh pass: %s/%s refreshed", refreshed, checked)
        return {"checked": checked, "refreshed": refreshed, "failed": checked - refreshed, "details": details}

    def snapshot(self) -> List[Dict[str, Any]]:
        """Return a synchronous snapshot of pool statistics."""
        return [agent.stats() for agent in self._agents.values()]


__all__ = ["AgentPool", "AgentTaskError", "WORKER_SYSTEM_PROMPT"]
