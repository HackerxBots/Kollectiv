"""Tests for the GitHub sync layer.

Covers the REST client (commits, diffs, file contents, tree, pull requests,
rate limits), webhook signature verification, the webhook endpoints and the
sync engine's push / PR / cron passes. All HTTP is mocked.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Dict, List, cast

import httpx
import pytest
from fastapi import FastAPI

from config.settings import Settings
from src.github.github_client import GitHubClient
from src.github.webhook_handler import router as webhook_router
from src.github.webhook_handler import set_orchestrator, verify_signature
from src.orchestrator.brain import OrchestratorBrain
from src.orchestrator.sync_engine import SyncEngine
from src.storage.state_manager import StateManager
from src.utils.errors import AuthenticationError, GitHubError, RateLimitError
from tests.conftest import FakeTeraBoxPool

# ----------------------------------------------------------------------
# Transport
# ----------------------------------------------------------------------
COMMIT_SHA = "a" * 40


def build_github_transport(calls: List[Dict[str, Any]], secret: str = "") -> httpx.MockTransport:
    """Build a transport that mimics the GitHub REST API."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append({"method": request.method, "path": path, "url": str(request.url)})

        if path == "/repos/octocat/hello-world":
            return httpx.Response(
                200,
                json={
                    "full_name": "octocat/hello-world",
                    "default_branch": "main",
                    "private": False,
                    "permissions": {"push": True, "admin": False},
                },
            )
        if path == "/repos/octocat/hello-world/commits":
            return httpx.Response(
                200,
                json=[
                    {
                        "sha": COMMIT_SHA,
                        "commit": {
                            "message": "feat: add parser\n\nWith detail.",
                            "author": {"name": "Ada", "email": "ada@example.com", "date": "2026-01-01T10:00:00Z"},
                        },
                        "author": {"login": "ada"},
                        "html_url": "https://github.com/octocat/hello-world/commit/aaa",
                    }
                ],
            )
        if path.startswith(f"/repos/octocat/hello-world/commits/{COMMIT_SHA}"):
            accept = request.headers.get("Accept", "")
            if "diff" in accept:
                return httpx.Response(
                    200,
                    text="diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n+print('hi')\n",
                )
            return httpx.Response(
                200,
                json={
                    "sha": COMMIT_SHA,
                    "stats": {"total": 1, "additions": 12, "deletions": 3},
                    "files": [{"filename": "src/app.py", "status": "modified"}],
                },
            )
        if path == "/repos/octocat/hello-world/contents/src/new.py" and request.method == "PUT":
            return httpx.Response(
                200,
                json={
                    "content": {"path": "src/new.py", "sha": "blob-sha", "html_url": "https://github.com/x"},
                    "commit": {"sha": "commit-sha"},
                },
            )
        if path.startswith("/repos/octocat/hello-world/contents/") and request.method == "GET":
            accept = request.headers.get("Accept", "")
            if "raw" in accept:
                return httpx.Response(200, text="print('file body')\n")
            return httpx.Response(
                200,
                json={
                    "name": "app.py",
                    "path": "src/app.py",
                    "size": 19,
                    "sha": "abc",
                    "content": "cHJpbnQoJ2ZpbGUgYm9keScpCg==",
                    "encoding": "base64",
                },
            )
        if path == "/repos/octocat/hello-world/branches/main":
            return httpx.Response(200, json={"commit": {"commit": {"tree": {"sha": "tree-sha"}}}})
        if path == "/repos/octocat/hello-world/git/trees/tree-sha":
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"path": "README.md", "type": "blob", "size": 12, "sha": "1"},
                        {"path": "src", "type": "tree", "sha": "2"},
                        {"path": "src/app.py", "type": "blob", "size": 19, "sha": "3"},
                    ]
                },
            )
        if path == "/repos/octocat/hello-world/pulls" and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 7,
                        "title": "Add parser",
                        "user": {"login": "ada"},
                        "head": {"ref": "feature/parser"},
                        "base": {"ref": "main"},
                        "state": "open",
                        "draft": False,
                        "html_url": "https://github.com/octocat/hello-world/pull/7",
                        "updated_at": "2026-01-02T10:00:00Z",
                    }
                ],
            )
        if path == "/repos/octocat/hello-world/pulls/7":
            accept = request.headers.get("Accept", "")
            if "diff" in accept:
                return httpx.Response(200, text="diff --git a/src/parser.py b/src/parser.py\n+def parse():\n+    pass\n")
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "title": "Add parser",
                    "state": "open",
                    "user": {"login": "ada"},
                    "head": {"ref": "feature/parser"},
                    "base": {"ref": "main"},
                    "merge_commit_sha": "b" * 40,
                    "merged": False,
                },
            )
        if path == "/repos/octocat/hello-world/pulls/7/files":
            return httpx.Response(
                200,
                json=[{"filename": "src/parser.py", "status": "added", "additions": 2, "deletions": 0, "patch": "+..."}],
            )
        if path == "/repos/octocat/hello-world/issues/7/comments":
            return httpx.Response(201, json={"id": 99, "body": json.loads(request.content)["body"]})
        if path == "/repos/octocat/hello-world/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "c" * 40}})
        if path == "/repos/octocat/hello-world/git/refs" and request.method == "POST":
            return httpx.Response(201, json={"ref": json.loads(request.content)["ref"]})
        if path == "/repos/octocat/hello-world/pulls" and request.method == "POST":
            return httpx.Response(
                201, json={"number": 8, "html_url": "https://github.com/octocat/hello-world/pull/8", "title": "Kollektiv"}
            )
        if path == "/rate_limit":
            return httpx.Response(
                200, json={"resources": {"core": {"limit": 5000, "remaining": 4999, "reset": 1_800_000_000}}}
            )
        return httpx.Response(404, json={"message": f"no route for {request.method} {path}"})

    return httpx.MockTransport(handler)


