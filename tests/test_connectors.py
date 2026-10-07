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
from src.connectors.discord import DiscordConnector
from src.connectors.github import GitHubConnector
from src.connectors.google_workspace import GoogleWorkspaceConnector
from src.connectors.linear import LinearConnector
from src.connectors.notion import NotionConnector
from src.connectors.rest import RestConnector, build_rest_connectors
from src.connectors.slack import SlackConnector
from src.connectors.telegram import TelegramConnector
from src.connectors.webhook import WebhookConnector
from src.connectors.whatsapp import WhatsAppConnector
from src.utils.errors import AuthenticationError, ConfigurationError, ConnectorError


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
    assert registry.names == [
            "discord",
            "github",
            "google",
            "linear",
            "notion",
            "slack",
            "telegram",
            "webhook",
            "whatsapp",
        ]
    statuses = {status["name"]: status for status in registry.statuses()}
    assert statuses["google"]["configured"] is False
    assert "GOOGLE_CLIENT_ID" in statuses["google"]["detail"]
    assert "gmail_search" in statuses["google"]["actions"]
    assert "gmail_send" in statuses["google"]["dangerous_actions"]
    assert registry.summary()["count"] == 9


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
    assert registry.names == [
            "discord",
            "github",
            "google",
            "linear",
            "notion",
            "slack",
            "telegram",
            "webhook",
            "whatsapp",
        ]


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
            assert body["count"] == 9 and "webhook" in body["configured"]
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
    for name in ("discord", "github", "google", "linear", "notion", "slack", "telegram", "webhook", "whatsapp"):
        assert name in output
    assert "0/9 ready" in output


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


# ----------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------
def telegram(settings: Settings, handler: Any, **extra: Any) -> TelegramConnector:
    """Build a Telegram connector with a mocked Bot API."""
    resolved = bare(settings, **{"TELEGRAM_BOT_TOKEN": "123:ABC", "TELEGRAM_CHAT_ID": "4242", **extra})
    return TelegramConnector(resolved, token_store=FakeTokenStore(), client=transport(handler, "https://api.telegram.org"))


def test_telegram_unwraps_the_ok_envelope(settings: Settings) -> None:
    """``{"ok": true, "result": ...}`` becomes just the result."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/bot123:ABC/getMe")
        return httpx.Response(200, json={"ok": True, "result": {"id": 7, "username": "kollektiv_bot"}})

    connector = telegram(settings, handler)
    assert run(connector.call("get_me", {}))["username"] == "kollektiv_bot"
    assert connector.is_configured is True


def test_telegram_reports_an_api_error_instead_of_swallowing_it(settings: Settings) -> None:
    """HTTP 200 with ``ok: false`` is still a failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    connector = telegram(settings, handler)
    with pytest.raises(ConnectorError, match="chat not found"):
        run(connector.call("send_message", {"text": "hello"}))


def test_telegram_send_message_uses_the_default_chat_and_validates(settings: Settings) -> None:
    """The configured chat id is the default; empty text and missing ids are refused."""
    seen: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 11, "chat": {"id": 4242}}})

    connector = telegram(settings, handler)
    result = run(connector.call("send_message", {"text": "deploy finished", "parse_mode": "Markdown"}))
    assert result == {"sent": True, "message_id": 11, "chat": 4242}
    assert seen[0]["chat_id"] == "4242" and seen[0]["parse_mode"] == "Markdown"

    with pytest.raises(ConnectorError, match="needs some text"):
        run(connector.call("send_message", {"text": "   "}))

    no_chat = telegram(settings, handler, TELEGRAM_CHAT_ID="")
    with pytest.raises(ConnectorError, match="chat_id"):
        run(no_chat.call("send_message", {"text": "hi"}))


