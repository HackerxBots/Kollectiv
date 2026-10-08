"""Shared pytest fixtures for the Kollektiv test suite.

Every fixture here is hermetic: no network access, no real credentials, an
in-memory SQLite database and a temporary workspace directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from config.settings import Settings
from src.db import models as db_models


# ----------------------------------------------------------------------
# Global test tuning
# ----------------------------------------------------------------------
@pytest.fixture(autouse=True, scope="session")
def fast_retries() -> Any:
    """Collapse the retry backoff so the suite finishes quickly.

    Production defaults (1s base, 30s ceiling) are exercised by the retry unit
    tests directly; here we only care that the retry *logic* runs.
    """
    previous = {key: os.environ.get(key) for key in ("KOLLEKTIV_RETRY_BASE_DELAY", "KOLLEKTIV_RETRY_MAX_DELAY")}
    os.environ["KOLLEKTIV_RETRY_BASE_DELAY"] = "0.01"
    os.environ["KOLLEKTIV_RETRY_MAX_DELAY"] = "0.05"
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------
def make_settings(**kwargs: Any) -> Settings:
    """Build :class:`Settings` for tests, ignoring any developer ``.env`` file.

    ``_env_file`` exists at runtime (pydantic-settings) but is not part of the
    generated signature, hence the single ignore.
    """
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Settings with an in-memory database, a temp workspace and no secrets."""
    return make_settings(
        SECRET_KEY="test-secret-key",
        ENVIRONMENT="development",
        DATABASE_URL="sqlite://",
        WORKSPACE_DIR=str(tmp_path / "workspace"),
        LOG_LEVEL="WARNING",
        AUTO_INIT_DB=True,
        GITHUB_TOKEN="ghp_test_token",
        GITHUB_REPO="octocat/hello-world",
        GITHUB_WEBHOOK_SECRET="webhook-secret",
        BRAIN_API_KEY="",
        BRAIN_PROVIDER="deepseek",
        TERABOX_BASE_URL="https://openapi.terabox.com",
    )


@pytest.fixture()
def agent_settings(settings: Settings) -> Settings:
    """Settings already carrying two worker agent accounts."""
    accounts = [
        {"name": "Vega", "provider": "custom", "base_url": "https://worker1.example.com/v1"},
        {"name": "Terra", "provider": "custom", "base_url": "https://worker2.example.com/v1"},
    ]
    return settings.model_copy(update={"ARENA_ACCOUNTS": json.dumps(accounts)}, deep=True)


@pytest.fixture()
def terabox_accounts() -> List[Dict[str, str]]:
    """Two TeraBox account dicts for pool tests."""
    return [
        {"email": "box1@example.com", "access_token": "tb-1", "refresh_token": "rt-1"},
        {"email": "box2@example.com", "access_token": "tb-2", "refresh_token": "rt-2"},
    ]


# ----------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------
@pytest.fixture(autouse=True)
def db_engine():
    """An in-memory SQLite engine wired into ``src.db.models`` for the test.

    Autouse: every test (including the ones that never touch SQLite directly)
    gets an isolated database, so encrypted tokens written by one test can
    never leak into another.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db_models.set_engine(engine)
    db_models.init_db(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)
    db_models.set_engine(None)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# HTTP mocking
# ----------------------------------------------------------------------
class MockRouter:
    """Route httpx requests by method + path prefix to handler callables.

    Each handler receives the :class:`httpx.Request` and returns either an
    :class:`httpx.Response` or a ``(status, payload)`` tuple.
    """

    def __init__(self) -> None:
        self.routes: List[tuple[str, str, Any]] = []
        self.calls: List[Dict[str, Any]] = []

    def add(self, method: str, path_contains: str, handler: Any) -> "MockRouter":
        """Register a handler for ``method`` + path substring."""
        self.routes.append((method.upper(), path_contains, handler))
        return self

    def respond(self, method: str, path_contains: str, status: int = 200, payload: Any = None) -> "MockRouter":
        """Register a static JSON response."""

        def handler(request: httpx.Request) -> httpx.Response:
            if isinstance(payload, (dict, list)):
                return httpx.Response(status, json=payload)
            return httpx.Response(status, text="" if payload is None else str(payload))

        return self.add(method, path_contains, handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        """The httpx.MockTransport handler entry point."""
        body = request.content.decode("utf-8", errors="replace") if request.content else ""
        self.calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "path": request.url.path,
                "body": body,
                "headers": dict(request.headers),
            }
        )
        for method, path_contains, handler in self.routes:
            if method == request.method and path_contains in request.url.path:
                result = handler(request)
                if isinstance(result, tuple):
                    status, payload = result
                    if isinstance(payload, (dict, list)):
                        return httpx.Response(status, json=payload)
                    return httpx.Response(status, text=str(payload))
                return result
        return httpx.Response(404, json={"message": f"no mock route for {request.method} {request.url.path}"})

    def client(self, base_url: str = "https://mock.local") -> httpx.AsyncClient:
        """Build an AsyncClient bound to this router."""
        return httpx.AsyncClient(
            base_url=base_url,
            transport=httpx.MockTransport(self.handler),
            headers={"User-Agent": "Kollektiv-tests"},
        )


@pytest.fixture()
def router() -> MockRouter:
    """A fresh mock router per test."""
    return MockRouter()


# ----------------------------------------------------------------------
# Fake brain client
# ----------------------------------------------------------------------
class FakeMessage:
    """Minimal stand-in for an OpenAI chat message."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.role = "assistant"


