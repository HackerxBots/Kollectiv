"""Hermetic tests for the connector layer (GitHub, Google, Notion, webhooks, REST).

No network and no real credentials: every connector takes an injected
``httpx.AsyncClient`` backed by :class:`httpx.MockTransport`, and the token
store is a plain dict.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Dict, List, Optional

import httpx
import pytest

from config.settings import Settings
from src.connectors.base import Connector, ConnectorAction, ConnectorRegistry
from src.connectors.github import GitHubConnector
from src.connectors.google_workspace import GoogleWorkspaceConnector
from src.connectors.notion import NotionConnector
from src.connectors.rest import RestConnector, build_rest_connectors
from src.connectors.webhook import WebhookConnector
from src.utils.errors import ConfigurationError, ConnectorError


class FakeTokenStore:
    """Minimal stand-in for :class:`~src.utils.token_store.TokenStore`."""

    def __init__(self, records: Optional[Dict[tuple, Dict[str, Any]]] = None) -> None:
        self.records = records or {}

    def get_token(self, service: str, account: str = "default") -> Dict[str, Any]:
        return dict(self.records.get((service, account), {}))

    def save_token(self, service: str, account: str, token_data: Dict[str, Any]) -> None:
        self.records[(service, account)] = dict(token_data)


def transport(handler: Any, base_url: str = "https://api.github.com") -> httpx.AsyncClient:
    """Build a MockTransport-backed client for a connector."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base_url)


def bare(settings: Settings, **extra: Any) -> Settings:
    """Settings with *no* service configured (the fixture configures GitHub)."""
    return settings.model_copy(update={"GITHUB_TOKEN": "", "GITHUB_REPO": "owner/repo", **extra})


def run(coro: Any) -> Any:
    """Run a coroutine from a synchronous test."""
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# Registry mechanics
# ----------------------------------------------------------------------
def test_registry_registers_every_connector_even_unconfigured(settings: Settings) -> None:
    """All four built-ins exist so the UI can explain what is missing."""
    registry = ConnectorRegistry.from_settings(bare(settings), token_store=FakeTokenStore())
    assert registry.names == ["github", "google", "notion", "webhook"]
    statuses = {status["name"]: status for status in registry.statuses()}
    assert statuses["google"]["configured"] is False
    assert "GOOGLE_CLIENT_ID" in statuses["google"]["detail"]
    assert "gmail_search" in statuses["google"]["actions"]
    assert "gmail_send" in statuses["google"]["dangerous_actions"]
    assert registry.summary()["count"] == 4


def test_registry_reports_configured_connectors(settings: Settings) -> None:
    """Configured connectors show up in ``configured`` and in the summary."""
    resolved = bare(settings, NOTION_TOKEN="ntn_test")
    registry = ConnectorRegistry.from_settings(resolved, token_store=FakeTokenStore())
    assert registry.configured_names() == ["notion"]
    assert registry.summary()["configured"] == ["notion"]
    assert registry.summary()["actions"] >= 15


def test_registry_rejects_unknown_connector_and_action(settings: Settings) -> None:
    """Unknown names and actions raise ConnectorError with the alternatives."""
    registry = ConnectorRegistry.from_settings(bare(settings), token_store=FakeTokenStore())
    with pytest.raises(ConnectorError, match="Unknown connector"):
        run(registry.call("nope", "x"))
    with pytest.raises(ConnectorError, match="available: gmail_search"):
        run(registry.call("google", "nope"))


def test_dangerous_actions_require_confirmation(settings: Settings) -> None:
    """A dangerous action refuses to run without ``confirm=True``."""
    resolved = bare(settings, NOTION_TOKEN="ntn_test")
    registry = ConnectorRegistry.from_settings(resolved, token_store=FakeTokenStore())
    with pytest.raises(ConnectorError, match="confirm=True"):
        run(registry.call("notion", "create_page", {"parent_id": "p", "title": "t"}))


def test_registry_refuses_unconfigured_connectors(settings: Settings) -> None:
    """An unconfigured connector explains what to set instead of calling out."""
    registry = ConnectorRegistry.from_settings(bare(settings), token_store=FakeTokenStore())
    with pytest.raises(ConnectorError, match="not configured"):
        run(registry.call("google", "gmail_search", {"query": "x"}))


