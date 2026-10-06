"""The orchestration brain: a small LLM that plans, reviews and summarises.

The brain is the only *thinking* component in Kollektiv. It talks to any
OpenAI-compatible chat completions endpoint -- DeepSeek by default, Groq as a
fallback -- and is responsible for:

* :meth:`split_task` -- turning a project brief into N subtasks.
* :meth:`summarize_state` -- compressing the shared state into a short context.
* :meth:`review_output` -- scoring a worker's output and deciding on retries.
* :meth:`generate_context_for_agent` -- building the prompt a worker receives.

Every method degrades to a deterministic heuristic when the API key is missing
or the API is unreachable, so the orchestrator never hard-blocks on the brain.

Usage::

    brain = OrchestratorBrain(settings)
    subtasks = await brain.split_task("Build a URL shortener", n_agents=3)
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional

from openai import APIError, AsyncOpenAI
from openai import AuthenticationError as OpenAIAuthError
from openai import RateLimitError as OpenAIRateLimitError

from config.settings import Settings, get_settings
from src.utils.errors import BrainError, BrainTransientError, ConfigurationError, RateLimitError
from src.utils.logger import get_logger
from src.utils.retry import async_retry

LOGGER = get_logger(__name__)

#: Provider defaults used when a custom ``BRAIN_PROVIDER`` is configured.
PROVIDER_DEFAULTS: Dict[str, Dict[str, str]] = {
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "model": "deepseek/deepseek-chat"},
    "together": {"base_url": "https://api.together.xyz/v1", "model": "deepseek-ai/DeepSeek-V3"},
    "ollama": {"base_url": "http://localhost:11434/v1", "model": "llama3.1"},
}

PLANNER_SYSTEM_PROMPT = (
    "You are the planning brain of a multi-agent software team. You decompose a project "
    "into independent, parallelisable subtasks with clear interfaces so several workers can "
    "build them simultaneously without stepping on each other. You always answer with JSON "
    "only -- no prose, no markdown fences."
)

REVIEW_SYSTEM_PROMPT = (
    "You are a strict but fair technical reviewer. You score a worker's output against the "
    "task it was given and reply with JSON only."
)


class OrchestratorBrain:
    """LLM-backed planner/reviewer.

    Args:
        settings: Optional settings override.
        provider: Override ``BRAIN_PROVIDER``.
        api_key: Override ``BRAIN_API_KEY``.
        model: Override ``BRAIN_MODEL``.
        base_url: Override ``BRAIN_BASE_URL``.
        client_factory: Injection point used by tests to supply a fake client.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        client_factory: Optional[Any] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.provider = (provider or self.settings.BRAIN_PROVIDER or "deepseek").lower()
        defaults = PROVIDER_DEFAULTS.get(self.provider, PROVIDER_DEFAULTS["deepseek"])

        self.api_key = api_key if api_key is not None else self.settings.BRAIN_API_KEY
        self.model = model or self.settings.BRAIN_MODEL or defaults["model"]
        self.base_url = base_url or self.settings.BRAIN_BASE_URL or defaults["base_url"]

        self.fallback_api_key = self.settings.BRAIN_FALLBACK_API_KEY
        self.fallback_model = self.settings.BRAIN_FALLBACK_MODEL
        self.fallback_base_url = self.settings.BRAIN_FALLBACK_BASE_URL
        self.fallback_provider = self.settings.BRAIN_FALLBACK_PROVIDER

        self._client_factory = client_factory
        self._client: Optional[AsyncOpenAI] = None
        self._fallback_client: Optional[AsyncOpenAI] = None
        self.calls = 0
        self.failures = 0
        self.last_error: str = ""

    # ------------------------------------------------------------------
    # Client management
    # ------------------------------------------------------------------
    @property
    def is_configured(self) -> bool:
        """Return ``True`` when an API key is available."""
        return bool(self.api_key)

    def _build_client(self, api_key: str, base_url: str) -> Any:
        """Create an OpenAI-compatible async client (or the test double)."""
        if self._client_factory is not None:
            return self._client_factory(api_key, base_url, self.settings)
        return AsyncOpenAI(
            api_key=api_key or "not-needed",
            base_url=base_url,
            timeout=self.settings.BRAIN_TIMEOUT,
            max_retries=0,  # Kollektiv owns the retry policy
        )

    @property
    def client(self) -> Any:
        """The primary LLM client, created on first use."""
        if self._client is None:
            if not self.api_key:
                raise ConfigurationError(
                    "BRAIN_API_KEY is not set. Add a DeepSeek (or Groq) key to .env to enable "
                    "LLM planning; Kollektiv will otherwise use built-in heuristics."
                )
            self._client = self._build_client(self.api_key, self.base_url)
        return self._client

    @property
    def fallback_client(self) -> Optional[Any]:
        """The fallback provider's client, or ``None`` when unconfigured."""
        if not self.fallback_api_key:
            return None
        if self._fallback_client is None:
            self._fallback_client = self._build_client(self.fallback_api_key, self.fallback_base_url)
        return self._fallback_client

    async def close(self) -> None:
        """Close the underlying HTTP clients."""
        for client in (self._client, self._fallback_client):
            close = getattr(client, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - closing is best effort
                LOGGER.debug("Closing the brain client raised: %s", exc)

    # ------------------------------------------------------------------
    # Raw completion
    # ------------------------------------------------------------------
    @async_retry(max_retries=3, base_delay=2.0, max_delay=30.0)
    async def complete(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> str:
        """Run a single chat completion.

        Args:
            prompt: The user message.
            system_prompt: Optional system message.
            temperature: Sampling temperature (defaults to ``BRAIN_TEMPERATURE``).
            max_tokens: Output cap (defaults to ``BRAIN_MAX_TOKENS``).
            json_mode: Request a JSON object response where supported.

        Returns:
            The assistant message content.

        Raises:
            ConfigurationError: When no API key is configured.
            RateLimitError: When the provider rate limits the request.
            BrainError: For any other API failure.
        """
        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.settings.BRAIN_TEMPERATURE if temperature is None else temperature,
            "max_tokens": max_tokens or self.settings.BRAIN_MAX_TOKENS,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        self.calls += 1
        try:
            return await self._chat(self.client, kwargs)
        except OpenAIRateLimitError as exc:
            self.failures += 1
            self.last_error = str(exc)
            raise RateLimitError(f"{self.provider} rate limit: {exc}", retry_after=5.0) from exc
        except OpenAIAuthError as exc:
            self.failures += 1
            self.last_error = str(exc)
            raise ConfigurationError(f"{self.provider} rejected the API key: {exc}") from exc
        except (APIError, Exception) as exc:  # noqa: BLE001 - fallback is intentional
            self.failures += 1
            self.last_error = str(exc)
            fallback = self.fallback_client
            if fallback is not None:
                LOGGER.warning(
                    "Primary brain (%s) failed (%s); retrying on the %s fallback",
                    self.provider,
                    exc,
                    self.fallback_provider,
                )
                try:
                    retry_kwargs = dict(kwargs)
                    retry_kwargs["model"] = self.fallback_model
                    return await self._chat(fallback, retry_kwargs)
                except Exception as fallback_exc:  # noqa: BLE001 - report both failures
                    self.last_error = f"{exc} / fallback: {fallback_exc}"
                    raise _classify_brain_error(
                        f"Both {self.provider} and {self.fallback_provider} failed",
                        exc,
                        str(fallback_exc),
                    ) from fallback_exc
            raise _classify_brain_error(f"{self.provider} completion failed: {exc}", exc) from exc

    async def _chat(self, client: Any, kwargs: Dict[str, Any]) -> str:
        """Call the client's chat completion endpoint and extract the text."""
        response = await client.chat.completions.create(**kwargs)
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise BrainError("The LLM returned no choices")
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None) if message is not None else None
        if content is None and isinstance(choices[0], dict):  # dict-style test doubles
            content = (choices[0].get("message") or {}).get("content")
        if content is None:
            raise BrainError("The LLM returned an empty message")
        return str(content)

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------
    async def split_task(self, task_description: str, n_agents: int) -> List[Dict[str, Any]]:
        """Split a project brief into ``n_agents`` subtasks.

        Args:
            task_description: The project brief.
            n_agents: Desired number of subtasks (also the worker count).

        Returns:
            ``[{id, title, description, dependencies, priority, deliverable}]``.
            Falls back to a heuristic plan when the LLM is unavailable.
        """
        n = max(1, int(n_agents))
        if not task_description.strip():
            raise ValueError("task_description must not be empty")

        if not self.is_configured:
            LOGGER.warning("Brain is not configured; using the heuristic planner")
            return self._heuristic_split(task_description, n)

        prompt = self._planning_prompt(task_description, n)
        try:
            raw = await self.complete(prompt, system_prompt=PLANNER_SYSTEM_PROMPT, json_mode=True)
            subtasks = self._parse_subtasks(raw, n)
            if subtasks:
                return subtasks
            LOGGER.warning("The LLM returned an unusable plan; falling back to heuristics")
        except (BrainError, RateLimitError, ConfigurationError) as exc:
            LOGGER.error("Task splitting failed (%s); falling back to heuristics", exc)
        return self._heuristic_split(task_description, n)

    @staticmethod
    def _planning_prompt(task_description: str, n: int) -> str:
        """Build the planning prompt."""
        return (
            f"Project brief:\n{task_description.strip()}\n\n"
            f"Decompose this into exactly {n} subtasks that can be built in parallel by {n} workers "
            "sharing one repository. Requirements:\n"
            "1. Every subtask must produce concrete files (give the file paths).\n"
            "2. Define the interfaces between subtasks explicitly (function signatures, "
            "CLI flags, API shapes, data schemas) so parallel work integrates.\n"
            "3. Use `dependencies` (subtask ids) only when a subtask genuinely cannot start "
            "before another finishes.\n"
            "4. Order subtasks so foundations (schemas, config, core types) come first.\n\n"
            "Reply with JSON of this exact shape:\n"
            '{"subtasks": [{"id": "t1", "title": "short title", '
            '"description": "precise instructions, including file paths and interface contracts", '
            '"dependencies": [], "priority": 3, "deliverable": "files produced"}]}'
        )

    @staticmethod
    def _parse_subtasks(raw: str, n: int) -> List[Dict[str, Any]]:
        """Parse and normalise the planner's JSON answer."""
        data = _loads_lenient(raw)
        if isinstance(data, dict):
            items = data.get("subtasks") or data.get("tasks") or data.get("plan") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []
        if not isinstance(items, list):
            return []

        subtasks: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("id") or f"t{index}")
            if task_id in seen_ids:
                task_id = f"{task_id}-{index}"
            seen_ids.add(task_id)
            dependencies = item.get("dependencies") or item.get("depends_on") or []
            if isinstance(dependencies, str):
                dependencies = [dep.strip() for dep in dependencies.split(",") if dep.strip()]
            elif not isinstance(dependencies, list):
                dependencies = []
            try:
                priority = int(item.get("priority", 3))
            except (TypeError, ValueError):
                priority = 3
            subtasks.append(
                {
                    "id": task_id,
                    "title": str(item.get("title") or f"Subtask {index}").strip()[:300],
                    "description": str(item.get("description") or item.get("details") or "").strip(),
                    "dependencies": [str(dep) for dep in dependencies],
                    "priority": max(1, min(5, priority)),
                    "deliverable": str(item.get("deliverable") or "").strip(),
                    "status": "pending",
                }
            )
        # Drop dependencies pointing at unknown ids (the LLM sometimes invents them).
        known = {task["id"] for task in subtasks}
        for task in subtasks:
            task["dependencies"] = [dep for dep in task["dependencies"] if dep in known and dep != task["id"]]
        return subtasks

    @staticmethod
    def _heuristic_split(task_description: str, n: int) -> List[Dict[str, Any]]:
        """Produce a deterministic fallback plan without an LLM.

        The plan follows the phases any software project needs: discovery,
        interfaces, implementation, integration, tests and documentation. It is
        deliberately generic -- a worker with a good prompt can still execute it.
        """
        brief = task_description.strip()
        phases = [
            (
                "Requirements and file layout",
                "Restate the goal, list the files to create and the exact interfaces between them "
                "(function signatures, CLI flags, JSON schemas).",
                "docs/PLAN.md",
            ),
            (
                "Core implementation",
                "Implement the core modules named in docs/PLAN.md, with complete code and no placeholders.",
                "core module files",
            ),
            (
                "Data and configuration layer",
                "Implement configuration loading, persistence and any external client wrappers described in the plan.",
                "config and storage modules",
            ),
            (
                "Command line / HTTP interface",
                "Expose the core functionality through the interface defined in docs/PLAN.md.",
                "entry point",
            ),
            (
                "Tests",
                "Write pytest tests covering the happy path and the two most likely failure modes.",
                "tests/",
            ),
            (
                "Documentation and packaging",
                "Write the README, usage examples and packaging metadata.",
                "README.md",
            ),
            (
                "Integration polish",
                "Wire the pieces together, remove duplication and confirm imports resolve.",
                "integration commit",
            ),
            (
                "Hardening",
                "Add input validation, retries and logging around every external call.",
                "hardening commit",
            ),
        ]
        count = max(1, min(n, len(phases)))
        chosen = phases[:count]
        subtasks: List[Dict[str, Any]] = []
        for index, (title, description, deliverable) in enumerate(chosen, start=1):
            subtasks.append(
                {
                    "id": f"t{index}",
                    "title": title,
                    "description": f"{description}\n\nProject brief:\n{brief[:2000]}",
                    "dependencies": [] if index == 1 else [f"t{index - 1}"] if index == 2 else [],
                    "priority": 5 if index == 1 else 3,
                    "deliverable": deliverable,
                    "status": "pending",
                }
            )
        return subtasks

    # ------------------------------------------------------------------
    # Summarisation
    # ------------------------------------------------------------------
    async def summarize_state(self, state: Dict[str, Any], max_chars: int = 2500) -> str:
        """Compress the full project state into a short context string.

        Args:
            state: The parsed ``PROJECT_STATE.md`` dict.
            max_chars: Target length for the compressed summary.

        Returns:
            A concise summary; deterministic when the brain is unconfigured.
        """
        compact = self._compact_state(state)
        if not self.is_configured:
            return compact[:max_chars]

        prompt = (
            "Compress this project state into at most 200 words for another AI worker. Keep "
            "exact file paths, function/endpoint names, task ids and any blocking problems. "
            "Drop chatter.\n\nState JSON:\n"
            f"{json.dumps(compact, default=str)[:12000]}"
        )
        try:
            summary = await self.complete(prompt, temperature=0.0, max_tokens=600)
            return summary.strip()[:max_chars] or compact[:max_chars]
        except (BrainError, RateLimitError, ConfigurationError) as exc:
            LOGGER.warning("State summarisation failed (%s); using the deterministic summary", exc)
            return compact[:max_chars]

    @staticmethod
    def _compact_state(state: Dict[str, Any]) -> str:
        """Build a deterministic, dependency-free state summary."""
        lines: List[str] = []
        if state.get("project_name"):
            lines.append(f"Project: {state['project_name']}")
        if state.get("last_commit"):
            lines.append(f"Last commit: {state['last_commit']}")
        tasks = state.get("tasks") or []
        if tasks:
            counts: Dict[str, int] = {}
            for task in tasks:
                key = str(task.get("status", "unknown"))
                counts[key] = counts.get(key, 0) + 1
            lines.append("Tasks: " + ", ".join(f"{value} {key}" for key, value in sorted(counts.items())))
            for task in tasks:
                lines.append(
                    f"- [{task.get('status')}] {task.get('id')}: {str(task.get('title'))[:120]}"
                )
        files = state.get("files") or []
        if files:
            paths = [entry.get("path") if isinstance(entry, dict) else str(entry) for entry in files]
            lines.append("Files: " + ", ".join(path for path in paths[:40] if path))
        history = state.get("history") or []
        if history:
            lines.append("Recent activity:")
            for event in history[-8:]:
                lines.append(
                    f"- {event.get('agent_id') or 'system'} {event.get('action')}: "
                    f"{str(event.get('result'))[:160]}"
                )
        return "\n".join(lines) if lines else "No project state recorded yet."

    # ------------------------------------------------------------------
    # Review
    # ------------------------------------------------------------------
    async def review_output(self, task: Dict[str, Any], output: str) -> Dict[str, Any]:
        """Score a worker's output and decide whether it needs a retry.

        Args:
            task: The task the output answers.
            output: The raw worker output.

        Returns:
            ``{score, feedback, needs_retry, strengths, issues}`` with ``score``
            in ``[0, 1]``. ``needs_retry`` is true below a 0.6 threshold.
        """
        if not output or not output.strip():
            return {
                "score": 0.0,
                "feedback": "The agent returned an empty response.",
                "needs_retry": True,
                "strengths": [],
                "issues": ["empty output"],
            }

        if not self.is_configured:
            return self._heuristic_review(task, output)

        prompt = (
            "Task given to a worker:\n"
            f"Title: {task.get('title', '')}\nDescription: {str(task.get('description', ''))[:2000]}\n"
            f"Expected deliverable: {task.get('deliverable', '')}\n\n"
            f"Worker output (truncated to 8000 chars):\n{output[:8000]}\n\n"
            "Score the output from 0 to 1 on completeness, correctness and integrability. "
            "Reply with JSON only:\n"
            '{"score": 0.0, "feedback": "what to fix, specific and actionable", '
            '"strengths": ["..."], "issues": ["..."], "needs_retry": true}'
        )
        try:
            raw = await self.complete(prompt, system_prompt=REVIEW_SYSTEM_PROMPT, json_mode=True, temperature=0.0)
            data = _loads_lenient(raw)
            if isinstance(data, dict):
                score = _clamp_score(data.get("score"))
                feedback = str(data.get("feedback") or "").strip()
                issues = data.get("issues") or []
                strengths = data.get("strengths") or []
                return {
                    "score": score,
                    "feedback": feedback or "No feedback provided.",
                    "needs_retry": bool(data.get("needs_retry", score < 0.6)),
                    "strengths": [str(item) for item in strengths][:10],
                    "issues": [str(item) for item in issues][:10],
                }
        except (BrainError, RateLimitError, ConfigurationError) as exc:
            LOGGER.warning("Output review failed (%s); using the heuristic reviewer", exc)
        return self._heuristic_review(task, output)

    @staticmethod
    def _heuristic_review(task: Dict[str, Any], output: str) -> Dict[str, Any]:
        """Score an output without an LLM, using structural signals."""
        score = 0.5
        issues: List[str] = []
        strengths: List[str] = []

        code_blocks = re.findall(r"```", output)
        blocks = len(code_blocks) // 2
        has_paths = bool(
            re.search(r"```[a-zA-Z0-9_+-]*\s*(?:path|file|filename)\s*=", output)
            or re.search(r"```[\w./-]+\.\w+", output)
        )
        if blocks >= 1:
            score += 0.15
            strengths.append(f"{blocks} code block(s) provided")
        else:
            score -= 0.2
            issues.append("no code blocks found")

        if has_paths:
            score += 0.15
            strengths.append("file paths attached to code blocks")
        else:
            issues.append("code blocks lack explicit file paths")

        if re.search(r"\.\.\.\s*(rest|unchanged|code omitted|truncated)", output, re.IGNORECASE) or "TODO" in output:
            score -= 0.25
            issues.append("output contains placeholders or TODOs")
        # Short prose with no artifacts is weak; a short *file* is not.
        if blocks == 0 and len(output) < 200:
            score -= 0.25
            issues.append("response is suspiciously short")
        elif len(output) > 1500:
            score += 0.05
        # Structural signals are judged on the prose only: a "# TODO: ..."
        # comment inside a code block is not document structure.
        prose = re.sub(r"```.*?```", "", output, flags=re.DOTALL)
        if re.search(r"^#{1,6}\s+\S", prose, re.MULTILINE):
            score += 0.05
            strengths.append("structured with headings")

        # A dependency it was told to rely on is mentioned: good integration signal.
        for dep in task.get("dependencies") or []:
            if str(dep) in output:
                score += 0.05
                strengths.append(f"references dependency {dep}")

        score = _clamp_score(score)
        feedback = (
            "Heuristic review (the LLM brain is not configured). "
            + ("Looks complete." if score >= 0.6 else "Needs another pass.")
        )
        if issues:
            feedback += " Issues: " + "; ".join(issues[:4]) + "."
        return {
            "score": score,
            "feedback": feedback,
            "needs_retry": score < 0.6,
            "strengths": strengths,
            "issues": issues,
        }

    # ------------------------------------------------------------------
    # Agent context
    # ------------------------------------------------------------------
    async def generate_context_for_agent(self, agent_id: str, task: Dict[str, Any], state: Dict[str, Any]) -> str:
        """Build the complete prompt context for one worker.

        Args:
            agent_id: The worker's identifier.
            task: The task being dispatched.
            state: The current project state.

        Returns:
            A markdown context block containing the task, what other agents
            built, the existing files and the constraints of the shared repo.
        """
        parts: List[str] = ["## Mission", ""]
        project = state.get("project_name") or state.get("project_id") or "the project"
        parts.append(f"You are **{agent_id}**, one worker among several building {project}.")
        if state.get("description"):
            parts.append(f"Overall goal: {str(state['description'])[:600]}")
        parts.append("")

        summary = await self.summarize_state(state)
        parts.append("## Where the project stands")
        parts.append("")
        parts.append(summary)
        parts.append("")

        parts.append("## Your assignment")
        parts.append("")
        parts.append(f"- Task id: `{task.get('id', '?')}`")
        parts.append(f"- Title: {task.get('title', '')}")
        parts.append("")
        parts.append(str(task.get("description", "")).strip() or "No further description provided.")
        if task.get("deliverable"):
            parts.append("")
            parts.append(f"**Deliverable:** {task['deliverable']}")
        if task.get("dependencies"):
            parts.append("")
            parts.append(
                "**Already handled by teammates (assume they exist):** "
                + ", ".join(str(dep) for dep in task["dependencies"])
            )
        parts.append("")

        files = state.get("files") or []
        if files:
            parts.append("## Files already in the repository")
            parts.append("")
            for entry in files[:40]:
                if isinstance(entry, dict):
                    parts.append(f"- `{entry.get('path') or entry.get('name', '?')}`")
                else:
                    parts.append(f"- `{entry}`")
            parts.append("")

        constraints = [
            "Stay inside your task; do not rewrite files owned by other tasks.",
            "Follow the interfaces described above exactly -- teammates code against them.",
            "Emit complete files in fenced code blocks tagged with their path, e.g. ```python path=src/app.py",
            "No placeholders, no `...`, no pseudocode: the output is applied to a real repository.",
        ]
        parts.append("## Constraints")
        parts.append("")
        for item in constraints:
            parts.append(f"- {item}")
        parts.append("")

        if self.is_configured and state.get("tasks"):
            # Ask the LLM for one extra paragraph of tactical advice. Failures
            # are non-fatal: the deterministic context above is already complete.
            advice_prompt = (
                f"Worker {agent_id} is about to do task '{task.get('title')}'.\n"
                f"Task description: {str(task.get('description'))[:1500]}\n\n"
                f"Project state summary:\n{summary[:2500]}\n\n"
                "In under 120 words, give this worker the three most important tactical pointers "
                "to avoid conflicts and integrate cleanly with the other workers. Plain text only."
            )
            try:
                advice = (await self.complete(advice_prompt, temperature=0.2, max_tokens=350)).strip()
                if advice:
                    parts.append("## Tactical advice from the orchestrator")
                    parts.append("")
                    parts.append(advice)
                    parts.append("")
            except (BrainError, RateLimitError, ConfigurationError) as exc:
                LOGGER.debug("Tactical advice unavailable: %s", exc)

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Extras used by the sync engine / collector
    # ------------------------------------------------------------------
    async def summarize_diff(self, diff: str, max_words: int = 120) -> str:
        """Summarise a git diff for the shared state.

        Args:
            diff: Unified diff text.
            max_words: Target summary length.

        Returns:
            A short human readable summary (deterministic fallback on failure).
        """
        if not diff.strip():
            return "Empty diff."
        if not self.is_configured:
            return _fallback_diff_summary(diff, max_words)
        prompt = (
            f"Summarise this git diff in at most {max_words} words for teammates who need to know "
            "what changed and which files/functions are affected. Plain text.\n\n"
            f"{diff[:12000]}"
        )
        try:
            summary = await self.complete(prompt, temperature=0.0, max_tokens=300)
            return summary.strip() or _fallback_diff_summary(diff, max_words)
        except (BrainError, RateLimitError, ConfigurationError) as exc:
            LOGGER.warning("Diff summarisation failed (%s)", exc)
            return _fallback_diff_summary(diff, max_words)

    async def review_pr(self, diff: str, pr_title: str = "") -> Dict[str, Any]:
        """Review a pull request diff and return actionable feedback.

        Returns:
            ``{score, summary, feedback, risks}``.
        """
        if not self.is_configured:
            files = _diff_files(diff)
            return {
                "score": 0.7,
                "summary": f"{len(files)} file(s) changed: {', '.join(files[:8])}",
                "feedback": "LLM review unavailable (BRAIN_API_KEY unset); only structural analysis performed.",
                "risks": [] if files else ["empty diff"],
            }
        prompt = (
            f"Pull request: {pr_title or '(untitled)'}\n\nDiff:\n{diff[:14000]}\n\n"
            "Review for correctness, security and integrability with the rest of the repository. "
            'Reply with JSON only: {"score": 0.0, "summary": "one sentence", '
            '"feedback": "what the author should change, concrete", "risks": ["..."]}'
        )
        try:
            raw = await self.complete(prompt, json_mode=True, temperature=0.0)
            data = _loads_lenient(raw)
            if isinstance(data, dict):
                return {
                    "score": _clamp_score(data.get("score")),
                    "summary": str(data.get("summary") or "")[:500],
                    "feedback": str(data.get("feedback") or "")[:2000],
                    "risks": [str(item) for item in (data.get("risks") or [])][:10],
                }
        except (BrainError, RateLimitError, ConfigurationError) as exc:
            LOGGER.warning("PR review failed (%s)", exc)
        files = _diff_files(diff)
        return {
            "score": 0.6,
            "summary": f"{len(files)} file(s) changed",
            "feedback": "Automated review failed; a human should look at this diff.",
            "risks": [],
        }

    def stats(self) -> Dict[str, Any]:
        """Return brain usage statistics."""
        return {
            "provider": self.provider,
            "model": self.model,
            "configured": self.is_configured,
            "fallback_provider": self.fallback_provider if self.fallback_api_key else "",
            "calls": self.calls,
            "failures": self.failures,
            "last_error": self.last_error,
        }