def test_telegram_get_updates_clamps_the_limit(settings: Settings) -> None:
    """A caller cannot ask for more than Telegram allows."""
    seen: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content.decode()))
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 1}]})

    connector = telegram(settings, handler)
    assert run(connector.call("get_updates", {"limit": 5000}))["count"] == 1
    assert seen["limit"] == 100


def test_telegram_send_document_needs_a_target(settings: Settings) -> None:
    """A document requires a URL or file id."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return httpx.Response(200, json={"ok": True, "result": {}})

    connector = telegram(settings, handler)
    with pytest.raises(ConnectorError, match="document"):
        run(connector.call("send_document", {}))


def test_telegram_unknown_action_is_a_connector_error(settings: Settings) -> None:
    """An unknown action fails loudly and cheaply."""
    connector = telegram(settings, lambda request: httpx.Response(200, json={"ok": True, "result": {}}))
    with pytest.raises(ConnectorError, match="Unhandled Telegram action"):
        run(connector.call("delete_everything", {}))


# ----------------------------------------------------------------------
# Discord
# ----------------------------------------------------------------------
def discord(settings: Settings, handler: Any, **extra: Any) -> DiscordConnector:
    """Build a Discord connector with a mocked API."""
    resolved = bare(settings, **{"DISCORD_BOT_TOKEN": "bot-token", "DISCORD_DEFAULT_CHANNEL": "555", **extra})
    return DiscordConnector(
        resolved,
        token_store=FakeTokenStore(),
        client=transport(handler, "https://discord.com/api/v10"),
    )


def test_discord_webhook_only_is_configured_and_honest_about_it(settings: Settings) -> None:
    """A webhook alone is a usable route; the detail says what will not work."""
    connector = DiscordConnector(
        bare(settings, DISCORD_BOT_TOKEN="", DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/1/abc"),
        token_store=FakeTokenStore(),
    )
    assert connector.is_configured is True
    assert "webhook only" in connector.detail()

    empty = bare(settings, DISCORD_BOT_TOKEN="", DISCORD_WEBHOOK_URL="")
    assert DiscordConnector(empty, token_store=FakeTokenStore()).is_configured is False


def test_discord_uses_the_bot_prefix_and_default_channel(settings: Settings) -> None:
    """Discord wants ``Authorization: Bot …`` and the channel defaults to settings."""
    seen: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/users/@me"):
            return httpx.Response(200, json={"id": "9", "username": "kollektiv"})
        return httpx.Response(200, json={"id": "msg-1"})

    connector = discord(settings, handler)
    assert run(connector.call("get_me", {}))["username"] == "kollektiv"
    assert seen[0].headers["authorization"] == "Bot bot-token"

    result = run(connector.call("send_message", {"content": "build failed"}))
    assert result["channel_id"] == "555" and result["ids"] == ["msg-1"]
    assert json.loads(seen[1].content.decode()) == {"content": "build failed"}


def test_discord_chunks_long_messages(settings: Settings) -> None:
    """A 4500-character message becomes three posts, not a 400."""
    seen: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode())["content"])
        return httpx.Response(200, json={"id": f"m{len(seen)}"})

    connector = discord(settings, handler)
    result = run(connector.call("send_message", {"content": "x" * 4500}))
    assert result["messages"] == 3
    assert all(len(chunk) <= 2000 for chunk in seen)


def test_discord_rate_limits_are_retried(settings: Settings) -> None:
    """429 is transient, so the retry wrapper replays it and then succeeds."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, json={"message": "You are being rate limited."})
        return httpx.Response(200, json={"id": "1", "username": "kollektiv"})

    connector = discord(settings, handler)
    assert run(connector.call("get_me", {}))["id"] == "1"
    assert attempts["n"] == 2


def test_discord_rejects_a_message_without_a_channel(settings: Settings) -> None:
    """No channel, no post — and no request is made."""
    connector = discord(
        settings,
        lambda request: httpx.Response(200, json={}),  # pragma: no cover
        DISCORD_DEFAULT_CHANNEL="",
    )
    with pytest.raises(ConnectorError, match="channel_id"):
        run(connector.call("send_message", {"content": "hello"}))


