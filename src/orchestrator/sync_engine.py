"""Keep GitHub, TeraBox and the worker agents in agreement.

Three triggers drive synchronisation:

* ``POST /webhooks/github`` (push / pull_request) -> :meth:`on_push`,
  :meth:`on_pr`.
* A cron job every ``CRON_INTERVAL_MINUTES`` -> :meth:`cron_sync`.
* Manual ``POST /sync`` -> :meth:`cron_sync`.

Each pass follows the same shape: read the truth from GitHub, archive the
interesting parts to TeraBox, rewrite ``PROJECT_STATE.md`` and (optionally)
push the resulting context to the agents.

Usage::

    engine = SyncEngine(github, pool, state, brain)
    await engine.on_push(webhook_payload)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from config.settings import Settings, get_settings
from src.github.github_client import GitHubClient
from src.orchestrator.brain import OrchestratorBrain
from src.storage.pool_manager import TeraBoxPoolManager
from src.storage.state_manager import StateManager
from src.utils.errors import GitHubError, KollektivError
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)

#: Maximum archive size for a single commit snapshot (bytes).
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024


class SyncEngine:
    """Synchronise GitHub -> TeraBox -> agents.

    Args:
        github: GitHub REST client.
        pool: TeraBox storage pool (may be unconfigured; everything degrades).
        state: Shared project state manager.
        brain: The orchestration brain (used to summarise diffs/PRs).
        agent_pool: Optional agent pool used by :meth:`push_context_to_agents`.
        settings: Optional settings override.
    """

    def __init__(
        self,
        github: GitHubClient,
        pool: TeraBoxPoolManager,
        state: StateManager,
        brain: OrchestratorBrain,
        agent_pool: Optional[Any] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self.github = github
        self.pool = pool
        self.state = state
        self.brain = brain
        self.agent_pool = agent_pool
        self.settings = settings or get_settings()
        self.run_count = 0
        self.last_sync_at: Optional[datetime] = None
        self.last_error: str = ""
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Webhook entry points
    # ------------------------------------------------------------------
    async def on_push(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle a GitHub ``push`` event.

        Steps: fetch the pushed commits, archive a snapshot of the changed
        files to TeraBox, record the new HEAD in the state document and log an
        event to the history.

        Args:
            payload: The GitHub webhook payload.

        Returns:
            ``{handled, commits, archived, state_updated, branch, error}``.
        """
        async with self._lock:
            branch = (payload.get("ref") or "").replace("refs/heads/", "")
            commits_payload = payload.get("commits") or []
            head_sha = payload.get("after") or (commits_payload[-1]["id"] if commits_payload else "")
            result: Dict[str, Any] = {
                "handled": "push",
                "branch": branch,
                "commits": len(commits_payload),
                "archived": [],
                "state_updated": False,
                "error": "",
            }
            LOGGER.info("Syncing push: %s commit(s) to %s", len(commits_payload), branch or "?")

            try:
                changed_files = self._changed_files_from_push(payload)
                if self.settings.GITHUB_PUSH_AGENT_OUTPUT and changed_files:
                    result["archived"] = await self._archive_files(changed_files, head_sha or "head")
                else:
                    result["archived"] = []

                summaries: List[str] = []
                for commit in commits_payload[:5]:
                    summaries.append(commit.get("message", "").splitlines()[0])

                state = await self.state.read_state()
                state["last_commit"] = head_sha
                state["last_commit_branch"] = branch
                if summaries:
                    state["last_commit_message"] = summaries[-1]
                state = await self._refresh_file_index(state, changed_files)
                persisted = await self.state.write_state(state)
                result["state_updated"] = persisted

                await self.state.append_event(
                    {
                        "agent_id": payload.get("pusher", {}).get("name") or "github",
                        "action": "push",
                        "result": f"{len(commits_payload)} commit(s) to {branch}: {head_sha[:7]}",
                        "branch": branch,
                        "sha": head_sha,
                    },
                    persist=False,
                )
                self.last_sync_at = datetime.now(UTC)
            except Exception as exc:  # noqa: BLE001 - webhooks must not explode
                self.last_error = str(exc)
                result["error"] = str(exc)
                LOGGER.error("Push sync failed: %s", exc, exc_info=True)
            return result

    async def on_pr(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle a GitHub ``pull_request`` event.

        Fetches the PR diff, summarises it with the brain and records the
        context (plus optional review) in the state document.

        Args:
            payload: The GitHub webhook payload.

        Returns:
            ``{handled, pr, action, summary, review, state_updated, error}``.
        """
        async with self._lock:
            pr = payload.get("pull_request") or {}
            number = pr.get("number") or payload.get("number")
            action = payload.get("action", "")
            result: Dict[str, Any] = {
                "handled": "pull_request",
                "pr": number,
                "action": action,
                "title": pr.get("title", ""),
                "summary": "",
                "review": {},
                "state_updated": False,
                "error": "",
            }
            if number is None:
                result["error"] = "payload contained no pull request number"
                return result

            try:
                diff = await self.github.get_pr_diff(int(number))
                summary = await self.brain.summarize_diff(diff)
                review = await self.brain.review_pr(diff, pr.get("title", ""))
                result["summary"] = summary
                result["review"] = review

                state = await self.state.read_state()
                prs = [entry for entry in (state.get("open_prs") or []) if isinstance(entry, dict)]
                prs = [entry for entry in prs if entry.get("number") != number]
                if action in {"opened", "reopened", "synchronize"}:
                    prs.append(
                        {
                            "number": number,
                            "title": pr.get("title", ""),
                            "author": (pr.get("user") or {}).get("login", ""),
                            "branch": (pr.get("head") or {}).get("ref", ""),
                            "summary": summary[:1000],
                            "score": review.get("score"),
                        }
                    )
                state["open_prs"] = self._sort_prs(prs)
                if action == "closed" and not pr.get("merged"):
                    state["last_closed_pr"] = number
                state["last_pr_summary"] = summary
                result["state_updated"] = await self.state.write_state(state)

                await self.state.append_event(
                    {
                        "agent_id": (pr.get("user") or {}).get("login") or "github",
                        "action": f"pr_{action}",
                        "result": f"#{number} {pr.get('title', '')} -- {summary[:200]}",
                        "score": review.get("score"),
                    },
                    persist=False,
                )
                self.last_sync_at = datetime.now(UTC)
            except GitHubError as exc:
                self.last_error = str(exc)
                result["error"] = str(exc)
                LOGGER.error("PR sync failed for #%s: %s", number, exc)
            except Exception as exc:  # noqa: BLE001 - webhooks must not explode
                self.last_error = str(exc)
                result["error"] = str(exc)
                LOGGER.error("PR sync failed unexpectedly: %s", exc, exc_info=True)
            return result

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------
    async def cron_sync(self, record_event: bool = True) -> Dict[str, Any]:
        """The periodic pass: pull GitHub, archive, refresh state, notify agents.

        Args:
            record_event: Append an event to the state history.

        Returns:
            ``{started_at, commits, last_commit, archived, state_updated,
            agents_notified, context, errors}``.
        """
        started = datetime.now(UTC)
        self.run_count += 1
        summary: Dict[str, Any] = {
            "started_at": started.isoformat(),
            "commits": 0,
            "last_commit": "",
            "archived": [],
            "state_updated": False,
            "agents_notified": 0,
            "context": "",
            "errors": [],
        }
        LOGGER.info("Cron sync #%s starting", self.run_count)

        # 1. Pull the latest commits from GitHub.
        commits: List[Dict[str, Any]] = []
        if self.github.is_configured():
            try:
                commits = await self.github.get_latest_commits(n=10)
                summary["commits"] = len(commits)
                if commits:
                    summary["last_commit"] = commits[0]["sha"]
            except KollektivError as exc:
                # A rejected/limited token must degrade the pass, not abort it;
                # /health and the summary both report what failed.
                summary["errors"].append(f"github: {exc}")
                LOGGER.error("Cron sync could not read commits: %s", exc)
        else:
            summary["errors"].append("github: not configured")

        # 2. Upload session artifacts (workspace files not yet mirrored).
        try:
            summary["archived"] = await self._archive_workspace()
        except Exception as exc:  # noqa: BLE001 - partial success is acceptable
            summary["errors"].append(f"archive: {exc}")
            LOGGER.error("Cron sync could not archive artifacts: %s", exc)

        # 3. Refresh PROJECT_STATE.md with the new GitHub facts.
        try:
            state = await self.state.read_state(force=True)
            if commits:
                state["recent_commits"] = [
                    {
                        "sha": commit["sha"][:7],
                        "message": commit["message"].splitlines()[0][:200],
                        "author": commit["author"],
                        "timestamp": commit["timestamp"],
                    }
                    for commit in commits[:10]
                ]
                state["last_commit"] = commits[0]["sha"]
            tree: List[Dict[str, Any]] = []
            if self.github.is_configured():
                try:
                    tree = await self.github.get_repo_tree(recursive=False)
                except GitHubError as exc:
                    summary["errors"].append(f"tree: {exc}")
            if tree:
                state["repo_tree_top"] = [entry["path"] for entry in tree][:100]
            summary["state_updated"] = await self.state.write_state(state)
        except Exception as exc:  # noqa: BLE001 - keep syncing
            summary["errors"].append(f"state: {exc}")
            LOGGER.error("Cron sync could not refresh the state document: %s", exc)

        # 4. Notify the agents of the new context.
        try:
            context = await self.state.get_latest_context("orchestrator")
            summary["context"] = context[:2000]
            notified = await self.push_context_to_agents(context)
            summary["agents_notified"] = notified
        except Exception as exc:  # noqa: BLE001 - keep syncing
            summary["errors"].append(f"notify: {exc}")
            LOGGER.error("Cron sync could not notify the agents: %s", exc)

        if record_event:
            try:
                await self.state.append_event(
                    {
                        "agent_id": "sync-engine",
                        "action": "cron_sync",
                        "result": (
                            f"{summary['commits']} commit(s), {len(summary['archived'])} archived, "
                            f"{summary['agents_notified']} agent(s) notified"
                        ),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - logging the event is best effort
                LOGGER.warning("Could not record the sync event: %s", exc)

        self.last_sync_at = datetime.now(UTC)
        summary["duration_seconds"] = round((self.last_sync_at - started).total_seconds(), 2)
        LOGGER.info(
            "Cron sync #%s finished in %ss (%s commit(s), %s archived)",
            self.run_count,
            summary["duration_seconds"],
            summary["commits"],
            len(summary["archived"]),
        )
        return summary

    async def push_context_to_agents(self, context: str, deliver: bool = False) -> int:
        """Share updated context with every worker.

        This is a *context update*, not a task: the context is stored in the
        shared state so the next dispatched prompt carries it. When
        ``deliver`` is true and an agent pool is attached, a short
        notification prompt is also sent to each healthy agent (useful for
        endpoints that keep a conversation).

        Args:
            context: The context block to share.
            deliver: Also send the context to each agent immediately.

        Returns:
            The number of agents notified.
        """
        if not context.strip():
            return 0
        try:
            state = await self.state.read_state()
            updates = [entry for entry in (state.get("context_updates") or []) if isinstance(entry, dict)]
            updates.append(
                {"at": datetime.now(UTC).isoformat(timespec="seconds"), "context": context[:4000]}
            )
            state["context_updates"] = updates[-10:]
            await self.state.write_state(state)
        except Exception as exc:  # noqa: BLE001 - the state write is best effort
            LOGGER.warning("Could not store the context update: %s", exc)

        pool = self._resolve_agent_pool()
        if pool is None or not deliver:
            return 0

        message = (
            "Context update from the orchestrator -- do not treat this as a task and do not "
            "produce code. Acknowledge in one line and wait for your next assignment.\n\n" + context
        )
        notified = 0
        for agent in getattr(pool, "agents", []):
            try:
                if not await agent.is_ready():
                    continue
                await agent.send_prompt(message)
                notified += 1
            except Exception as exc:  # noqa: BLE001 - one bad agent must not stop the rest
                LOGGER.warning("Could not push context to %s: %s", getattr(agent, "label", "?"), exc)
        LOGGER.info("Pushed the context update to %s agent(s)", notified)
        return notified

    def _resolve_agent_pool(self) -> Optional[Any]:
        """Return the agent pool, resolving the orchestrator indirection."""
        if self.agent_pool is not None:
            return self.agent_pool
        orchestrator = getattr(self, "orchestrator", None)
        return getattr(orchestrator, "agent_pool", None) if orchestrator is not None else None

    async def on_pr_merged(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle a merged PR: record the merge in the state document.

        Args:
            payload: The GitHub ``pull_request`` payload (action ``closed``).

        Returns:
            ``{handled, pr, merge_commit, state_updated, error}``.
        """
        pr = payload.get("pull_request") or {}
        number = pr.get("number")
        result: Dict[str, Any] = {
            "handled": "pr_merged",
            "pr": number,
            "merge_commit": pr.get("merge_commit_sha", ""),
            "state_updated": False,
            "error": "",
        }
        try:
            state = await self.state.read_state()
            open_prs = [
                entry for entry in (state.get("open_prs") or []) if entry.get("number") != number
            ]
            state["open_prs"] = open_prs
            merged = [entry for entry in (state.get("merged_prs") or []) if isinstance(entry, dict)]
            merged.append(
                {
                    "number": number,
                    "title": pr.get("title", ""),
                    "author": (pr.get("user") or {}).get("login", ""),
                    "merge_commit_sha": pr.get("merge_commit_sha", ""),
                    "merged_at": pr.get("merged_at", datetime.now(UTC).isoformat()),
                }
            )
            state["merged_prs"] = merged[-25:]
            if pr.get("merge_commit_sha"):
                state["last_commit"] = pr["merge_commit_sha"]
            state["last_merge_summary"] = (
                f"PR #{number} '{pr.get('title', '')}' merged into "
                f"{(pr.get('base') or {}).get('ref', '')}"
            )
            result["state_updated"] = await self.state.write_state(state)
            await self.state.append_event(
                {
                    "agent_id": (pr.get("user") or {}).get("login") or "github",
                    "action": "pr_merged",
                    "result": f"#{number} merged as {str(pr.get('merge_commit_sha', ''))[:7]}",
                },
                persist=False,
            )
        except Exception as exc:  # noqa: BLE001 - webhooks must not explode
            result["error"] = str(exc)
            LOGGER.error("Merge handling failed for PR #%s: %s", number, exc, exc_info=True)
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _changed_files_from_push(payload: Dict[str, Any]) -> List[str]:
        """Collect the file paths touched by a push payload."""
        files: List[str] = []
        for commit in payload.get("commits") or []:
            for key in ("added", "modified"):
                for path in commit.get(key) or []:
                    if path not in files:
                        files.append(path)
        return files[:200]

    async def _archive_files(self, files: Sequence[str], ref: str) -> List[Dict[str, Any]]:
        """Download changed files from GitHub and archive them to TeraBox.

        Only files below :data:`MAX_ARCHIVE_BYTES` are archived; the pass is
        skipped entirely when the storage pool has no healthy accounts.

        Args:
            files: Repository relative paths.
            ref: The commit/branch to read from.

        Returns:
            A list of upload results.
        """
        if not files or not self._pool_ready():
            return []
        archived: List[Dict[str, Any]] = []
        for path in files:
            try:
                content = await self.github.get_file_content(path, ref=ref)
            except GitHubError as exc:
                LOGGER.debug("Skipping %s (unreadable at %s): %s", path, ref[:7], exc)
                continue
            if len(content.encode("utf-8", errors="replace")) > MAX_ARCHIVE_BYTES:
                LOGGER.warning("Skipping %s: larger than the archive limit", path)
                continue
            remote = (
                f"{getattr(self.pool, 'remote_root', self.settings.TERABOX_REMOTE_ROOT).rstrip('/')}/"
                f"{self.state.project_id or 'global'}/github/{ref[:7] or 'head'}/{path}"
            )
            try:
                result = await self.pool.write_text(remote, content)
                archived.append({"path": remote, "source": path, "size": result.get("size", 0)})
            except Exception as exc:  # noqa: BLE001 - archiving is best effort
                LOGGER.warning("Could not archive %s: %s", path, exc)
        LOGGER.info("Archived %s/%s changed file(s) to TeraBox", len(archived), len(files))
        return archived

    async def _archive_workspace(self) -> List[Dict[str, Any]]:
        """Mirror new/changed workspace artifacts to TeraBox.

        Files already recorded in the state document (matched by size) are
        skipped, so repeated cron passes upload only what changed.

        Returns:
            A list of upload results.
        """
        if not self._pool_ready():
            return []
        workspace = Path(self.settings.workspace_path) / (self.state.project_id or "default")
        if not workspace.exists():
            return []
        state = await self.state.read_state()
        known = {
            (entry.get("path"), entry.get("size"))
            for entry in (state.get("files") or [])
            if isinstance(entry, dict)
        }

        uploaded: List[Dict[str, Any]] = []
        for path in sorted(workspace.rglob("*")):
            if not path.is_file():
                continue
            if any(part in {".git", ".venv", "node_modules", "__pycache__"} for part in path.parts):
                continue
            relative = str(path.relative_to(workspace))
            size = path.stat().st_size
            remote = (
                f"{getattr(self.pool, 'remote_root', self.settings.TERABOX_REMOTE_ROOT).rstrip('/')}/"
                f"{self.state.project_id or 'global'}/artifacts/{relative}"
            )
            if (remote, size) in known:
                continue
            try:
                result = await self.pool.upload_file(str(path), remote)
                uploaded.append({"path": result.get("path", remote), "size": size})
            except Exception as exc:  # noqa: BLE001 - keep going
                LOGGER.warning("Could not archive %s: %s", relative, exc)
            if len(uploaded) >= 50:  # bound a single cron pass
                LOGGER.info("Archive limit reached for this pass; the rest goes next time")
                break

        if uploaded:
            state = await self.state.read_state()
            files = [entry for entry in (state.get("files") or []) if isinstance(entry, dict)]
            existing_paths = {entry.get("path") for entry in files}
            for entry in uploaded:
                if entry["path"] not in existing_paths:
                    files.append(entry)
            state["files"] = files[-500:]
            await self.state.write_state(state)
        return uploaded

    async def _refresh_file_index(self, state: Dict[str, Any], changed_files: Sequence[str]) -> Dict[str, Any]:
        """Merge freshly pushed file paths into the state file inventory."""
        if not changed_files:
            return state
        files = [entry for entry in (state.get("files") or []) if isinstance(entry, dict)]
        known = {entry.get("path") for entry in files}
        for path in changed_files:
            if path in known:
                continue
            size = 0
            try:
                meta = await self.github.get_file_metadata(path)
                size = int(meta.get("size") or meta.get("decoded_size") or 0)
            except GitHubError:
                size = 0
            files.append({"path": path, "size": size, "source": "github"})
        state["files"] = files[-500:]
        return state

    def _pool_ready(self) -> bool:
        """Return ``True`` when TeraBox storage can accept writes."""
        return bool(getattr(self.pool, "is_configured", lambda: False)())

    @staticmethod
    def _sort_prs(prs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Sort pull requests by number, descending."""
        return sorted(prs, key=lambda entry: int(entry.get("number") or 0), reverse=True)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        """Return sync engine status for the API/MCP surfaces."""
        return {
            "runs": self.run_count,
            "last_sync_at": self.last_sync_at.isoformat() if self.last_sync_at else None,
            "last_error": self.last_error,
            "cron_interval_minutes": self.settings.CRON_INTERVAL_MINUTES,
            "github_configured": self.github.is_configured(),
            "storage_configured": self._pool_ready(),
        }


__all__ = ["SyncEngine", "MAX_ARCHIVE_BYTES"]
