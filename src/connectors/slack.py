"""Slack connector: post messages with a bot token or an incoming webhook.

Two independent routes, exactly like Discord:

* **Bot token** (``SLACK_BOT_TOKEN``, ``xoxb-...``) — Web API access. Create an
  app at api.slack.com/apps, add the ``chat:write`` scope (and ``channels:read``
  for ``list_channels``), install it to the workspace, then invite the bot to
  the channel you want it to post in.
* **Incoming webhook** (``SLACK_WEBHOOK_URL``) — one URL, one channel, no app.

Slack's Web API always answers HTTP 200, so failures are checked in the body
(``{"ok": false, "error": "..."}``); that is handled here rather than left to
the caller, because an "ok" envelope around a rejection is a classic trap.
"""

from __future__ import annotations

from typing import Any, List, Optional

import httpx

from config.settings import Settings
from src.connectors.base import Connector, ConnectorAction, Payload
from src.utils.errors import AuthenticationError, ConnectorError, ConnectorTransientError
from src.utils.logger import get_logger
from src.utils.retry import async_retry

LOGGER = get_logger(__name__)

#: Slack's own guidance for ``chat.postMessage``; longer text is chunked.
MAX_MESSAGE_LENGTH = 3000

SLACK_ACTIONS: List[ConnectorAction] = [
    ConnectorAction(
        name="auth_test",
        description="Return the token's identity, team and scopes (connectivity check).",
        params={},
    ),
    ConnectorAction(
        name="list_channels",
        description="List public channels so you can copy an id or name.",
        params={"limit": "Max channels (default 50).", "types": "Conversation types (default public_channel)."},
    ),
    ConnectorAction(
        name="post_message",
        description="Post a message to a channel with the bot token.",
        params={
            "text": "Message text (Slack markup is passed through).",
            "channel": "Channel id or name; defaults to SLACK_DEFAULT_CHANNEL.",
            "thread_ts": "Optional parent timestamp to answer in a thread.",
        },
        dangerous=True,
    ),
    ConnectorAction(
        name="send_webhook",
        description="Post a message through the configured incoming webhook.",
        params={"text": "Message text."},
        dangerous=True,
    ),
]