@pytest.fixture()
def github_calls() -> List[Dict[str, Any]]:
    """Records every request the client makes."""
    return []


@pytest.fixture()
def github(settings: Any, github_calls: List[Dict[str, Any]]) -> GitHubClient:
    """A GitHub client wired to the mock transport."""
    client = httpx.AsyncClient(
        base_url=settings.GITHUB_API_URL,
        headers={"Authorization": f"Bearer {settings.GITHUB_TOKEN}"},
        transport=build_github_transport(github_calls),
    )
    return GitHubClient(settings=settings, client=client)


# ----------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------
async def test_get_latest_commits_shape(github: GitHubClient) -> None:
    """Commits come back normalised with author and message metadata."""
    commits = await github.get_latest_commits(5)
    assert len(commits) == 1
    commit = commits[0]
    assert commit["sha"] == COMMIT_SHA
    assert commit["short_sha"] == "aaaaaaa"
    assert commit["author"] == "ada"
    assert commit["timestamp"] == "2026-01-01T10:00:00Z"
    assert commit["message"].startswith("feat: add parser")


async def test_get_latest_commits_with_stats(github: GitHubClient, github_calls: List[Dict[str, Any]]) -> None:
    """``include_stats`` fills the per-commit file statistics."""
    commits = await github.get_latest_commits(1, include_stats=True)
    assert commits[0]["files_changed"] == 1
    assert commits[0]["additions"] == 12
    assert commits[0]["deletions"] == 3
    assert commits[0]["changed_files"] == [{"path": "src/app.py", "status": "modified"}]
    assert any("commits/aaa" in call["path"] for call in github_calls)


async def test_get_commit_diff_uses_diff_media_type(github: GitHubClient) -> None:
    """The diff endpoint returns unified diff text."""
    diff = await github.get_commit_diff(COMMIT_SHA)
    assert diff.startswith("diff --git a/src/app.py")
    assert "print('hi')" in diff


async def test_get_file_content_and_metadata(github: GitHubClient) -> None:
    """Raw content and base64 metadata are both supported."""
    content = await github.get_file_content("src/app.py")
    assert content == "print('file body')\n"
    metadata = await github.get_file_metadata("src/app.py")
    assert metadata["size"] == 19
    assert metadata["decoded_size"] == 19