def test_discord_webhook_route_posts_to_the_url(settings: Settings) -> None:
    """``send_webhook`` posts to the configured URL, not to the API host."""
    seen: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(204)

    connector = DiscordConnector(
        bare(settings, DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/1/abc"),
        token_store=FakeTokenStore(),
        client=transport(handler, "https://discord.com/api/v10"),
    )
    assert run(connector.call("send_webhook", {"content": "ping"}))["sent"] is True
    assert seen == ["https://discord.com/api/webhooks/1/abc"]


def test_discord_bot_actions_require_a_token(settings: Settings) -> None:
    """Bot actions without a token fail with an explanation, not a 401 storm."""
    connector = DiscordConnector(
        bare(settings, DISCORD_BOT_TOKEN="", DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/1/abc"),
        token_store=FakeTokenStore(),
        client=transport(lambda request: httpx.Response(200, json={}), "https://discord.com/api/v10"),
    )
    with pytest.raises(AuthenticationError, match="DISCORD_BOT_TOKEN"):
        run(connector.call("get_me", {}))


# ----------------------------------------------------------------------
# Slack
# ----------------------------------------------------------------------
def slack(settings: Settings, handler: Any, **extra: Any) -> SlackConnector:
    """Build a Slack connector with a mocked API."""
    resolved = bare(settings, **{"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_DEFAULT_CHANNEL": "#general", **extra})
    return SlackConnector(
        resolved,
        token_store=FakeTokenStore(),
        client=transport(handler, "https://slack.com/api"),
    )


def test_slack_treats_ok_false_as_a_failure(settings: Settings) -> None:
    """Slack answers HTTP 200 with ``ok: false``; that is an error, not a result."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    connector = slack(settings, handler)
    with pytest.raises(ConnectorError, match="channel_not_found"):
        run(connector.call("post_message", {"text": "hello"}))


def test_slack_auth_test_and_channel_listing(settings: Settings) -> None:
    """``auth_test`` proves the token; channels are trimmed to id/name/privacy."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth.test"):
            return httpx.Response(200, json={"ok": True, "team": "Kollektiv", "user": "bot", "url": "https://x.slack.com"})
        return httpx.Response(
            200,
            json={"ok": True, "channels": [{"id": "C1", "name": "general", "is_private": False}, {"id": "C2"}]},
        )

    connector = slack(settings, handler)
    assert run(connector.call("auth_test", {}))["team"] == "Kollektiv"
    assert run(connector.call("list_channels", {})) == [
        {"id": "C1", "name": "general", "members": None},
        {"id": "C2", "name": None, "members": None},
    ]


def test_slack_post_message_reports_each_chunk(settings: Settings) -> None:
    """Long text is chunked and every ``ts`` is returned."""
    seen: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode())["text"])
        return httpx.Response(200, json={"ok": True, "ts": f"t{len(seen)}"})

    connector = slack(settings, handler)
    result = run(connector.call("post_message", {"text": "y" * 4000}))
    assert result["messages"] == 2 and result["ts"] == ["t1", "t2"]
    assert all(len(chunk) <= 3000 for chunk in seen)
    assert result["channel"] == "#general"


def test_slack_webhook_route_works_without_a_bot_token(settings: Settings) -> None:
    """An incoming webhook needs only the URL."""
    seen: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="ok")

    connector = SlackConnector(
        bare(settings, SLACK_BOT_TOKEN="", SLACK_WEBHOOK_URL="https://hooks.slack.com/services/T/B/X"),
        token_store=FakeTokenStore(),
        client=transport(handler, "https://slack.com/api"),
    )
    assert connector.is_configured is True
    assert "webhook only" in connector.detail()
    assert run(connector.call("send_webhook", {"text": "deploy done"}))["sent"] is True
    assert seen == ["https://hooks.slack.com/services/T/B/X"]

    with pytest.raises(ConnectorError, match="some text"):
        run(connector.call("send_webhook", {"text": "   "}))

    no_url = SlackConnector(
        bare(settings, SLACK_BOT_TOKEN="", SLACK_WEBHOOK_URL=""),
        token_store=FakeTokenStore(),
        client=transport(handler, "https://slack.com/api"),
    )
    with pytest.raises(ConnectorError, match="SLACK_WEBHOOK_URL"):
        run(no_url.call("send_webhook", {"text": "hi"}))