def test_catalog_marks_configured_state(settings: Settings) -> None:
    """The catalogue is what the brain/UI reads: connector + action + flags."""
    resolved = bare(settings, EVENT_WEBHOOKS="https://hooks.test/x")
    registry = ConnectorRegistry.from_settings(resolved, token_store=FakeTokenStore())
    catalog = {entry["connector"]: entry for entry in registry.catalog() if entry["name"] == "notify"}
    assert catalog["webhook"]["configured"] is True
    assert catalog["webhook"]["dangerous"] is False


# ----------------------------------------------------------------------
# GitHub
# ----------------------------------------------------------------------
def test_github_connector_actions(settings: Settings) -> None:
    """Commits, PRs and files come back in the small shape agents expect."""
    resolved = settings.model_copy(
        update={"GITHUB_TOKEN": "ghp_test", "GITHUB_REPO": "HackerxBots/Kollectiv"}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            return httpx.Response(
                200,
                json=[{"sha": "abcdef1234567890", "commit": {"message": "feat: x\n\nbody", "author": {"name": "A", "date": "2026-01-01"}}}],
            )
        if request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[{"number": 7, "title": "Fix", "user": {"login": "octo"}, "html_url": "u"}])
        if "/contents/" in request.url.path:
            return httpx.Response(
                200,
                json={"content": base64.b64encode(b"hello\n").decode(), "encoding": "base64", "name": "README.md", "path": "README.md"},
            )
        return httpx.Response(404, json={})

    connector = GitHubConnector(resolved, token_store=FakeTokenStore())
    connector._github._client = transport(handler, base_url=resolved.GITHUB_API_URL)
    commits = run(connector.call("recent_commits", {"limit": 5}))
    assert commits[0]["sha"] == "abcdef123456" and commits[0]["message"] == "feat: x"
    pulls = run(connector.call("open_pull_requests", {}))
    assert pulls[0]["number"] == 7 and pulls[0]["title"] == "Fix"
    assert pulls[0]["author"] == "octo" and pulls[0]["status"] == "open" and pulls[0]["url"] == "u"
    assert connector.status()["configured"] is True


def test_github_connector_requires_a_real_repo(settings: Settings) -> None:
    """The owner/repo placeholder counts as unconfigured."""
    connector = GitHubConnector(bare(settings), token_store=FakeTokenStore())
    assert connector.is_configured is False


# ----------------------------------------------------------------------
# Google Workspace
# ----------------------------------------------------------------------
def _google_settings(settings: Settings, **extra: Any) -> Settings:
    """Settings with a complete Google OAuth client."""
    return settings.model_copy(
        update={
            "GOOGLE_CLIENT_ID": "client-id",
            "GOOGLE_CLIENT_SECRET": "client-secret",
            "GOOGLE_REFRESH_TOKEN": "refresh-token",
            **extra,
        }
    )


def test_google_refreshes_and_caches_the_access_token(settings: Settings) -> None:
    """One refresh call is made; the second action reuses the token."""
    calls: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/token":
            assert b"grant_type=refresh_token" in request.content
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if request.url.path.endswith("/messages"):
            assert request.headers["authorization"] == "Bearer at-1"
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        if "/messages/m1" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "id": "m1",
                    "snippet": "hi",
                    "payload": {"headers": [{"name": "Subject", "value": "Hello"}], "mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"Body!").decode()}},
                },
            )
        return httpx.Response(404, json={})

    connector = GoogleWorkspaceConnector(_google_settings(settings), token_store=FakeTokenStore())
    connector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    messages = run(connector.call("gmail_search", {"query": "is:unread"}))
    assert messages[0]["subject"] == "Hello"
    detail = run(connector.call("gmail_read", {"message_id": "m1"}))
    assert detail["body"] == "Body!"
    assert calls.count("/token") == 1


