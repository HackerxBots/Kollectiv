"""GitHub connector: the repository the agents work on, as callable actions.

Thin adapter over :class:`~src.github.github_client.GitHubClient` so the brain,
the HTTP API and MCP clients can read commits, PRs, issues and file trees
through the same interface as every other service.
"""

from __future__ import annotations

from typing import Any, List, Optional

from config.settings import Settings
from src.connectors.base import Connector, ConnectorAction, Payload
from src.github.github_client import GitHubClient
from src.utils.errors import ConnectorError

GITHUB_ACTIONS: List[ConnectorAction] = [
    ConnectorAction(
        name="recent_commits",
        description="List the most recent commits on the default branch.",
        params={"limit": "How many commits to return (1-100, default 20)."},
    ),
    ConnectorAction(
        name="commit_diff",
        description="Return the unified diff of one commit.",
        params={"sha": "Commit SHA (full or short)."},
    ),
    ConnectorAction(
        name="open_pull_requests",
        description="List the open pull requests.",
        params={},
    ),
    ConnectorAction(
        name="pull_request_diff",
        description="Return the unified diff of a pull request.",
        params={"number": "Pull request number."},
    ),
    ConnectorAction(
        name="file",
        description="Read a file from the repository at an optional ref.",
        params={"path": "Repository-relative path.", "ref": "Branch/tag/SHA (optional)."},
    ),
    ConnectorAction(
        name="repo_tree",
        description="List the repository tree (optionally recursive).",
        params={"branch": "Branch to read (optional)."},
    ),
    ConnectorAction(
        name="open_issues",
        description="List open issues, optionally filtered by labels.",
        params={"labels": "Comma separated labels (optional)."},
    ),
    ConnectorAction(
        name="comment_on_pull_request",
        description="Post a comment on a pull request.",
        params={"number": "Pull request number.", "comment": "Markdown body."},
        dangerous=True,
    ),
]


class GitHubConnector(Connector):
    """Repository actions backed by the GitHub REST API."""

    name = "github"
    category = "code"
    description = "Commits, pull requests, issues and files of the project repository."
    required_env = ("GITHUB_TOKEN", "GITHUB_REPO")

    def __init__(
        self,
        settings: Optional[Settings] = None,
        token_store: Any = None,
        client: Any = None,
        github: Optional[GitHubClient] = None,
    ) -> None:
        super().__init__(settings, token_store=token_store, client=client)
        self._github = github or GitHubClient(settings=self.settings)

    @property
    def is_configured(self) -> bool:
        """True when a token and a real ``owner/repo`` are configured."""
        return bool(self._github.is_configured())

    #: Read the smallest thing GitHub can answer cheaply.
    probe_action = "recent_commits"
    probe_params = {"limit": 1}

    def actions(self) -> List[ConnectorAction]:
        """Return the repository actions."""
        return GITHUB_ACTIONS

    async def call(self, action: str, params: Payload) -> Any:
        """Perform a repository action."""
        if action == "recent_commits":
            limit = int(params.get("limit") or 20)
            commits = await self._github.get_latest_commits(n=max(1, min(limit, 100)))
            return [
                {
                    "sha": str(commit.get("sha", ""))[:12],
                    "message": (str(commit.get("message") or "").strip().splitlines() or [""])[0],
                    "author": commit.get("author", ""),
                    "date": commit.get("timestamp", ""),
                    "url": commit.get("url", ""),
                }
                for commit in commits
            ]
        if action == "commit_diff":
            return {"sha": params.get("sha", ""), "diff": await self._github.get_commit_diff(str(params["sha"]))}
        if action == "open_pull_requests":
            return [
                {
                    "number": pr.get("number"),
                    "title": pr.get("title"),
                    "author": pr.get("author"),
                    "branch": pr.get("branch"),
                    "base": pr.get("base"),
                    "status": pr.get("status"),
                    "url": pr.get("url"),
                    "updated_at": pr.get("updated_at"),
                }
                for pr in await self._github.list_open_prs()
            ]
        if action == "pull_request_diff":
            return {"number": params.get("number"), "diff": await self._github.get_pr_diff(int(params["number"]))}
        if action == "file":
            return {
                "path": params.get("path", ""),
                "content": await self._github.get_file_content(str(params["path"]), params.get("ref")),
            }
        if action == "repo_tree":
            tree = await self._github.get_repo_tree(branch=params.get("branch"))
            return [
                {"path": entry.get("path"), "type": entry.get("type"), "size": entry.get("size")}
                for entry in tree
            ]
        if action == "open_issues":
            return [
                {
                    "number": issue.get("number"),
                    "title": issue.get("title"),
                    "labels": issue.get("labels"),
                    "url": issue.get("url"),
                    "updated_at": issue.get("updated_at"),
                }
                for issue in await self._github.list_issues(labels=params.get("labels"))
            ]
        if action == "comment_on_pull_request":
            posted = await self._github.post_pr_comment(int(params["number"]), str(params["comment"]))
            if not posted:
                raise ConnectorError(f"GitHub refused the comment on PR #{params.get('number')}")
            return {"posted": True, "number": params.get("number")}
        raise ConnectorError(f"Unhandled GitHub action {action!r}")

    async def close(self) -> None:
        """Close the GitHub client (and the injected client, if owned)."""
        await self._github.close()
        await super().close()


__all__ = ["GITHUB_ACTIONS", "GitHubConnector"]