# ----------------------------------------------------------------------
# Linear
# ----------------------------------------------------------------------
def linear(settings: Settings, handler: Any, **extra: Any) -> LinearConnector:
    """Build a Linear connector with a mocked GraphQL endpoint."""
    resolved = bare(settings, **{"LINEAR_API_KEY": "lin_api_test", "LINEAR_TEAM_ID": "team-1", **extra})
    return LinearConnector(resolved, token_store=FakeTokenStore(), client=transport(handler, "https://api.linear.app"))


def test_linear_viewer_and_teams(settings: Settings) -> None:
    """GraphQL responses are unwrapped into plain dicts."""
    queries: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        queries.append(body["query"])
        if "viewer" in body["query"]:
            return httpx.Response(200, json={"data": {"viewer": {"id": "u1", "name": "Ada", "email": "ada@x.io"}}})
        return httpx.Response(200, json={"data": {"teams": {"nodes": [{"id": "t1", "name": "Core", "key": "CORE"}]}}})

    connector = linear(settings, handler)
    assert run(connector.call("viewer", {}))["user"]["name"] == "Ada"
    assert run(connector.call("list_teams", {}))[0]["key"] == "CORE"
    assert len(queries) == 2


def test_linear_surfaces_graphql_errors(settings: Settings) -> None:
    """A 200 response carrying ``errors`` is still a failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "Entity not found"}]})

    connector = linear(settings, handler)
    with pytest.raises(ConnectorError, match="Entity not found"):
        run(connector.call("viewer", {}))


def test_linear_create_issue_requires_title_and_team(settings: Settings) -> None:
    """Missing fields are refused before any request goes out."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return httpx.Response(200, json={"data": {}})

    connector = linear(settings, handler)
    with pytest.raises(ConnectorError, match="title"):
        run(connector.call("create_issue", {"team_id": "t1"}))

    no_team = linear(settings, handler, LINEAR_TEAM_ID="")
    with pytest.raises(ConnectorError, match="team"):
        run(no_team.call("create_issue", {"title": "Ship it"}))


def test_linear_create_issue_and_comment(settings: Settings) -> None:
    """The mutation payload is sent as declared and the result is trimmed."""
    sent: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        sent.append(body)
        if "commentCreate" in body["query"]:
            return httpx.Response(200, json={"data": {"commentCreate": {"success": True, "comment": {"id": "c1", "url": "https://linear.app/c/1"}}}})
        return httpx.Response(200, json={"data": {"issueCreate": {"success": True, "issue": {"id": "i1", "identifier": "CORE-1", "url": "https://linear.app/i/1"}}}})

    connector = linear(settings, handler)
    issue = run(connector.call("create_issue", {"title": "Ship it", "description": "soon", "priority": 2}))
    assert issue["identifier"] == "CORE-1"
    assert sent[0]["variables"]["input"]["teamId"] == "team-1"
    assert sent[0]["variables"]["input"]["priority"] == 2

    comment = run(connector.call("comment_issue", {"issue_id": "i1", "body": "done"}))
    assert comment["created"] is True and sent[1]["variables"]["input"]["issueId"] == "i1"

    with pytest.raises(ConnectorError, match="priority"):
        run(connector.call("create_issue", {"title": "x", "priority": 9}))


