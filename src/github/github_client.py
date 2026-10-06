"""Async GitHub REST client used as Kollektiv's real-time sync layer.

Everything the orchestrator needs to stay aware of the codebase lives here:
commits and their diffs, file contents, the repository tree, pull requests and
comments. All calls are retried with exponential backoff and every failure is
mapped to a :class:`~src.utils.errors.GitHubError`.

Authentication uses a fine-grained personal access token (``GITHUB_TOKEN``)
sent as ``Authorization: Bearer``.

Usage::

    github = GitHubClient(token, "owner/repo")
    commits = await github.get_latest_commits(5)
    await github.post_pr_comment(12, "Kollektiv reviewed this diff")
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from config.settings import Settings, get_settings
from src.utils.errors import AuthenticationError, GitHubError, GitHubTransientError, RateLimitError
from src.utils.logger import get_logger
from src.utils.net import async_client_kwargs
from src.utils.retry import async_retry

LOGGER = get_logger(__name__)

#: GitHub's API version header, pinned for reproducible behaviour.
GITHUB_API_VERSION = "2022-11-28"


class GitHubClient:
    """Thin async wrapper around the GitHub REST API.

    Args:
        token: Personal access token (``GITHUB_TOKEN``).
        repo: Repository in ``owner/name`` form (``GITHUB_REPO``).
        settings: Optional settings override.
        client: Pre-built :class:`httpx.AsyncClient` (used in tests).
    """

    def __init__(
        self,
        token: str = "",
        repo: str = "",
        settings: Optional[Settings] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.token = token or self.settings.GITHUB_TOKEN
        self.repo = repo or self.settings.GITHUB_REPO
        if "/" not in (self.repo or ""):
            LOGGER.warning("GITHUB_REPO %r is not in owner/repo form; GitHub calls will fail", self.repo)
        self.owner, _, self.repo_name = (self.repo or "/").partition("/")

        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            **async_client_kwargs(
                self.settings,
                base_url=self.settings.GITHUB_API_URL.rstrip("/"),
                timeout=httpx.Timeout(self.settings.GITHUB_REQUEST_TIMEOUT, connect=10.0),
                headers=self._default_headers(),
            )
        )

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------
    def _default_headers(self) -> Dict[str, str]:
        """Return the default request headers."""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "Kollektiv/0.1 (+https://github.com/HackerxBots/Kollektiv)",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @property
    def client(self) -> httpx.AsyncClient:
        """The underlying HTTP client."""
        return self._client

    def is_configured(self) -> bool:
        """Return ``True`` when a token and a valid ``owner/repo`` are present."""
        return bool(self.token and self.owner and self.repo_name)

    async def close(self) -> None:
        """Close the HTTP client (only when this instance created it)."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "GitHubClient":
        """Return the client for ``async with`` usage."""
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        """Close the HTTP client."""
        await self.close()

    def _repo_path(self, suffix: str = "") -> str:
        """Build a repository scoped API path."""
        return f"/repos/{self.owner}/{self.repo_name}{suffix}"

    @async_retry(max_retries=3, base_delay=1.0, max_delay=30.0, exclude=(RateLimitError,))
    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "request",
        absolute: bool = False,
        accept: Optional[str] = None,
        follow_redirects: Optional[bool] = None,
    ) -> httpx.Response:
        """Perform an API request, mapping failures to typed exceptions.

        Args:
            method: HTTP verb.
            url: Path relative to the API base URL (or absolute when
                ``absolute`` is true).
            params: Query parameters.
            json_body: JSON request body.
            context: Description used in logs and errors.
            absolute: Treat ``url`` as a full URL (used for ``download_url``).
            accept: Override the ``Accept`` header (e.g. raw diffs).
            follow_redirects: Override redirect behaviour.

        Returns:
            The raw :class:`httpx.Response` (2xx only).

        Raises:
            RateLimitError: On HTTP 403/429 rate limits.
            AuthenticationError: On HTTP 401.
            GitHubError: For any other non-2xx response.
        """
        headers: Dict[str, str] = {}
        if accept:
            headers["Accept"] = accept
        if not self.token:
            LOGGER.warning("No GITHUB_TOKEN configured; calls will be unauthenticated and heavily rate limited")
            headers.pop("Authorization", None)

        try:
            response = await self._client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers or None,
                follow_redirects=follow_redirects if follow_redirects is not None else True,
            )
        except httpx.HTTPError as exc:
            raise GitHubTransientError(f"{context} failed: {exc}") from exc

        if response.status_code in (403, 429) and _is_rate_limited(response):
            reset = response.headers.get("X-RateLimit-Reset")
            raise RateLimitError(
                f"GitHub rate limit hit during {context}",
                retry_after=_seconds_until(reset),
                remaining=response.headers.get("X-RateLimit-Remaining"),
            )
        if response.status_code == 401:
            raise AuthenticationError(f"GitHub rejected the token during {context}")
        if response.status_code == 404:
            raise GitHubError(f"{context} returned 404 (repo {self.repo} or resource missing)", status=404)
        if response.status_code >= 500:
            raise GitHubTransientError(
                f"{context} failed with HTTP {response.status_code}: {response.text[:300]}",
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise GitHubError(
                f"{context} failed with HTTP {response.status_code}: {response.text[:300]}",
                status=response.status_code,
            )
        return response

    # ------------------------------------------------------------------
    # Commits
    # ------------------------------------------------------------------
    async def get_latest_commits(
        self, n: int = 10, branch: Optional[str] = None, include_stats: bool = False
    ) -> List[Dict[str, Any]]:
        """Return the most recent commits.

        The list endpoint does not include per-file statistics, so
        ``files_changed``/``additions``/``deletions`` are ``0`` unless
        ``include_stats`` is set -- that costs one extra API call per commit.

        Args:
            n: Number of commits to fetch (1-100).
            branch: Branch or SHA to start from; defaults to the repo default.
            include_stats: Fetch each commit's file list to fill the stats.

        Returns:
            ``[{sha, short_sha, message, author, timestamp, files_changed,
            additions, deletions, url}]``.
        """
        params: Dict[str, Any] = {"per_page": max(1, min(n, 100))}
        if branch:
            params["sha"] = branch
        response = await self._request(
            "GET", self._repo_path("/commits"), params=params, context="get_latest_commits"
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise GitHubError(f"Unexpected commits payload: {str(payload)[:200]}")

        commits: List[Dict[str, Any]] = []
        for item in payload:
            commit = item.get("commit") or {}
            author = item.get("author") or {}
            commit_author = commit.get("author") or {}
            sha = item.get("sha", "")
            stats = item.get("stats") or {}
            files_changed = item.get("files") or []
            commits.append(
                {
                    "sha": sha,
                    "short_sha": sha[:7],
                    "message": (commit.get("message") or "").strip(),
                    "author": author.get("login") or commit_author.get("name") or "unknown",
                    "author_email": commit_author.get("email", ""),
                    "timestamp": commit_author.get("date") or "",
                    "files_changed": len(files_changed) if files_changed else stats.get("total", 0),
                    "additions": stats.get("additions", 0),
                    "deletions": stats.get("deletions", 0),
                    "url": item.get("html_url", ""),
                }
            )
        if include_stats:
            await self._enrich_commit_stats(commits)
        LOGGER.debug("Fetched %s commits from %s", len(commits), self.repo)
        return commits

    async def _enrich_commit_stats(self, commits: List[Dict[str, Any]]) -> None:
        """Fill ``files_changed``/``additions``/``deletions`` on each commit.

        One extra API call per commit; failures leave the zeroed values.
        """
        import asyncio

        async def enrich(commit: Dict[str, Any]) -> None:
            """Fetch one commit's file list and stats."""
            try:
                details = await self.get_commit_details(commit["sha"])
            except GitHubError as exc:
                LOGGER.debug("Could not enrich %s: %s", commit["sha"][:7], exc)
                return
            stats = details.get("stats") or {}
            files = details.get("files") or []
            commit["files_changed"] = len(files) if files else stats.get("total", 0)
            commit["additions"] = stats.get("additions", commit.get("additions", 0))
            commit["deletions"] = stats.get("deletions", commit.get("deletions", 0))
            commit["changed_files"] = [
                {"path": entry.get("filename", ""), "status": entry.get("status", "")} for entry in files[:50]
            ]

        await asyncio.gather(*(enrich(commit) for commit in commits))

    async def get_commit_diff(self, sha: str, max_chars: int = 200_000) -> str:
        """Return the unified diff of a commit.

        Args:
            sha: Commit SHA.
            max_chars: Truncate very large diffs (LLM context safety).

        Returns:
            The diff text, or ``""`` when it is empty/too large.
        """
        response = await self._request(
            "GET",
            self._repo_path(f"/commits/{quote(sha)}"),
            accept="application/vnd.github.diff",
            context=f"get_commit_diff({sha[:7]})",
        )
        text = response.text or ""
        if len(text) > max_chars:
            LOGGER.warning("Diff for %s is %s chars; truncating", sha[:7], len(text))
            return text[:max_chars] + "\n... [truncated by Kollektiv]"
        return text

    async def get_commit_details(self, sha: str) -> Dict[str, Any]:
        """Return the full commit object (files, stats, parents)."""
        response = await self._request(
            "GET", self._repo_path(f"/commits/{quote(sha)}"), context=f"get_commit_details({sha[:7]})"
        )
        return response.json()

    # ------------------------------------------------------------------
    # Files and tree
    # ------------------------------------------------------------------
    async def get_file_content(self, path: str, ref: Optional[str] = None) -> str:
        """Return the raw content of a file.

        Args:
            path: Repository relative path.
            ref: Branch, tag or SHA; defaults to the default branch.

        Returns:
            The file content as text.

        Raises:
            GitHubError: When the file does not exist or is a directory.
        """
        params: Dict[str, Any] = {}
        if ref:
            params["ref"] = ref
        response = await self._request(
            "GET",
            self._repo_path(f"/contents/{quote(path)}"),
            params=params,
            accept="application/vnd.github.raw",
            context=f"get_file_content({path})",
        )
        return response.text

    async def get_file_metadata(self, path: str, ref: Optional[str] = None) -> Dict[str, Any]:
        """Return size/sha/encoding metadata for a file."""
        params: Dict[str, Any] = {}
        if ref:
            params["ref"] = ref
        response = await self._request(
            "GET", self._repo_path(f"/contents/{quote(path)}"), params=params, context=f"get_file_metadata({path})"
        )
        data = response.json()
        if isinstance(data, dict) and data.get("content"):
            data["decoded_size"] = len(base64.b64decode(data["content"]))
        return data

    async def get_repo_tree(self, branch: Optional[str] = None, recursive: bool = True) -> List[Dict[str, Any]]:
        """Return the repository file tree.

        Args:
            branch: Branch, tag or SHA; defaults to the repo default branch.
            recursive: Fetch the full tree rather than the top level.

        Returns:
            ``[{path, type, size, sha}]``.
        """
        ref = branch or self.settings.GITHUB_DEFAULT_BRANCH or "main"
        # Resolve the branch to a tree SHA first.
        try:
            branch_info = await self._request(
                "GET", self._repo_path(f"/branches/{quote(ref)}"), context=f"get_branch({ref})"
            )
            tree_sha = ((branch_info.json().get("commit") or {}).get("commit") or {}).get("tree", {}).get("sha", "")
        except GitHubError:
            tree_sha = ref
        if not tree_sha:
            tree_sha = ref

        params: Dict[str, Any] = {"recursive": "1"} if recursive else {}
        response = await self._request(
            "GET",
            self._repo_path(f"/git/trees/{quote(tree_sha, safe='')}"),
            params=params,
            context="get_repo_tree",
        )
        payload = response.json()
        entries = payload.get("tree") or []
        return [
            {
                "path": item.get("path", ""),
                "type": "directory" if item.get("type") == "tree" else "file",
                "size": item.get("size", 0),
                "sha": item.get("sha", ""),
            }
            for item in entries
        ]

    # ------------------------------------------------------------------
    # Pull requests
    # ------------------------------------------------------------------
    async def list_open_prs(self) -> List[Dict[str, Any]]:
        """Return open pull requests.

        Returns:
            ``[{number, title, author, branch, base, status, url, draft,
            updated_at}]``.
        """
        response = await self._request(
            "GET",
            self._repo_path("/pulls"),
            params={"state": "open", "per_page": 100, "sort": "updated", "direction": "desc"},
            context="list_open_prs",
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise GitHubError(f"Unexpected PR payload: {str(payload)[:200]}")
        return [
            {
                "number": pr.get("number"),
                "title": pr.get("title", ""),
                "author": (pr.get("user") or {}).get("login", "unknown"),
                "branch": (pr.get("head") or {}).get("ref", ""),
                "base": (pr.get("base") or {}).get("ref", ""),
                "status": "draft" if pr.get("draft") else pr.get("state", "open"),
                "url": pr.get("html_url", ""),
                "updated_at": pr.get("updated_at", ""),
                "mergeable_state": pr.get("mergeable_state", ""),
            }
            for pr in payload
        ]

    async def get_pr(self, pr_number: int) -> Dict[str, Any]:
        """Return the full pull request object."""
        response = await self._request(
            "GET", self._repo_path(f"/pulls/{pr_number}"), context=f"get_pr({pr_number})"
        )
        return response.json()

    async def get_pr_diff(self, pr_number: int, max_chars: int = 200_000) -> str:
        """Return the unified diff of a pull request."""
        response = await self._request(
            "GET",
            self._repo_path(f"/pulls/{pr_number}"),
            accept="application/vnd.github.diff",
            context=f"get_pr_diff({pr_number})",
        )
        text = response.text or ""
        if len(text) > max_chars:
            return text[:max_chars] + "\n... [truncated by Kollektiv]"
        return text

    async def list_pr_files(self, pr_number: int) -> List[Dict[str, Any]]:
        """Return the files changed by a pull request."""
        response = await self._request(
            "GET",
            self._repo_path(f"/pulls/{pr_number}/files"),
            params={"per_page": 100},
            context=f"list_pr_files({pr_number})",
        )
        return [
            {
                "filename": item.get("filename", ""),
                "status": item.get("status", ""),
                "additions": item.get("additions", 0),
                "deletions": item.get("deletions", 0),
                "patch": item.get("patch", ""),
            }
            for item in response.json()
        ]

    async def post_pr_comment(self, pr_number: int, comment: str) -> bool:
        """Post an issue-style comment on a pull request.

        Args:
            pr_number: Pull request number.
            comment: Markdown body.

        Returns:
            ``True`` when GitHub accepted the comment.
        """
        try:
            response = await self._request(
                "POST",
                self._repo_path(f"/issues/{pr_number}/comments"),
                json_body={"body": comment},
                context=f"post_pr_comment({pr_number})",
            )
        except (GitHubError, AuthenticationError, RateLimitError) as exc:
            LOGGER.error("Could not comment on PR #%s: %s", pr_number, exc)
            return False
        posted = response.status_code in (200, 201)
        if posted:
            LOGGER.info("Commented on PR #%s", pr_number)
        return posted

    async def list_issues(self, labels: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return open issues (optionally filtered by a comma separated label list)."""
        params: Dict[str, Any] = {"state": "open", "per_page": 100}
        if labels:
            params["labels"] = labels
        response = await self._request("GET", self._repo_path("/issues"), params=params, context="list_issues")
        return [
            {
                "number": item.get("number"),
                "title": item.get("title", ""),
                "labels": [label.get("name") for label in item.get("labels", [])],
                "author": (item.get("user") or {}).get("login", "unknown"),
                "is_pr": "pull_request" in item,
                "created_at": item.get("created_at", ""),
            }
            for item in response.json()
        ]

    # ------------------------------------------------------------------
    # Branch / commit helpers
    # ------------------------------------------------------------------
    async def get_branch_sha(self, branch: str) -> str:
        """Return the head SHA of ``branch`` (``""`` when it does not exist)."""
        try:
            response = await self._request(
                "GET", self._repo_path(f"/git/ref/heads/{quote(branch)}"), context=f"get_branch_sha({branch})"
            )
        except GitHubError:
            return ""
        return ((response.json().get("object") or {}).get("sha")) or ""

    async def create_branch(self, branch: str, from_branch: Optional[str] = None) -> bool:
        """Create ``branch`` from ``from_branch`` (default branch by default)."""
        base = from_branch or self.settings.GITHUB_DEFAULT_BRANCH
        base_sha = await self.get_branch_sha(base)
        if not base_sha:
            LOGGER.error("Cannot create branch %s: base %s not found", branch, base)
            return False
        try:
            await self._request(
                "POST",
                self._repo_path("/git/refs"),
                json_body={"ref": f"refs/heads/{branch}", "sha": base_sha},
                context=f"create_branch({branch})",
            )
            return True
        except GitHubError as exc:
            LOGGER.error("Could not create branch %s: %s", branch, exc)
            return False

    async def put_file(
        self,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create or update a file on a branch.

        Args:
            path: Repository relative path.
            content: UTF-8 file content.
            message: Commit message.
            branch: Target branch.
            sha: Blob SHA when updating an existing file.

        Returns:
            ``{path, sha, commit_sha, url}`` or ``None`` on failure.
        """
        body: Dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        try:
            response = await self._request(
                "PUT", self._repo_path(f"/contents/{quote(path)}"), json_body=body, context=f"put_file({path})"
            )
        except (GitHubError, AuthenticationError, RateLimitError) as exc:
            LOGGER.error("Could not write %s: %s", path, exc)
            return None
        raw = response.json()
        data: Dict[str, Any] = raw if isinstance(raw, dict) else {}
        content_raw = data.get("content")
        content_block: Dict[str, Any] = content_raw if isinstance(content_raw, dict) else {}
        commit_raw = data.get("commit")
        commit_block: Dict[str, Any] = commit_raw if isinstance(commit_raw, dict) else {}
        return {
            "path": content_block.get("path", path),
            "sha": content_block.get("sha", ""),
            "commit_sha": commit_block.get("sha", ""),
            "url": content_block.get("html_url", ""),
        }

    async def create_pr(
        self,
        title: str,
        head: str,
        base: Optional[str] = None,
        body: str = "",
        draft: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Open a pull request.

        Returns:
            ``{number, url, title, branch}`` or ``None`` on failure.
        """
        payload = {
            "title": title,
            "head": head,
            "base": base or self.settings.GITHUB_DEFAULT_BRANCH,
            "body": body,
            "draft": draft,
        }
        try:
            response = await self._request(
                "POST", self._repo_path("/pulls"), json_body=payload, context="create_pr"
            )
        except (GitHubError, AuthenticationError, RateLimitError) as exc:
            LOGGER.error("Could not create PR from %s: %s", head, exc)
            return None
        data = response.json()
        return {
            "number": data.get("number"),
            "url": data.get("html_url", ""),
            "title": data.get("title", title),
            "branch": head,
        }

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    async def check_connection(self) -> Dict[str, Any]:
        """Verify token/repo access.

        Returns:
            ``{ok, repo, default_branch, permissions, error}``.
        """
        try:
            response = await self._request("GET", self._repo_path(), context="check_connection")
        except (GitHubError, AuthenticationError, RateLimitError) as exc:
            return {"ok": False, "repo": self.repo, "error": str(exc)}
        data = response.json()
        return {
            "ok": True,
            "repo": data.get("full_name", self.repo),
            "default_branch": data.get("default_branch", ""),
            "private": data.get("private", False),
            "permissions": data.get("permissions", {}),
            "error": "",
        }

    async def get_rate_limit(self) -> Dict[str, Any]:
        """Return the current GitHub API rate limit status."""
        try:
            response = await self._request("GET", "/rate_limit", context="get_rate_limit")
        except (GitHubError, AuthenticationError, RateLimitError) as exc:
            return {"error": str(exc)}
        return (response.json() or {}).get("resources", {})


def _is_rate_limited(response: httpx.Response) -> bool:
    """Return ``True`` when a 403/429 response is actually a rate limit."""
    if response.status_code == 429:
        return True
    if response.headers.get("X-RateLimit-Remaining") == "0":
        return True
    try:
        message = str((response.json() or {}).get("message", "")).lower()
    except ValueError:
        message = response.text.lower()
    return "rate limit" in message or "abuse" in message


def _seconds_until(reset_header: Optional[str]) -> Optional[float]:
    """Convert an epoch ``X-RateLimit-Reset`` header into seconds from now."""
    if not reset_header:
        return None
    try:
        remaining = float(reset_header) - _now()
        return max(remaining, 1.0)
    except (TypeError, ValueError):
        return None


def _now() -> float:
    """Return the current epoch seconds (isolated for testability)."""
    import time

    return time.time()


__all__ = ["GitHubClient", "GITHUB_API_VERSION"]