async def test_get_repo_tree_resolves_branch(github: GitHubClient) -> None:
    """The tree is resolved through the branch's tree SHA."""
    tree = await github.get_repo_tree()
    assert {entry["path"] for entry in tree} == {"README.md", "src", "src/app.py"}
    assert next(entry for entry in tree if entry["path"] == "src")["type"] == "directory"


async def test_pull_request_helpers(github: GitHubClient) -> None:
    """Listing, diffing, reviewing and commenting on PRs all work."""
    prs = await github.list_open_prs()
    assert prs[0]["number"] == 7
    assert prs[0]["branch"] == "feature/parser"
    assert prs[0]["status"] == "open"

    diff = await github.get_pr_diff(7)
    assert "src/parser.py" in diff

    files = await github.list_pr_files(7)
    assert files[0]["filename"] == "src/parser.py"

    assert await github.post_pr_comment(7, "Kollektiv reviewed this") is True


async def test_branch_file_and_pr_mutations(github: GitHubClient) -> None:
    """Branch creation, file writes and PR creation report their results."""
    assert await github.get_branch_sha("main") == "c" * 40
    assert await github.create_branch("kollektiv/agent-1") is True
    written = await github.put_file("src/new.py", "print('new')\n", "feat: add file", "kollektiv/agent-1")
    assert written is not None and written["commit_sha"] == "commit-sha"
    pr = await github.create_pr("Kollektiv output", head="kollektiv/agent-1")
    assert pr is not None and pr["number"] == 8


async def test_check_connection_and_rate_limit(github: GitHubClient) -> None:
    """Connection checks and rate limit lookups are reported."""
    connection = await github.check_connection()
    assert connection["ok"] is True
    assert connection["default_branch"] == "main"
    rates = await github.get_rate_limit()
    assert rates["core"]["remaining"] == 4999


async def test_rate_limit_is_not_retried(settings: Any) -> None:
    """A 403 rate limit raises RateLimitError immediately (no sleeping)."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(1_800_000_000)},
        )

    client = httpx.AsyncClient(
        base_url=settings.GITHUB_API_URL, transport=httpx.MockTransport(handler)
    )
    gh = GitHubClient(settings=settings, client=client)
    with pytest.raises(RateLimitError):
        await gh.get_latest_commits(1)
    assert calls["count"] == 1


async def test_bad_token_and_missing_resource(settings: Any) -> None:
    """401 maps to AuthenticationError, 404 to GitHubError."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(404, json={"message": "Not Found"})

    client = httpx.AsyncClient(base_url=settings.GITHUB_API_URL, transport=httpx.MockTransport(handler))
    gh = GitHubClient(settings=settings, client=client)
    with pytest.raises(AuthenticationError):
        await gh.get_latest_commits(1)
    with pytest.raises(GitHubError):
        await gh.get_file_content("nope.py")


async def test_unconfigured_client_reports_clearly(settings: Any) -> None:
    """A missing token/repo is reported instead of crashing."""
    bare = settings.model_copy(update={"GITHUB_TOKEN": "", "GITHUB_REPO": "not-a-repo"}, deep=True)
    gh = GitHubClient(settings=bare)
    assert gh.is_configured() is False
    await gh.close()


# ----------------------------------------------------------------------
# Webhook signature
# ----------------------------------------------------------------------
def test_verify_signature_roundtrip() -> None:
    """A correct HMAC signature verifies; tampering fails."""
    body = b'{"zen": "keep it simple"}'
    secret = "s3cret"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(body, signature, secret) is True
    assert verify_signature(body + b"x", signature, secret) is False
    assert verify_signature(body, "sha256=deadbeef", secret) is False
    assert verify_signature(body, None, secret) is False
    assert verify_signature(body, None, "") is True  # no secret configured