def test_linear_list_issues_filters(settings: Settings) -> None:
    """Only the filters that were passed reach the query variables."""
    seen: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content.decode())["variables"])
        return httpx.Response(200, json={"data": {"issues": {"nodes": []}}})

    connector = linear(settings, handler)
    run(connector.call("list_issues", {"limit": 5, "team_id": "t9", "state": "In Progress"}))
    assert seen == {"first": 5, "teamId": "t9", "state": "In Progress"}


# ----------------------------------------------------------------------
# WhatsApp (official Cloud API + the opt-in OpenClaw-style bridge)
# ----------------------------------------------------------------------
def test_whatsapp_cloud_route_posts_an_official_message(settings: Settings) -> None:
    """The Cloud API is the default when its credentials exist."""
    sent: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v21.0/12345/messages")
        sent.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})

    resolved = bare(settings, WA_BACKEND="cloud", WA_CLOUD_TOKEN="eaag", WA_PHONE_NUMBER_ID="12345", WA_DEFAULT_TO="2348000000000")
    connector = WhatsAppConnector(resolved, token_store=FakeTokenStore(), client=transport(handler, "https://graph.facebook.com"))
    result = run(connector.call("send_message", {"text": "hi", "to": "+2348000000000"}))
    assert result["sent"] is True and result["to"] == "2348000000000"
    assert sent[0]["text"] == {"preview_url": False, "body": "hi"}
    assert run(connector.call("status", {}))["backend"] == "cloud"


def test_whatsapp_cloud_route_requires_credentials(settings: Settings) -> None:
    """Complete credentials are required before the cloud route is usable."""
    resolved = bare(settings, WA_BACKEND="cloud", WA_CLOUD_TOKEN="", WA_PHONE_NUMBER_ID="")
    connector = WhatsAppConnector(resolved, token_store=FakeTokenStore())
    assert connector.is_configured is False
    assert "WA_CLOUD_TOKEN" in connector.detail()
    with pytest.raises(ConnectorError, match="not usable"):
        run(connector.call("send_message", {"text": "hi", "to": "123"}))


def test_whatsapp_bridge_is_inert_until_the_unofficial_flag_is_set(settings: Settings) -> None:
    """The OpenClaw-style linked-device route is opt-in, and says why."""
    resolved = bare(settings, WA_BACKEND="bridge", WA_BRIDGE_URL="http://127.0.0.1:8090", WA_ALLOW_UNOFFICIAL=False)
    connector = WhatsAppConnector(resolved, token_store=FakeTokenStore())
    assert connector.is_configured is False
    detail = connector.detail()
    assert "terms" in detail and "banned" in detail
    assert run(connector.call("status", {}))["configured"] is False
    with pytest.raises(ConnectorError, match="WA_ALLOW_UNOFFICIAL|not usable"):
        run(connector.call("send_message", {"text": "hi", "to": "123"}))


def test_whatsapp_bridge_pairs_and_sends_when_enabled(settings: Settings) -> None:
    """With the flag set, the bridge contract is used exactly as documented."""
    calls: List[tuple] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.headers.get("authorization")))
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"connected": True, "me": "2348000000000"})
        return httpx.Response(200, json={"sent": True, "id": "3EB0"})

    resolved = bare(
        settings,
        WA_BACKEND="bridge",
        WA_BRIDGE_URL="http://127.0.0.1:8090",
        WA_BRIDGE_TOKEN="bridge-secret",
        WA_ALLOW_UNOFFICIAL=True,
        WA_DEFAULT_TO="2348000000000",
    )
    connector = WhatsAppConnector(resolved, token_store=FakeTokenStore(), client=transport(handler, "http://127.0.0.1:8090"))
    status = run(connector.call("status", {}))
    assert status["connected"] is True and "unofficial" in status["warning"]

    result = run(connector.call("send_message", {"text": "ping"}))
    assert result["sent"] is True and result["backend"] == "bridge"
    assert calls[1][:2] == ("POST", "http://127.0.0.1:8090/send")
    assert calls[1][2] == "Bearer bridge-secret"