# ----------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------
def _loads_lenient(raw: str) -> Any:
    """Parse JSON from an LLM answer, tolerating code fences and stray prose.

    Args:
        raw: The model's raw answer.

    Returns:
        The parsed object, or an empty dict when nothing parseable is found.
    """
    if not raw:
        return {}
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            candidate = text[start : end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    LOGGER.warning("Could not parse JSON from the LLM answer: %s", raw[:200])
    return {}


def _classify_brain_error(message: str, exc: BaseException, fallback_message: str = "") -> BrainError:
    """Return a transient or permanent :class:`BrainError` for ``exc``.

    Connection problems and 5xx responses are retryable; authentication and
    request-shape problems are not.

    Args:
        message: Human readable failure description.
        exc: The original exception.
        fallback_message: Optional description of the fallback provider failure.

    Returns:
        A :class:`BrainError` (or :class:`BrainTransientError`) instance.
    """
    details: Dict[str, Any] = {"primary": str(exc)}
    if fallback_message:
        details["fallback"] = fallback_message
    status = getattr(exc, "status_code", None)
    is_connection = "Connection" in type(exc).__name__ or "Timeout" in type(exc).__name__
    if is_connection or (isinstance(status, int) and status >= 500):
        return BrainTransientError(message, **details)
    return BrainError(message, **details)


def _clamp_score(value: Any) -> float:
    """Coerce an LLM score into ``[0, 1]`` (accepting both 0-1 and 0-10 scales)."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.5
    if score > 1.0:
        score = score / 10.0 if score <= 10.0 else 1.0
    return round(max(0.0, min(1.0, score)), 3)


def _diff_files(diff: str) -> List[str]:
    """Extract the list of files touched by a unified diff."""
    return re.findall(r"^diff --git a/(\S+)", diff, re.MULTILINE)


def _fallback_diff_summary(diff: str, max_words: int = 120) -> str:
    """Summarise a diff structurally without an LLM."""
    files = _diff_files(diff)
    additions = len(re.findall(r"^\+(?!\+\+)", diff, re.MULTILINE))
    deletions = len(re.findall(r"^-(?!--)", diff, re.MULTILINE))
    summary = (
        f"{len(files)} file(s) changed (+{additions}/-{deletions} lines)"
        + (f": {', '.join(files[:10])}" if files else "")
    )
    return " ".join(summary.split()[: max(10, max_words)])


__all__ = ["OrchestratorBrain", "PROVIDER_DEFAULTS"]