class SlackConnector(Connector):
    """Slack Web API and incoming webhook access."""

    name = "slack"
    category = "chat"
    description = "Slack: post to channels with a bot token or an incoming webhook."
    required_env = ("SLACK_BOT_TOKEN", "SLACK_WEBHOOK_URL")

    def __init__(
        self,
        settings: Optional[Settings] = None,
        token_store: Any = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        """Build the connector; the client is injectable for tests.

        Args:
            settings: Optional settings override.
            token_store: Optional encrypted token store.
            client: Optional pre-built client (tests); one is created otherwise.
        """
        super().__init__(settings, token_store=token_store, client=client)
        if client is None:
            from src.utils.net import async_client_kwargs

            self._client = httpx.AsyncClient(
                **async_client_kwargs(
                    self.settings,
                    base_url=self.settings.SLACK_BASE_URL.rstrip("/"),
                    timeout=httpx.Timeout(self.settings.SLACK_REQUEST_TIMEOUT, connect=10.0),
                )
            )

    @property
    def token(self) -> str:
        """Bot token: the encrypted store wins over the environment."""
        stored = self.stored_token() or {}
        return str(stored.get("access_token") or stored.get("token") or self.settings.SLACK_BOT_TOKEN)

    @property
    def webhook_url(self) -> str:
        """Incoming webhook URL, if configured (stored token wins)."""
        stored = self.stored_token(account="webhook") or {}
        return str(stored.get("url") or self.settings.SLACK_WEBHOOK_URL)

    @property
    def is_configured(self) -> bool:
        """True when either a bot token or a webhook URL is available."""
        return bool(self.token or self.webhook_url)

    def detail(self) -> str:
        """Report which of the two routes is available."""
        if self.token and self.webhook_url:
            return "ready (bot + webhook)"
        if self.token:
            return "ready (bot)"
        if self.webhook_url:
            return "ready (webhook only: send_webhook works, channel actions need a bot token)"
        return "not configured (set SLACK_BOT_TOKEN and/or SLACK_WEBHOOK_URL)"

    #: ``auth.test`` costs nothing and proves the token and its scopes.
    probe_action = "auth_test"
    probe_params: Payload = {}

    @async_retry(max_retries=3, retry_on=(ConnectorTransientError, httpx.TransportError, httpx.TimeoutException))
    async def _request(self, method: str, path: str, **kwargs: Any) -> Payload:
        """Call the Slack Web API and unwrap its ``ok`` envelope."""
        if not self.token:
            raise AuthenticationError("SLACK_BOT_TOKEN is unset; create a Slack app with the chat:write scope")
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json; charset=utf-8"}
        response = await self._client.request(method, path, headers=headers, **kwargs)
        if response.status_code == 429:
            raise ConnectorTransientError("Slack rate limit reached")
        if response.status_code >= 500:
            raise ConnectorTransientError(f"Slack returned {response.status_code}")
        try:
            data = response.json() if response.content else {}
        except ValueError as exc:  # non-JSON error page
            raise ConnectorError(f"Slack returned a non-JSON response ({response.status_code})") from exc
        if not isinstance(data, dict) or not data.get("ok", False):
            error = (data or {}).get("error") or f"HTTP {response.status_code}"
            if error in {"invalid_auth", "not_authed", "token_revoked", "account_inactive"}:
                raise AuthenticationError(f"Slack rejected the token: {error}")
            if error in {"ratelimited", "service_unavailable", "fatal_error"}:
                raise ConnectorTransientError(f"Slack is not ready: {error}")
            raise ConnectorError(f"Slack {path} failed: {error}")
        return data

    def actions(self) -> List[ConnectorAction]:
        """Return the Slack actions."""
        return SLACK_ACTIONS

    async def call(self, action: str, params: Payload) -> Any:
        """Perform a Slack action.

        Args:
            action: Action name from :data:`SLACK_ACTIONS`.
            params: Action parameters.

        Returns:
            A JSON-friendly result.

        Raises:
            ConnectorError: For unknown actions or missing required parameters.
        """
        if action == "auth_test":
            data = await self._request("POST", "/auth.test")
            return {
                "team": data.get("team"),
                "user": data.get("user"),
                "bot_id": data.get("bot_id"),
                "url": data.get("url"),
            }
        if action == "list_channels":
            data = await self._request(
                "POST",
                "/conversations.list",
                json={
                    "limit": max(1, min(int(params.get("limit") or 50), 200)),
                    "types": str(params.get("types") or "public_channel"),
                    "exclude_archived": True,
                },
            )
            return [
                {"id": channel.get("id"), "name": channel.get("name"), "members": channel.get("num_members")}
                for channel in data.get("channels", []) or []
            ]
        if action == "post_message":
            channel = str(params.get("channel") or self.settings.SLACK_DEFAULT_CHANNEL)
            if not channel:
                raise ConnectorError("post_message needs a channel (or set SLACK_DEFAULT_CHANNEL)")
            chunks = _chunk(str(params.get("text") or ""))
            if not chunks:
                raise ConnectorError("post_message needs some text")
            sent: List[str] = []
            for index, chunk in enumerate(chunks):
                body: Payload = {"channel": channel, "text": chunk}
                # Only the first chunk continues an existing thread.
                if params.get("thread_ts") and index == 0:
                    body["thread_ts"] = str(params["thread_ts"])
                data = await self._request("POST", "/chat.postMessage", json=body)
                sent.append(str(data.get("ts")))
                channel = str(data.get("channel") or channel)
            return {"sent": True, "messages": len(sent), "ts": sent, "channel": channel}
        if action == "send_webhook":
            if not self.webhook_url:
                raise ConnectorError("SLACK_WEBHOOK_URL is unset; create an incoming webhook in your Slack app")
            chunks = _chunk(str(params.get("text") or ""))
            if not chunks:
                raise ConnectorError("send_webhook needs some text")
            for chunk in chunks:
                response = await self._client.post(self.webhook_url, json={"text": chunk})
                if response.status_code >= 400:
                    raise ConnectorError(f"Slack webhook returned {response.status_code}: {response.text[:200]}")
            return {"sent": True, "messages": len(chunks), "channel": "webhook"}
        raise ConnectorError(f"Unhandled Slack action {action!r}")


def _chunk(text: str, limit: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """Split text into Slack-sized chunks on line boundaries where possible."""
    body = text.strip()
    if not body:
        return []
    chunks: List[str] = []
    while len(body) > limit:
        window = body[:limit]
        cut = max(window.rfind("\n"), window.rfind(" "))
        if cut < limit // 2:
            cut = limit
        chunks.append(body[:cut].rstrip())
        body = body[cut:].lstrip()
    if body:
        chunks.append(body)
    return chunks


__all__ = ["MAX_MESSAGE_LENGTH", "SLACK_ACTIONS", "SlackConnector"]