class FakeChoice:
    """Minimal stand-in for an OpenAI choice."""

    def __init__(self, content: str) -> None:
        self.message = FakeMessage(content)
        self.index = 0


class FakeCompletion:
    """Minimal stand-in for a chat completion response."""

    def __init__(self, content: str) -> None:
        self.choices = [FakeChoice(content)]
        self.model = "fake-model"


class FakeCompletions:
    """``client.chat.completions`` replacement that records calls.

    Responses are taken from ``responses`` in order. When that list is empty
    the optional ``responder`` callable is asked for content based on the
    request, which lets tests answer different prompt types apart (planner vs
    reviewer vs tactical advice).
    """

    def __init__(
        self,
        responses: Optional[Iterable[str]] = None,
        default: str = "{}",
        responder: Optional[Any] = None,
    ) -> None:
        self.responses = list(responses or [])
        self.default = default
        self.responder = responder
        self.calls: List[Dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeCompletion:
        """Return the next scripted response (or ask the responder)."""
        self.calls.append(kwargs)
        if self.responses:
            content = self.responses.pop(0)
        elif self.responder is not None:
            content = self.responder(kwargs)
        else:
            content = self.default
        return FakeCompletion(content)


class FakeChat:
    """``client.chat`` replacement."""

    def __init__(self, completions: FakeCompletions) -> None:
        self.completions = completions


class FakeOpenAIClient:
    """Duck-typed ``AsyncOpenAI`` used by brain and agent tests."""

    def __init__(
        self,
        responses: Optional[Iterable[str]] = None,
        default: str = "{}",
        responder: Optional[Any] = None,
    ) -> None:
        self.completions = FakeCompletions(responses, default, responder)
        self.chat = FakeChat(self.completions)
        self.closed = False

    @property
    def calls(self) -> List[Dict[str, Any]]:
        """Every completion request made against this client."""
        return self.completions.calls

    async def close(self) -> None:
        """Record that the client was closed."""
        self.closed = True


@pytest.fixture()
def fake_openai() -> FakeOpenAIClient:
    """A fake OpenAI client returning an empty JSON object by default."""
    return FakeOpenAIClient()


# ----------------------------------------------------------------------
# Fake storage / agents used by orchestrator and sync tests
# ----------------------------------------------------------------------
class FakeTeraBoxPool:
    """In-memory stand-in for :class:`TeraBoxPoolManager`."""

    def __init__(self, root: str = "/Kollektiv") -> None:
        self.root = root
        self.files: Dict[str, str] = {}
        self.uploads: List[tuple[str, str]] = []
        self.account_count = 1
        self.accounts: List[Any] = []
        self.settings = make_settings(SECRET_KEY="test", TERABOX_REMOTE_ROOT=root)

    @property
    def remote_root(self) -> str:
        """Root path for stored objects (mirrors the real pools)."""
        return self.root

    def is_configured(self) -> bool:
        """Pretend to be configured so sync paths run."""
        return True

    async def initialize(self) -> Dict[str, Any]:
        """No-op initialisation."""
        return {"initialized": True, "accounts": 1, "healthy": 1, "failed": 0}

    async def read_text(self, remote_path: str) -> Optional[str]:
        """Return the stored text for ``remote_path``."""
        return self.files.get(remote_path)

    async def write_text(self, remote_path: str, content: str) -> Dict[str, Any]:
        """Store text at ``remote_path``."""
        self.files[remote_path] = content
        self.uploads.append((remote_path, content))
        return {"path": remote_path, "size": len(content.encode()), "url": "https://example.invalid/x"}

    async def upload_file(self, local_path: str, remote_path: str) -> Dict[str, Any]:
        """Read a local file and store it."""
        content = Path(local_path).read_text(encoding="utf-8")
        self.files[remote_path] = content
        self.uploads.append((remote_path, content))
        return {"path": remote_path, "size": len(content.encode()), "url": "", "account_id": "acct-1"}

    async def download_file(self, remote_path: str, local_path: str) -> bool:
        """Write the stored text to a local path."""
        if remote_path not in self.files:
            return False
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_text(self.files[remote_path], encoding="utf-8")
        return True

    async def list_project_files(self, project_id: str) -> List[Dict[str, Any]]:
        """List stored paths under a project prefix."""
        return [
            {"path": path, "size": len(content.encode()), "type": "file", "account_id": "acct-1"}
            for path, content in self.files.items()
            if f"/{project_id}/" in path
        ]

    async def get_total_quota(self) -> Dict[str, Any]:
        """Report a fixed quota."""
        return {"used_gb": 1.0, "free_gb": 9.0, "total_gb": 10.0, "accounts": 1, "healthy": 1, "per_account": []}

    def get_pool_status(self) -> List[Dict[str, Any]]:
        """Return a single fake account status."""
        return [{"account_id": "acct-1", "healthy": True, "free_gb": 9.0}]

    async def close(self) -> None:
        """No-op shutdown."""
        return None


class FakeAgent:
    """A worker agent that returns scripted output and tracks usage."""

    def __init__(self, account_id: str, outputs: Optional[Iterable[str]] = None) -> None:
        self.account_id = account_id
        self.name = ""
        self.label = account_id
        self.email = f"{account_id}@example.com"
        self.outputs = list(outputs or ["```python path=src/app.py\nprint('hi')\n```"])
        self.prompts: List[str] = []
        self.busy = False
        self.tasks_done = 0
        self.tasks_failed = 0
        self.total_latency_ms = 0.0
        self.last_error = ""
        self.last_used_at = 0.0
        self.cooldown_until = 0.0

    def is_rate_limited(self) -> bool:
        """Never rate limited in tests."""
        return False

    async def is_ready(self) -> bool:
        """Always ready."""
        return True

    async def send_prompt(self, prompt: str, **kwargs: Any) -> str:
        """Record the prompt and return the next scripted output."""
        self.prompts.append(prompt)
        self.tasks_done += 1
        return self.outputs.pop(0) if self.outputs else ""

    async def reset_session(self) -> bool:
        """Pretend a reset succeeded."""
        return True

    async def get_session_status(self, probe: bool = False) -> Dict[str, Any]:
        """Return a healthy status payload."""
        return {"account_id": self.account_id, "label": self.label, "alive": True, "token_valid": True}

    def apply_rate_limit(self, seconds: Optional[float] = None) -> None:
        """Record a cooldown."""
        self.cooldown_until = 1.0

    def stats(self) -> Dict[str, Any]:
        """Return agent statistics."""
        return {
            "account_id": self.account_id,
            "label": self.label,
            "status": "idle",
            "tasks_done": self.tasks_done,
            "tasks_failed": self.tasks_failed,
            "authenticated": True,
        }

    async def close(self) -> None:
        """No-op shutdown."""
        return None


class FakeAgentPool:
    """A pool with one or more :class:`FakeAgent` workers."""

    def __init__(self, agents: Optional[List[FakeAgent]] = None) -> None:
        self._agents = agents or [FakeAgent("agent-1")]
        self.max_concurrency = 2
        self.settings = make_settings(SECRET_KEY="test")
        #: Display names, mirroring the real pool's contract (see src/agents/names.py).
        self.names: Dict[str, str] = {
            agent.account_id: getattr(agent, "name", "") or f"Agent {index + 1}"
            for index, agent in enumerate(self._agents)
        }

    @property
    def agents(self) -> List[FakeAgent]:
        """All fake agents."""
        return self._agents

    @property
    def size(self) -> int:
        """Number of fake agents."""
        return len(self._agents)

    def is_configured(self) -> bool:
        """Always configured."""
        return True

    def get_agent(self, account_id: str) -> Optional[FakeAgent]:
        """Look up an agent by id."""
        return next((agent for agent in self._agents if agent.account_id == account_id), None)

    async def initialize(self) -> Dict[str, Any]:
        """No-op initialisation."""
        return {"initialized": True, "agents": self.size, "ready": self.size, "failed": 0, "details": []}

    async def ensure_initialized(self) -> None:
        """No-op."""
        return None

    async def get_available_agent(self, exclude: Optional[List[str]] = None) -> Optional[FakeAgent]:
        """Return the first agent not excluded."""
        excluded = set(exclude or [])
        return next((agent for agent in self._agents if agent.account_id not in excluded), None)

    async def wait_for_agent(self, timeout: Optional[float] = None) -> Optional[FakeAgent]:
        """Return the first agent immediately."""
        return self._agents[0] if self._agents else None

    async def assign_task(self, task: Dict[str, Any], context: str = "", **kwargs: Any) -> str:
        """Delegate to the first available agent."""
        agent = await self.get_available_agent()
        if agent is None:
            raise RuntimeError("no agents available")
        return await agent.send_prompt(f"{context}\n{task.get('title', '')}")

    async def assign_many(self, jobs: List[Dict[str, Any]], **kwargs: Any) -> List[Dict[str, Any]]:
        """Run every job against the fake agents."""
        results = []
        for job in jobs:
            output = await self.assign_task(job.get("task", {}), job.get("context", ""))
            results.append(
                {"task_id": job.get("task", {}).get("id"), "success": True, "output": output, "error": ""}
            )
        return results

    async def get_pool_status(self, probe: bool = False) -> List[Dict[str, Any]]:
        """Return statuses for every fake agent."""
        return [agent.stats() for agent in self._agents]

    async def refresh_all_sessions(self, force: bool = False) -> Dict[str, Any]:
        """Pretend all sessions refreshed."""
        return {"checked": self.size, "refreshed": self.size, "failed": 0, "details": []}

    def snapshot(self) -> List[Dict[str, Any]]:
        """Return a synchronous status snapshot."""
        return [agent.stats() for agent in self._agents]

    async def close(self) -> None:
        """No-op shutdown."""
        return None
