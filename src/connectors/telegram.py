"""Telegram connector: send messages, files and read updates through a bot.

The Bot API is the friendliest of the chat platforms: no OAuth dance, no
review, no business account. Create a bot with @BotFather, paste the token into
``TELEGRAM_BOT_TOKEN``, then send a message to the bot and read the chat id from
``get_updates`` (or from ``https://api.telegram.org/bot<token>/getUpdates``).

Sending is flagged ``dangerous`` — it changes something outside Kollektiv (a
human's phone buzzes). Reading is not.
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

TELEGRAM_ACTIONS: List[ConnectorAction] = [
    ConnectorAction(
        name="get_me",
        description="Return the bot's own identity (cheap connectivity check).",
        params={},
    ),
    ConnectorAction(
        name="get_updates",
        description="Read recent updates (messages sent to the bot) — also reveals chat ids.",
        params={
            "limit": "Max updates (default 10).",
            "timeout": "Long-poll seconds (default 0, i.e. return immediately).",
        },
    ),
    ConnectorAction(
        name="send_message",
        description="Send a text message to a chat.",
        params={
            "text": "Message text (Markdown is sent as plain text unless parse_mode is set).",
            "chat_id": "Destination chat id; defaults to TELEGRAM_CHAT_ID.",
            "parse_mode": "Optional: MarkdownV2, Markdown or HTML.",
            "disable_notification": "Send silently (true/false).",
        },
        dangerous=True,
    ),
    ConnectorAction(
        name="send_document",
        description="Send a file from a URL or a previously uploaded file id.",
        params={
            "document": "Public URL or Telegram file id.",
            "chat_id": "Destination chat id; defaults to TELEGRAM_CHAT_ID.",
            "caption": "Optional caption.",
        },
        dangerous=True,
    ),
]


class TelegramConnector(Connector):
    """Telegram Bot API access for one bot token."""

    name = "telegram"
    category = "chat"
    description = "Telegram bot: send messages and read updates."
    required_env = ("TELEGRAM_BOT_TOKEN",)

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
                    base_url=self.settings.TELEGRAM_BASE_URL.rstrip("/"),
                    timeout=httpx.Timeout(self.settings.TELEGRAM_REQUEST_TIMEOUT, connect=10.0),
                )
            )

    @property
    def token(self) -> str:
        """Bot token: the encrypted store wins over the environment."""
        stored = self.stored_token() or {}
        return str(stored.get("access_token") or stored.get("token") or self.settings.TELEGRAM_BOT_TOKEN)

    @property
    def is_configured(self) -> bool:
        """True when a bot token is available."""
        return bool(self.token)

    @async_retry(max_retries=3, retry_on=(ConnectorTransientError, httpx.TransportError, httpx.TimeoutException))
    async def _call(self, method: str, payload: Optional[Payload] = None) -> Payload:
        """Call one Bot API method and unwrap the ``{ok, result}`` envelope.

        Args:
            method: Bot API method name, e.g. ``sendMessage``.
            payload: JSON body.

        Returns:
            The ``result`` field of the response.

        Raises:
            AuthenticationError: When Telegram rejects the token.
            ConnectorTransientError: On rate limits and 5xx responses.
            ConnectorError: For every other failure.
        """
        if not self.token:
            raise AuthenticationError("TELEGRAM_BOT_TOKEN is unset; create a bot with @BotFather")
        response = await self._client.post(f"/bot{self.token}/{method}", json=payload or {})
        if response.status_code == 401:
            raise AuthenticationError("Telegram rejected the bot token")
        if response.status_code == 429:
            raise ConnectorTransientError("Telegram rate limit reached")
        if response.status_code >= 500:
            raise ConnectorTransientError(f"Telegram returned {response.status_code}")
        data = response.json() if response.content else {}
        if not isinstance(data, dict) or not data.get("ok", False):
            description = (data or {}).get("description") if isinstance(data, dict) else response.text[:200]
            raise ConnectorError(f"Telegram {method} failed: {description or response.status_code}")
        return data.get("result") or {}

    #: ``getMe`` is a single cheap request and proves the token works.
    probe_action = "get_me"
    probe_params: Payload = {}

    def actions(self) -> List[ConnectorAction]:
        """Return the Telegram actions."""
        return TELEGRAM_ACTIONS

    async def call(self, action: str, params: Payload) -> Any:
        """Perform a Telegram action.

        Args:
            action: Action name from :data:`TELEGRAM_ACTIONS`.
            params: Action parameters.

        Returns:
            The Bot API result, trimmed to what a caller needs.

        Raises:
            ConnectorError: For unknown actions or missing required parameters.
        """
        if action == "get_me":
            me = await self._call("getMe")
            return {
                "id": me.get("id"),
                "username": me.get("username"),
                "first_name": me.get("first_name"),
                "can_join_groups": me.get("can_join_groups"),
            }
        if action == "get_updates":
            updates = await self._call(
                "getUpdates",
                {
                    "limit": max(1, min(int(params.get("limit") or 10), 100)),
                    "timeout": max(0, int(params.get("timeout") or 0)),
                },
            )
            return {"count": len(updates or []), "updates": updates}
        if action == "send_message":
            text = str(params.get("text") or "").strip()
            if not text:
                raise ConnectorError("send_message needs some text")
            body: Payload = {
                "chat_id": str(params.get("chat_id") or self.settings.TELEGRAM_CHAT_ID),
                "text": text,
            }
            if not body["chat_id"]:
                raise ConnectorError("send_message needs a chat_id (or set TELEGRAM_CHAT_ID)")
            if params.get("parse_mode"):
                body["parse_mode"] = str(params["parse_mode"])
            if params.get("disable_notification") is not None:
                body["disable_notification"] = bool(params["disable_notification"])
            message = await self._call("sendMessage", body)
            return {
                "sent": True,
                "message_id": message.get("message_id"),
                "chat": (message.get("chat") or {}).get("id"),
            }
        if action == "send_document":
            document = str(params.get("document") or "").strip()
            if not document:
                raise ConnectorError("send_document needs a document URL or file id")
            body = {
                "chat_id": str(params.get("chat_id") or self.settings.TELEGRAM_CHAT_ID),
                "document": document,
            }
            if not body["chat_id"]:
                raise ConnectorError("send_document needs a chat_id (or set TELEGRAM_CHAT_ID)")
            if params.get("caption"):
                body["caption"] = str(params["caption"])
            message = await self._call("sendDocument", body)
            return {"sent": True, "message_id": message.get("message_id")}
        raise ConnectorError(f"Unhandled Telegram action {action!r}")


__all__ = ["TELEGRAM_ACTIONS", "TelegramConnector"]