def test_google_send_builds_a_base64_message(settings: Settings) -> None:
    """``gmail_send`` encodes an RFC-5322 message for the Gmail API."""
    sent: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        if request.url.path.endswith("/messages/send"):
            sent.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "sent-1"})
        return httpx.Response(404, json={})

    connector = GoogleWorkspaceConnector(_google_settings(settings), token_store=FakeTokenStore())
    connector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = run(connector.call("gmail_send", {"to": "a@b.c", "subject": "Hi", "body": "Text"}))
    assert result == {"sent": True, "id": "sent-1", "to": "a@b.c"}
    decoded = base64.urlsafe_b64decode(sent["raw"] + "=" * (-len(sent["raw"]) % 4)).decode()
    assert "To: a@b.c" in decoded and "Text" in decoded


def test_google_drive_search_and_export(settings: Settings) -> None:
    """Drive search returns files and export falls back to a download."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json={"files": [{"id": "f1", "name": "Notes", "mimeType": "application/vnd.google-apps.document"}]})
        if request.url.path.endswith("/files/f1/export"):
            return httpx.Response(200, text="exported text")
        if request.url.path.endswith("/files/f1"):
            return httpx.Response(200, content=b"downloaded text")
        return httpx.Response(404, json={})

    connector = GoogleWorkspaceConnector(_google_settings(settings), token_store=FakeTokenStore())
    connector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    files = run(connector.call("drive_search", {"query": "name contains 'Notes'"}))
    assert files[0]["name"] == "Notes"
    assert run(connector.call("drive_export", {"file_id": "f1"}))["text"] == "exported text"


def test_google_prefers_the_encrypted_token_store(settings: Settings) -> None:
    """A stored refresh token wins over the (absent) environment value."""
    store = FakeTokenStore({("google", "default"): {"refresh_token": "stored", "client_id": "c", "client_secret": "s"}})
    connector = GoogleWorkspaceConnector(settings, token_store=store)
    assert connector.is_configured is True
    assert connector._refresh_token() == "stored"

    def handler(request: httpx.Request) -> httpx.Response:
        assert b"refresh_token=stored" in request.content
        return httpx.Response(200, json={"access_token": "at", "expires_in": 10})

    connector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert run(connector.access_token()) == "at"


def test_google_reports_a_bad_refresh_token(settings: Settings) -> None:
    """A rejected grant raises AuthenticationError, not a stack trace."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    connector = GoogleWorkspaceConnector(_google_settings(settings), token_store=FakeTokenStore())
    connector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    from src.utils.errors import AuthenticationError

    with pytest.raises(AuthenticationError, match="refused the refresh token"):
        run(connector.access_token())


# ----------------------------------------------------------------------
# Notion
# ----------------------------------------------------------------------
def test_notion_search_sends_the_version_header(settings: Settings) -> None:
    """Notion calls carry the integration token and the API version."""
    seen: Dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        seen["version"] = request.headers.get("notion-version", "")
        if request.url.path == "/v1/search":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "p1",
                            "object": "page",
                            "url": "https://notion.so/p1",
                            "last_edited_time": "2026-01-01T00:00:00Z",
                            "parent": {"type": "workspace"},
                            "properties": {"Name": {"type": "title", "title": [{"plain_text": "Roadmap"}]}},
                        }
                    ]
                },
            )
        return httpx.Response(404, json={})

    resolved = settings.model_copy(update={"NOTION_TOKEN": "ntn_x"})
    connector = NotionConnector(resolved, token_store=FakeTokenStore())
    connector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.notion.com")
    results = run(connector.call("search", {"query": "roadmap"}))
    assert results[0]["title"] == "Roadmap"
    assert seen["auth"] == "Bearer ntn_x"
    assert seen["version"] == resolved.NOTION_VERSION


def test_notion_create_page_wraps_paragraphs(settings: Settings) -> None:
    """``create_page`` turns text lines into paragraph blocks."""
    body: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "new", "url": "https://notion.so/new"})

    resolved = settings.model_copy(update={"NOTION_TOKEN": "ntn_x"})
    connector = NotionConnector(resolved, token_store=FakeTokenStore())
    connector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.notion.com")
    result = run(connector.call("create_page", {"parent_id": "p1", "title": "T", "content": "one\ntwo"}))
    assert result["created"] is True
    assert len(body["children"]) == 2
    assert body["properties"]["title"]["title"][0]["text"]["content"] == "T"


