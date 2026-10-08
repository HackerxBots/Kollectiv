"""Discord connector: post into channels with a bot, or through a webhook.

Two ways in, both optional and independent:

* **Bot token** (``DISCORD_BOT_TOKEN``) — full channel access. Create the
  application in the Discord developer portal, add a bot, invite it to the
  server with the ``Send Messages`` permission, then use the channel id
  (Developer Mode → right-click a channel → Copy Channel ID).
* **Incoming webhook** (``DISCORD_WEBHOOK_URL``) — no bot, no invite, one URL
  for one channel. Perfect for run summaries; useless for reading.

Messages sent as the bot honour Discord's 2000-character limit, so longer text
is split into chunks (the connector reports how many messages it sent).
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

#: Discord's hard limit for a message body.
MAX_MESSAGE_LENGTH = 2000

DISCORD_ACTIONS: List[ConnectorAction] = [
    ConnectorAction(
        name="get_me",
        description="Return the bot's own identity (cheap connectivity check).",
        params={},
    ),
    ConnectorAction(
        name="list_channels",
        description="List the guild's text channels so you can copy an id.",
        params={"guild_id": "Server id (or DISCORD_GUILD_ID / the bot's first guild)."},
    ),
    ConnectorAction(
        name="send_message",
        description="Post a message to a channel as the bot (long text is chunked).",
        params={
            "content": "Message text.",
            "channel_id": "Destination channel id; defaults to DISCORD_DEFAULT_CHANNEL.",
        },
        dangerous=True,
    ),
    ConnectorAction(
        name="send_webhook",
        description="Post a message through the configured incoming webhook.",
        params={
            "content": "Message text.",
            "username": "Optional display name for the webhook post.",
        },
        dangerous=True,
    ),
]


def chunk_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """Split ``text`` into Discord-sized chunks, preferring line boundaries.

    Args:
        text: The full message.
        limit: Maximum characters per chunk (Discord allows 2000).

    Returns:
        One or more non-empty chunks; a single chunk when the text is short.
    """
    body = text.strip()
    if not body:
        return []
    chunks: List[str] = []
    while len(body) > limit:
        window = body[:limit]
        cut = max(window.rfind("\n"), window.rfind(" "))
        if cut < limit // 2:  # no useful boundary: hard split
            cut = limit
        chunks.append(body[:cut].rstrip())
        body = body[cut:].lstrip()
    if body:
        chunks.append(body)
    return chunks


class DiscordConnector(Connector):
    """Discord bot and webhook access."""

    name = "discord"
    category = "chat"
    description = "Discord: post to channels as a bot, or through a webhook."
    required_env = ("DISCORD_BOT_TOKEN", "DISCORD_WEBHOOK_URL")

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
                    base_url=f"{self.settings.DISCORD_BASE_URL.rstrip('/')}/{self.settings.DISCORD_API_VERSION}",
                    timeout=httpx.Timeout(self.settings.DISCORD_REQUEST_TIMEOUT, connect=10.0),
                )
            )

    @property
    def token(self) -> str:
        """Bot token: the encrypted store wins over the environment."""
        stored = self.stored_token() or {}
        return str(stored.get("access_token") or stored.get("token") or self.settings.DISCORD_BOT_TOKEN)

    @property
    def webhook_url(self) -> str:
        """Incoming webhook URL, if one is configured (stored token wins)."""
        stored = self.stored_token(account="webhook") or {}
        return str(stored.get("url") or self.settings.DISCORD_WEBHOOK_URL)

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
        return "not configured (set DISCORD_BOT_TOKEN and/or DISCORD_WEBHOOK_URL)"

    #: ``GET /users/@me`` is one request and proves the bot token works.
    probe_action = "get_me"
    probe_params: Payload = {}

    def _headers(self) -> Payload:
        """Return bot request headers (or raise when there is no token)."""
        if not self.token:
            raise AuthenticationError("DISCORD_BOT_TOKEN is unset; create a bot in the Discord developer portal")
        return {
            "Authorization": f"Bot {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "Kollektiv (https://github.com/HackerxBots/Kollektiv)",
        }

    @async_retry(max_retries=3, retry_on=(ConnectorTransientError, httpx.TransportError, httpx.TimeoutException))
    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Call the Discord API and translate its errors."""
        response = await self._client.request(method, path, headers=self._headers(), **kwargs)
        if response.status_code in (401, 403):
            raise AuthenticationError(
                f"Discord rejected the bot ({response.status_code}); check the token and the channel permissions"
            )
        if response.status_code == 404:
            raise ConnectorError(f"Discord object not found at {path}")
        if response.status_code == 429:
            raise ConnectorTransientError("Discord rate limit reached")
        if response.status_code >= 500:
            raise ConnectorTransientError(f"Discord returned {response.status_code}")
        if response.status_code >= 400:
            raise ConnectorError(f"Discord returned {response.status_code}: {response.text[:200]}")
        return response.json() if response.content else {}

    def actions(self) -> List[ConnectorAction]:
        """Return the Discord actions."""
        return DISCORD_ACTIONS

    async def call(self, action: str, params: Payload) -> Any:
        """Perform a Discord action.

        Args:
            action: Action name from :data:`DISCORD_ACTIONS`.
            params: Action parameters.

        Returns:
            A JSON-friendly result.

        Raises:
            ConnectorError: For unknown actions or missing required parameters.
        """
        if action == "get_me":
            me = await self._request("GET", "/users/@me")
            return {"id": me.get("id"), "username": me.get("username"), "bot": me.get("bot", True)}
        if action == "list_channels":
            guild_id = str(params.get("guild_id") or "")
            if not guild_id:
                guilds = await self._request("GET", "/users/@me/guilds")
                if not guilds:
                    raise ConnectorError("the bot is in no guild; invite it first")
                guild_id = str(guilds[0].get("id"))
            channels = await self._request("GET", f"/guilds/{guild_id}/channels")
            return [
                {"id": channel.get("id"), "name": channel.get("name"), "type": channel.get("type")}
                for channel in channels or []
                if channel.get("type") in (0, 5)  # text and announcement channels
            ]
        if action == "send_message":
            channel_id = str(params.get("channel_id") or self.settings.DISCORD_DEFAULT_CHANNEL)
            if not channel_id:
                raise ConnectorError("send_message needs a channel_id (or set DISCORD_DEFAULT_CHANNEL)")
            chunks = chunk_message(str(params.get("content") or ""))
            if not chunks:
                raise ConnectorError("send_message needs some content")
            ids: List[str] = []
            for chunk in chunks:
                message = await self._request("POST", f"/channels/{channel_id}/messages", json={"content": chunk})
                ids.append(str(message.get("id")))
            return {"sent": True, "messages": len(ids), "ids": ids, "channel_id": channel_id}
        if action == "send_webhook":
            if not self.webhook_url:
                raise ConnectorError("DISCORD_WEBHOOK_URL is unset; create an incoming webhook in the channel settings")
            chunks = chunk_message(str(params.get("content") or ""))
            if not chunks:
                raise ConnectorError("send_webhook needs some content")
            for chunk in chunks:
                body: Payload = {"content": chunk}
                if params.get("username"):
                    body["username"] = str(params["username"])
                # The webhook URL is absolute, so the base URL is bypassed.
                response = await self._client.post(self.webhook_url, json=body)
                if response.status_code >= 400:
                    raise ConnectorError(f"Discord webhook returned {response.status_code}: {response.text[:200]}")
            return {"sent": True, "messages": len(chunks), "channel": "webhook"}
        raise ConnectorError(f"Unhandled Discord action {action!r}")


__all__ = ["DISCORD_ACTIONS", "MAX_MESSAGE_LENGTH", "DiscordConnector", "chunk_message"]