# ----------------------------------------------------------------------
# Webhook endpoints
# ----------------------------------------------------------------------
class StubOrchestrator:
    """Minimal orchestrator used by the webhook tests."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = StateManager(FakeTeraBoxPool(), settings=settings, project_id="prj_hook")
        self.events: List[Dict[str, Any]] = []
        self.merged: List[Dict[str, Any]] = []
        self.sync_engine = self

    async def on_push(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Record a push."""
        self.events.append({"kind": "push", "payload": payload})
        return {"handled": "push"}

    async def on_pr(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Record a pull request."""
        self.events.append({"kind": "pr", "payload": payload})
        return {"handled": "pr"}

    async def on_pr_merged(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Record a merge."""
        self.merged.append(payload)
        return {"handled": "pr_merged"}


@pytest.fixture()
def webhook_app(settings: Any) -> FastAPI:
    """A FastAPI app with only the webhook router attached."""
    app = FastAPI()
    app.include_router(webhook_router)
    app.state.orchestrator = StubOrchestrator(settings)
    set_orchestrator(app.state.orchestrator)
    return app


async def test_webhook_ping(webhook_app: FastAPI, settings: Any) -> None:
    """A signed ping payload is acknowledged."""
    body = json.dumps({"zen": "Non-blocking is better than blocking."}).encode()
    signature = "sha256=" + hmac.new(settings.GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    transport = httpx.ASGITransport(app=webhook_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": signature, "Content-Type": "application/json"},
        )
    assert response.status_code == 200
    assert response.json()["handled"] == "ping"


async def test_webhook_rejects_bad_signature(webhook_app: FastAPI, settings: Any) -> None:
    """An invalid signature is rejected with 401 before any work happens."""
    transport = httpx.ASGITransport(app=webhook_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhooks/github",
            content=b'{"ref": "refs/heads/main"}',
            headers={
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": "sha256=not-the-right-signature",
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 401
    assert webhook_app.state.orchestrator.events == []


async def test_webhook_push_triggers_sync(webhook_app: FastAPI, settings: Any) -> None:
    """A signed push is accepted and queued to the sync engine."""
    body = json.dumps({"ref": "refs/heads/main", "commits": [{"id": "1" * 40}], "repository": {"full_name": "o/r"}}).encode()
    signature = "sha256=" + hmac.new(settings.GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    transport = httpx.ASGITransport(app=webhook_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": signature,
                "X-GitHub-Delivery": "delivery-1",
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 202
    payload = response.json()
    assert payload["verified"] is True
    assert payload["handled"] == "sync_engine.on_push"
    # The background task ran before the response was consumed.
    assert any(event["kind"] == "push" for event in webhook_app.state.orchestrator.events)


async def test_webhook_merged_pr_calls_merge_hook(webhook_app: FastAPI, settings: Any) -> None:
    """A merged PR triggers both the PR sync and the merge hook."""
    body = json.dumps(
        {"action": "closed", "number": 3, "pull_request": {"number": 3, "merged": True, "title": "Merge me"}}
    ).encode()
    signature = "sha256=" + hmac.new(settings.GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    transport = httpx.ASGITransport(app=webhook_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": signature,
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 202
    assert "orchestrator.on_pr_merged" in response.json()["handled"]
    assert webhook_app.state.orchestrator.merged


async def test_webhook_health(webhook_app: FastAPI) -> None:
    """The health route reports orchestrator attachment and signature state."""
    transport = httpx.ASGITransport(app=webhook_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/webhooks/github/health")
    assert response.status_code == 200
    body = response.json()
    assert body["orchestrator_attached"] is True
    assert body["signature_checking"] is True


# ----------------------------------------------------------------------
# Sync engine
# ----------------------------------------------------------------------
def build_sync_engine(settings: Settings, calls: List[Dict[str, Any]]) -> SyncEngine:
    """Build a sync engine with fake storage and a real (mocked) GitHub client."""
    pool = FakeTeraBoxPool()
    state = StateManager(pool, settings=settings, project_id="prj_sync")
    client = httpx.AsyncClient(
        base_url=settings.GITHUB_API_URL, transport=build_github_transport(calls)
    )
    github = GitHubClient(settings=settings, client=client)
    brain = OrchestratorBrain(settings)
    # ``FakeTeraBoxPool`` is a duck-typed double for the storage pool.
    return SyncEngine(github=github, pool=cast(Any, pool), state=state, brain=brain, settings=settings)


async def test_on_push_updates_state(settings: Any, github_calls: List[Dict[str, Any]]) -> None:
    """A push event updates the commit pointer, files and history."""
    engine = build_sync_engine(settings, github_calls)
    payload = {
        "ref": "refs/heads/main",
        "after": COMMIT_SHA,
        "pusher": {"name": "ada"},
        "commits": [
            {
                "id": COMMIT_SHA,
                "message": "feat: parser",
                "added": ["src/parser.py"],
                "modified": ["README.md"],
            }
        ],
    }
    result = await engine.on_push(payload)
    assert result["handled"] == "push"
    assert result["commits"] == 1
    assert result["state_updated"] is True

    state = await engine.state.read_state()
    assert state["last_commit"] == COMMIT_SHA
    assert {entry["path"] for entry in state["files"]} >= {"src/parser.py", "README.md"}
    assert any(event["action"] == "push" for event in state["history"])


async def test_on_pr_summarises_and_records(settings: Any, github_calls: List[Dict[str, Any]]) -> None:
    """A PR event stores the summary and review score."""
    engine = build_sync_engine(settings, github_calls)
    result = await engine.on_pr(
        {"action": "opened", "number": 7, "pull_request": {"number": 7, "title": "Add parser", "user": {"login": "ada"},
                                                          "head": {"ref": "feature/parser"}}}
    )
    assert result["handled"] == "pull_request"
    assert result["state_updated"] is True
    assert result["summary"]

    state = await engine.state.read_state()
    assert state["open_prs"][0]["number"] == 7
    assert any(event["action"] == "pr_opened" for event in state["history"])


async def test_pr_merged_updates_state(settings: Any, github_calls: List[Dict[str, Any]]) -> None:
    """``on_pr_merged`` moves the PR to the merged list and records the commit."""
    engine = build_sync_engine(settings, github_calls)
    await engine.on_pr({"action": "opened", "number": 7, "pull_request": {"number": 7, "title": "x", "head": {}}})
    merged = await engine.on_pr_merged(
        {"number": 7, "pull_request": {"number": 7, "title": "Add parser", "merged": True,
                                       "merge_commit_sha": "d" * 40, "user": {"login": "ada"}}}
    )
    assert merged["handled"] == "pr_merged"
    state = await engine.state.read_state()
    assert state["open_prs"] == []
    assert state["merged_prs"][0]["number"] == 7
    assert state["last_commit"] == "d" * 40


async def test_cron_sync_runs_end_to_end(settings: Any, github_calls: List[Dict[str, Any]]) -> None:
    """The periodic pass pulls commits, refreshes the document and records an event."""
    engine = build_sync_engine(settings, github_calls)
    summary = await engine.cron_sync()
    assert summary["commits"] == 1
    assert summary["last_commit"] == COMMIT_SHA
    assert summary["state_updated"] is True
    assert engine.status()["runs"] == 1

    state = await engine.state.read_state()
    assert state["recent_commits"][0]["sha"] == COMMIT_SHA[:7]
    assert any(event["action"] == "cron_sync" for event in state["history"])


async def test_cron_sync_survives_github_failure(settings: Any) -> None:
    """A GitHub outage degrades the sync instead of raising."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "server error"})

    pool = FakeTeraBoxPool()
    state = StateManager(pool, settings=settings, project_id="prj_fail")
    client = httpx.AsyncClient(base_url=settings.GITHUB_API_URL, transport=httpx.MockTransport(handler))
    github = GitHubClient(settings=settings, client=client)
    engine = SyncEngine(
        github=github,
        pool=cast(Any, pool),
        state=state,
        brain=OrchestratorBrain(settings),
        settings=settings,
    )

    summary = await engine.cron_sync()
    assert summary["commits"] == 0
    assert summary["errors"]