def test_notion_shares_the_page_with_the_integration(settings: Settings) -> None:
    """403 explains that the object was not shared with the integration."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "restricted"})

    resolved = settings.model_copy(update={"NOTION_TOKEN": "ntn_x"})
    connector = NotionConnector(resolved, token_store=FakeTokenStore())
    connector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.notion.com")
    from src.utils.errors import AuthenticationError

    with pytest.raises(AuthenticationError, match="share the page"):
        run(connector.call("search", {}))


# ----------------------------------------------------------------------
# Webhooks
# ----------------------------------------------------------------------
def test_webhook_notify_posts_to_every_target(settings: Settings) -> None:
    """Each configured URL receives the event; one failure does not stop the rest."""
    delivered: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        delivered.append(str(request.url))
        if "bad.test" in str(request.url):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text="ok")

    connector = WebhookConnector(settings, token_store=FakeTokenStore(), urls=["https://good.test/a", "https://bad.test/b"])
    connector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    results = run(connector.call("notify", {"text": "run finished"}))
    assert len(results) == 2
    assert any("error" in item for item in results)
    assert any(item.get("status") == 200 for item in results)
    # The bad target is retried (3 attempts), the good one succeeds once.
    assert set(delivered) == {"https://bad.test/b", "https://good.test/a"}


def test_webhook_is_unconfigured_without_urls(settings: Settings) -> None:
    """No URLs means a clear ConfigurationError, not a silent no-op."""
    connector = WebhookConnector(settings, token_store=FakeTokenStore(), urls=[])
    assert connector.is_configured is False
    with pytest.raises(ConfigurationError):
        run(connector.call("notify", {"text": "x"}))


def test_registry_broadcast_uses_the_webhook_connector(settings: Settings) -> None:
    """``registry.broadcast`` is the orchestrator's event fan-out."""
    payloads: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, text="ok")

    resolved = settings.model_copy(update={"EVENT_WEBHOOKS": "https://hooks.test/x"})
    registry = ConnectorRegistry.from_settings(resolved, token_store=FakeTokenStore())
    registry.get("webhook")._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[attr-defined]
    results = run(registry.broadcast({"type": "run.finished", "status": "completed"}))
    assert results[0]["status"] == 200
    assert payloads[0]["event"]["status"] == "completed"

    # No webhooks configured -> no-op, never an exception.
    quiet = ConnectorRegistry.from_settings(settings, token_store=FakeTokenStore())
    assert run(quiet.broadcast({"type": "x"})) == []


# ----------------------------------------------------------------------
# Declarative REST connectors
# ----------------------------------------------------------------------
def _slack_config() -> str:
    """A small declarative connector used by the REST tests."""
    return json.dumps(
        [
            {
                "name": "slack",
                "category": "chat",
                "description": "Slack messages",
                "base_url": "https://slack.test/api",
                "auth": "bearer",
                "token": "xoxb-test",
                "actions": [
                    {"name": "channel_history", "method": "GET", "path": "/conversations.history", "params": {"channel": "Channel", "limit": "Max"}},
                    {"name": "post_message", "method": "POST", "path": "/chat.postMessage", "dangerous": True, "params": {"channel": "Channel", "text": "Body"}},
                ],
            }
        ]
    )


def test_rest_connector_builds_from_settings(settings: Settings) -> None:
    """CUSTOM_CONNECTORS becomes a real connector with its actions."""
    resolved = settings.model_copy(update={"CUSTOM_CONNECTORS": _slack_config()})
    connectors = build_rest_connectors(resolved)
    assert [c.name for c in connectors] == ["slack"]
    slack = connectors[0]
    assert slack.is_configured is True
    assert [a.name for a in slack.actions()] == ["channel_history", "post_message"]
    assert slack.status()["dangerous_actions"] == ["post_message"]
    assert slack.category == "chat"