def test_whatsapp_unknown_backend_is_unconfigured(settings: Settings) -> None:
    """A typo in WA_BACKEND never guesses."""
    connector = WhatsAppConnector(bare(settings, WA_BACKEND="carrier-pigeon"), token_store=FakeTokenStore())
    assert connector.backend == ""
    assert connector.is_configured is False
    assert "WA_BACKEND=cloud" in connector.detail() or "not configured" in connector.detail()


def test_whatsapp_rejects_an_empty_message_and_target(settings: Settings) -> None:
    """Both halves of a message are validated before dialling out."""
    resolved = bare(settings, WA_BACKEND="cloud", WA_CLOUD_TOKEN="t", WA_PHONE_NUMBER_ID="1", WA_DEFAULT_TO="")
    connector = WhatsAppConnector(resolved, token_store=FakeTokenStore(), client=transport(lambda request: httpx.Response(200, json={}), "https://graph.facebook.com"))
    with pytest.raises(ConnectorError, match="'to' number"):
        run(connector.call("send_message", {"text": "hi"}))
    with pytest.raises(ConnectorError, match="some text"):
        run(connector.call("send_message", {"to": "123", "text": "  "}))


# ----------------------------------------------------------------------
# The five new connectors inside the registry
# ----------------------------------------------------------------------
def test_new_connectors_are_optional_and_never_break_startup(settings: Settings) -> None:
    """None of them is required for the registry to exist or to report state."""
    registry = ConnectorRegistry.from_settings(bare(settings), token_store=FakeTokenStore())
    statuses = {status["name"]: status for status in registry.statuses()}
    for name in ("telegram", "discord", "slack", "linear", "whatsapp"):
        assert statuses[name]["configured"] is False
        assert statuses[name]["detail"]
        assert statuses[name]["actions"]
    assert "send_message" in statuses["telegram"]["dangerous_actions"]
    assert "create_issue" in statuses["linear"]["dangerous_actions"]


def test_new_connectors_appear_in_the_catalog_with_their_actions(settings: Settings) -> None:
    """The catalog is what the dashboard and the gateway both read."""
    registry = ConnectorRegistry.from_settings(bare(settings), token_store=FakeTokenStore())
    catalog = registry.catalog()
    by_connector: Dict[str, set] = {}
    for entry in catalog:
        by_connector.setdefault(entry["connector"], set()).add(entry["name"])
    assert by_connector["telegram"] == {"get_me", "get_updates", "send_message", "send_document"}
    assert by_connector["discord"] == {"get_me", "list_channels", "send_message", "send_webhook"}
    assert by_connector["slack"] == {"auth_test", "list_channels", "post_message", "send_webhook"}
    assert by_connector["linear"] == {"viewer", "list_teams", "list_issues", "create_issue", "comment_issue"}
    assert by_connector["whatsapp"] == {"status", "send_message"}
    assert all(entry["configured"] is False for entry in catalog)


def test_whatsapp_chunks_a_long_message(settings: Settings) -> None:
    """A 5000-character message is split, not truncated."""
    seen: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode())["text"]["body"])
        return httpx.Response(200, json={"messages": [{"id": f"wamid.{len(seen)}"}]})

    resolved = bare(settings, WA_BACKEND="cloud", WA_CLOUD_TOKEN="t", WA_PHONE_NUMBER_ID="1", WA_DEFAULT_TO="234")
    connector = WhatsAppConnector(resolved, token_store=FakeTokenStore(), client=transport(handler, "https://graph.facebook.com"))
    result = run(connector.call("send_message", {"text": "z" * 5000}))
    assert result["messages"] == 2 and result["message_id"] == "wamid.1"
    assert all(len(chunk) <= 4096 for chunk in seen)
    assert "".join(seen) == "z" * 5000