def test_rest_connector_calls_endpoints(settings: Settings) -> None:
    """Placeholders fill the path; params become query string or JSON body."""
    seen: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content or b"{}")
        return httpx.Response(200, json={"ok": True})

    resolved = settings.model_copy(update={"CUSTOM_CONNECTORS": _slack_config()})
    slack = build_rest_connectors(resolved)[0]
    slack._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://slack.test/api")
    result = run(slack.call("channel_history", {"channel": "C1", "limit": 3}))
    assert result["result"] == {"ok": True}
    assert seen["path"] == "/api/conversations.history"
    assert seen["query"] == {"channel": "C1", "limit": "3"}
    assert seen["auth"] == "Bearer xoxb-test"
    run(slack.call("post_message", {"channel": "C1", "text": "hi"}))
    assert seen["body"] == {"channel": "C1", "text": "hi"}


def test_rest_connector_validates_configuration(settings: Settings) -> None:
    """Bad entries are rejected loudly at build time."""
    with pytest.raises(ConfigurationError, match="base_url"):
        RestConnector({"name": "broken"}, settings=settings)
    with pytest.raises(ConfigurationError, match="at least one action"):
        RestConnector({"name": "broken", "base_url": "https://x.test"}, settings=settings)
    with pytest.raises(ConfigurationError, match="unknown auth"):
        RestConnector(
            {"name": "broken", "base_url": "https://x.test", "auth": "magic", "actions": [{"name": "a", "path": "/a"}]},
            settings=settings,
        )


def test_rest_connector_requires_its_token(settings: Settings) -> None:
    """A bearer connector without a token reports itself unconfigured."""
    config = [{"name": "api", "base_url": "https://api.test", "auth": "bearer", "actions": [{"name": "ping", "path": "/ping"}]}]
    resolved = settings.model_copy(update={"CUSTOM_CONNECTORS": json.dumps(config)})
    connector = build_rest_connectors(resolved)[0]
    assert connector.is_configured is False

    store = FakeTokenStore({("api", "default"): {"access_token": "from-store"}})
    resolved2 = settings.model_copy(update={"CUSTOM_CONNECTORS": json.dumps(config)})
    connector2 = RestConnector(config[0], settings=resolved2, token_store=store)
    assert connector2.token == "from-store" and connector2.is_configured is True


def test_rest_connector_masks_bad_json_and_reports_errors(settings: Settings) -> None:
    """Non-JSON responses are wrapped; HTTP errors raise ConnectorError."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ok"):
            return httpx.Response(200, text="plain text")
        return httpx.Response(400, text="nope")

    config = [
        {
            "name": "api",
            "base_url": "https://api.test",
            "auth": "none",
            "actions": [{"name": "ok", "path": "/ok"}, {"name": "bad", "path": "/bad"}],
        }
    ]
    resolved = settings.model_copy(update={"CUSTOM_CONNECTORS": json.dumps(config)})
    connector = build_rest_connectors(resolved)[0]
    connector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.test")
    assert run(connector.call("ok", {}))["result"]["text"] == "plain text"
    with pytest.raises(ConnectorError, match="returned 400"):
        run(connector.call("bad", {}))


def test_invalid_custom_connectors_never_break_startup(settings: Settings) -> None:
    """Broken JSON or entries are logged and skipped."""
    resolved = settings.model_copy(update={"CUSTOM_CONNECTORS": "not-json"})
    assert build_rest_connectors(resolved) == []
    registry = ConnectorRegistry.from_settings(resolved, token_store=FakeTokenStore())
    assert registry.names == ["github", "google", "notion", "webhook"]


# ----------------------------------------------------------------------
# Surfaces: orchestrator health, HTTP API, CLI
# ----------------------------------------------------------------------
async def test_orchestrator_exposes_connectors(settings: Settings) -> None:
    """``start()`` builds the registry, ``health()`` reports it, ``stop()`` closes it."""
    from src.orchestrator.app import Orchestrator

    resolved = bare(settings, NOTION_TOKEN="ntn_x", EVENT_WEBHOOKS="https://hooks.test/x")
    orchestrator = Orchestrator(resolved)
    report = await orchestrator.start()
    assert "connectors" in report
    assert report["connectors"]["count"] >= 4
    assert "notion" in report["connectors"]["configured"]
    health = await orchestrator.health()
    assert health["subsystems"]["connectors"]["configured"] == ["notion", "webhook"]
    assert health["subsystems"]["connectors"]["event_webhooks"] == 1
    await orchestrator.stop()
    assert orchestrator.connectors is not None  # kept for introspection after stop


def test_api_lists_and_calls_connectors(settings: Settings) -> None:
    """``GET /connectors`` and ``POST /connectors/{name}/call`` work end to end."""
    from src.api.routes import create_app
    from src.orchestrator.app import Orchestrator

    resolved = bare(settings, EVENT_WEBHOOKS="https://hooks.test/x")
    orchestrator = Orchestrator(resolved)
    orchestrator.connectors = ConnectorRegistry.from_settings(resolved, token_store=FakeTokenStore())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"delivered": True})

    orchestrator.connectors.get("webhook")._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[attr-defined]
    app = create_app(resolved, orchestrator=orchestrator)
    # ASGITransport does not run the lifespan; the real app sets this on startup.
    app.state.orchestrator = orchestrator
    transport_client = httpx.ASGITransport(app=app)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=transport_client, base_url="http://test") as client:
            listing = await client.get("/connectors")
            assert listing.status_code == 200
            body = listing.json()
            assert body["count"] == 4 and "webhook" in body["configured"]
            called = await client.post("/connectors/webhook/call", json={"action": "notify", "params": {"text": "hi"}})
            assert called.status_code == 200
            assert called.json()["result"][0]["status"] == 200
            missing = await client.post("/connectors/webhook/call", json={"action": "nope"})
            assert missing.status_code == 400
            unconfigured = await client.post("/connectors/notion/call", json={"action": "search"})
            assert unconfigured.status_code == 400

    run(scenario())


def test_cli_lists_connectors(settings: Settings, capsys: Any) -> None:
    """``kollektiv connectors`` prints each service and what is missing."""
    from src.api.cli import cmd_connectors

    args = type("Args", (), {"json": False})()
    import src.api.cli as cli_module
    from config import settings as settings_module

    resolved = bare(settings)
    original = settings_module.get_settings
    settings_module.get_settings = lambda: resolved  # type: ignore[assignment]
    cli_module.get_settings = lambda: resolved  # type: ignore[assignment]
    try:
        code = run(cmd_connectors(args))
    finally:
        settings_module.get_settings = original  # type: ignore[assignment]
    assert code == 0
    output = capsys.readouterr().out
    for name in ("github", "google", "notion", "webhook"):
        assert name in output
    assert "0/4 ready" in output


def test_cli_call_reports_errors_without_a_traceback(settings: Settings, capsys: Any) -> None:
    """A bad connector name is a clean JSON error and a non-zero exit code."""
    from src.api.cli import cmd_call

    args = type("Args", (), {"connector": "nope", "action": "x", "params": "{}", "confirm": False})()
    assert run(cmd_call(args)) == 1
    assert "Unknown connector" in capsys.readouterr().out


def test_settings_redacts_connector_credentials(settings: Settings) -> None:
    """Connector secrets never reach logs or /health."""
    resolved = settings.model_copy(
        update={
            "GOOGLE_CLIENT_SECRET": "sec",
            "GOOGLE_REFRESH_TOKEN": "ref",
            "NOTION_TOKEN": "ntn_x",
            "CUSTOM_CONNECTORS": "[{}]",
            "EVENT_WEBHOOKS": "https://hooks.test/x?token=abc",
        }
    )
    redacted = resolved.redacted()
    for field in ("GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN", "NOTION_TOKEN", "CUSTOM_CONNECTORS", "EVENT_WEBHOOKS"):
        assert redacted[field] == "***redacted***"


def test_connector_base_defaults() -> None:
    """The base class provides sane defaults for subclasses."""

    class Dummy(Connector):
        name = "dummy"
        description = "test"

        def actions(self) -> List[ConnectorAction]:
            return [ConnectorAction("ping", "Ping it")]

        async def call(self, action: str, params: Dict[str, Any]) -> Any:
            return {"pong": True}

    dummy = Dummy()
    assert dummy.is_configured is True
    assert dummy.detail() == "ready"
    assert dummy.catalog()[0]["connector"] == "dummy"
    assert run(dummy.call("ping", {})) == {"pong": True}
