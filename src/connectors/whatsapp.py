"""WhatsApp connector: two routes, one honest warning.

Unlike Telegram, Discord and Slack, WhatsApp has **no bot API for personal
accounts**. There are exactly two ways in, and they are not equivalent:

1. **Official Cloud API** (``WA_BACKEND=cloud``) — Meta's Business platform:
   a verified business number, a permanent token, message templates for
   business-initiated conversations, and a free tier for replies inside the
   24-hour customer service window. Sanctioned, stable, and the only route we
   would put in front of a paying customer.

2. **Linked-device bridge** (``WA_BACKEND=bridge``) — what OpenClaw does: a
   local process that speaks the WhatsApp Web multi-device protocol (the
   Node libraries are `@whiskeysockets/baileys`, which OpenClaw's plugin uses,
   or `whatsapp-web.js`, which other guides show), pairs by QR code, and exposes
   send/receive over HTTP. It works with a personal number, needs no Meta
   account, and **violates Meta's terms**: automating a personal account can get
   the number banned, and QA is at the mercy of protocol changes.

Because of that, bridge mode refuses to run unless ``WA_ALLOW_UNOFFICIAL=true``
is set explicitly — one line in ``.env`` that says "I know, it is my number, I
accept the risk". The connector also keeps the bridge's surface small (status +
send) so a protocol change breaks one method rather than the agent's whole tool
belt.

Bridge contract (implement it with either Node library, it is ~40 lines)::

    GET  {WA_BRIDGE_URL}{WA_BRIDGE_STATUS_PATH}   -> {"connected": true, "me": "15551234567"}
    POST {WA_BRIDGE_URL}{WA_BRIDGE_SEND_PATH}     -> {"sent": true, "id": "3EB0..."}
         body: {"to": "15551234567", "text": "hello"}
         header: Authorization: Bearer <WA_BRIDGE_TOKEN> when the token is set

TODO: wppconnect-server and wa-automate expose their own paths and payloads
(``/api/{session}/send-message``, ``/sendText``). Point ``WA_BRIDGE_SEND_PATH``
at those and adapt the body in :meth:`WhatsAppConnector._bridge_send` rather than
forking this connector.
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

#: WhatsApp text messages are capped at 4096 characters.
MAX_MESSAGE_LENGTH = 4096

WHATSAPP_ACTIONS: List[ConnectorAction] = [
    ConnectorAction(
        name="status",
        description="Report which WhatsApp route is configured and whether it is connected.",
        params={},
    ),
    ConnectorAction(
        name="send_message",
        description="Send a text message (Cloud API or the linked-device bridge).",
        params={
            "to": "Destination number in international format (digits only); defaults to WA_DEFAULT_TO.",
            "text": "Message text.",
            "template": "Cloud API only: send a template instead of free text.",
            "language": "Cloud API template language code (default 'en_US').",
        },
        dangerous=True,
    ),
]


class WhatsAppConnector(Connector):
    """WhatsApp through Meta's Cloud API or a local linked-device bridge."""

    name = "whatsapp"
    category = "chat"
    description = "WhatsApp: official Cloud API, or a local linked-device bridge (unofficial)."
    required_env = ("WA_BACKEND",)

    #: ``status`` never sends anything, so it is safe to probe with.
    probe_action = "status"
    probe_params: Payload = {}

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
                    timeout=httpx.Timeout(self.settings.WA_REQUEST_TIMEOUT, connect=10.0),
                )
            )

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @property
    def backend(self) -> str:
        """Return the resolved backend: ``cloud``, ``bridge`` or ``""``."""
        configured = str(self.settings.WA_BACKEND or "").strip().lower()
        if configured in {"cloud", "bridge"}:
            return configured
        # No explicit choice: infer from whatever credentials exist.
        if self.settings.WA_CLOUD_TOKEN and self.settings.WA_PHONE_NUMBER_ID:
            return "cloud"
        if self.settings.WA_BRIDGE_URL:
            return "bridge"
        return ""

    @property
    def cloud_token(self) -> str:
        """Cloud API token: the encrypted store wins over the environment."""
        stored = self.stored_token(account="cloud") or {}
        return str(stored.get("access_token") or stored.get("token") or self.settings.WA_CLOUD_TOKEN)

    @property
    def bridge_token(self) -> str:
        """Bridge bearer token, when the bridge requires one."""
        stored = self.stored_token(account="bridge") or {}
        return str(stored.get("access_token") or stored.get("token") or self.settings.WA_BRIDGE_TOKEN)

    @property
    def is_configured(self) -> bool:
        """True when the selected route has everything it needs."""
        if self.backend == "cloud":
            return bool(self.cloud_token and self.settings.WA_PHONE_NUMBER_ID)
        if self.backend == "bridge":
            return bool(self.settings.WA_BRIDGE_URL and self.settings.WA_ALLOW_UNOFFICIAL)
        return False

    def detail(self) -> str:
        """Explain the current state, including the unofficial-route warning."""
        backend = self.backend
        if backend == "cloud":
            if not self.cloud_token or not self.settings.WA_PHONE_NUMBER_ID:
                return "cloud backend selected but WA_CLOUD_TOKEN / WA_PHONE_NUMBER_ID are incomplete"
            return "ready (official Cloud API)"
        if backend == "bridge":
            if not self.settings.WA_BRIDGE_URL:
                return "bridge backend selected but WA_BRIDGE_URL is unset"
            if not self.settings.WA_ALLOW_UNOFFICIAL:
                return (
                    "bridge configured but disabled: automating a personal account breaks Meta's terms "
                    "and can get the number banned. Set WA_ALLOW_UNOFFICIAL=true only if that is your "
                    "number and your risk, or use the official route (WA_BACKEND=cloud)."
                )
            return "ready (unofficial linked-device bridge — expect occasional breakage)"
        return "not configured (set WA_BACKEND=cloud with Cloud API credentials, or WA_BACKEND=bridge with WA_BRIDGE_URL)"

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    @async_retry(max_retries=3, retry_on=(ConnectorTransientError, httpx.TransportError, httpx.TimeoutException))
    async def _cloud_post(self, path: str, body: Payload, context: str) -> Payload:
        """POST to the Graph API and translate its errors."""
        if not self.cloud_token:
            raise AuthenticationError("WA_CLOUD_TOKEN is unset; create a permanent token in the Meta app dashboard")
        response = await self._client.post(
            f"{self.settings.WA_GRAPH_URL.rstrip('/')}/{self.settings.WA_GRAPH_VERSION}{path}",
            json=body,
            headers={"Authorization": f"Bearer {self.cloud_token}", "Content-Type": "application/json"},
        )
        if response.status_code in (401, 403):
            raise AuthenticationError(f"WhatsApp Cloud API rejected the token during {context}")
        if response.status_code == 429:
            raise ConnectorTransientError("WhatsApp Cloud API rate limit reached")
        if response.status_code >= 500:
            raise ConnectorTransientError(f"WhatsApp Cloud API returned {response.status_code}")
        if response.status_code >= 400:
            raise ConnectorError(f"WhatsApp Cloud API {context} failed: {response.text[:300]}")
        return response.json() if response.content else {}

    @async_retry(max_retries=3, retry_on=(ConnectorTransientError, httpx.TransportError, httpx.TimeoutException))
    async def _bridge_call(self, method: str, path: str, body: Optional[Payload] = None) -> Payload:
        """Call the local bridge and translate its errors."""
        url = f"{self.settings.WA_BRIDGE_URL.rstrip('/')}{path}"
        headers = {"Content-Type": "application/json"}
        if self.bridge_token:
            headers["Authorization"] = f"Bearer {self.bridge_token}"
        response = await self._client.request(method, url, json=body, headers=headers)
        if response.status_code in (401, 403):
            raise AuthenticationError("the WhatsApp bridge rejected WA_BRIDGE_TOKEN")
        if response.status_code >= 500:
            raise ConnectorTransientError(f"the WhatsApp bridge returned {response.status_code}")
        if response.status_code >= 400:
            raise ConnectorError(f"the WhatsApp bridge returned {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else {}

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def actions(self) -> List[ConnectorAction]:
        """Return the WhatsApp actions."""
        return WHATSAPP_ACTIONS

    async def call(self, action: str, params: Payload) -> Any:
        """Perform a WhatsApp action.

        Args:
            action: ``status`` or ``send_message``.
            params: Action parameters.

        Returns:
            A JSON-friendly result.

        Raises:
            ConnectorError: For unknown actions, missing parameters, or a
                configured-but-disabled bridge.
        """
        if action == "status":
            backend = self.backend
            if backend == "bridge":
                if not self.settings.WA_ALLOW_UNOFFICIAL:
                    return {"backend": backend, "configured": False, "detail": self.detail()}
                data = await self._bridge_call("GET", self.settings.WA_BRIDGE_STATUS_PATH)
                return {
                    "backend": backend,
                    "configured": True,
                    "connected": bool(data.get("connected", True)),
                    "me": data.get("me") or data.get("user"),
                    "warning": "unofficial linked-device bridge: Meta may flag or ban the number",
                }
            if backend == "cloud":
                return {
                    "backend": backend,
                    "configured": self.is_configured,
                    "phone_number_id": self.settings.WA_PHONE_NUMBER_ID or None,
                    "detail": self.detail(),
                }
            return {"backend": "", "configured": False, "detail": self.detail()}

        if action == "send_message":
            if not self.is_configured:
                raise ConnectorError(f"WhatsApp is not usable: {self.detail()}")
            to = str(params.get("to") or self.settings.WA_DEFAULT_TO).strip().lstrip("+")
            if not to:
                raise ConnectorError("send_message needs a 'to' number (or set WA_DEFAULT_TO)")
            text = str(params.get("text") or "").strip()
            if not text and not params.get("template"):
                raise ConnectorError("send_message needs some text (or a template name)")

            chunks = _chunk(text, MAX_MESSAGE_LENGTH)
            if self.backend == "cloud":
                ids: List[str] = []
                for chunk in chunks:
                    body: Payload = {"messaging_product": "whatsapp", "to": to}
                    if params.get("template"):
                        body["type"] = "template"
                        body["template"] = {
                            "name": str(params["template"]),
                            "language": {"code": str(params.get("language") or "en_US")},
                        }
                    else:
                        body["type"] = "text"
                        body["text"] = {"preview_url": False, "body": chunk}
                    data = await self._cloud_post(
                        f"/{self.settings.WA_PHONE_NUMBER_ID}/messages", body, "send_message"
                    )
                    messages = data.get("messages") or [{}]
                    ids.append(str(messages[0].get("id")))
                return {
                    "sent": True,
                    "backend": "cloud",
                    "to": to,
                    "messages": len(ids),
                    "message_id": ids[0] if ids else None,
                }

            data = await self._bridge_call(
                "POST",
                self.settings.WA_BRIDGE_SEND_PATH,
                {"to": to, "text": "\n".join(chunks) if len(chunks) > 1 else (chunks[0] if chunks else "")},
            )
            return {
                "sent": bool(data.get("sent", True)),
                "backend": "bridge",
                "to": to,
                "messages": 1 if chunks else 0,
                "message_id": data.get("id") or data.get("messageId"),
                "warning": "sent through an unofficial linked-device bridge",
            }
        raise ConnectorError(f"Unhandled WhatsApp action {action!r}")


def _chunk(text: str, limit: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """Split ``text`` into pieces no longer than ``limit`` characters.

    WhatsApp's own limit is 4096 characters; truncating a long message would
    quietly lose the end of it, so it is split at newlines where possible.

    Args:
        text: The message body.
        limit: Maximum characters per piece.

    Returns:
        The pieces, in order (empty list for empty or whitespace-only input).
    """
    body = (text or "").strip()
    if not body:
        return []
    if len(body) <= limit:
        return [body]
    chunks: List[str] = []
    remaining = body
    while remaining:
        piece = remaining[:limit]
        if len(remaining) > limit:
            split_at = piece.rfind("\n")
            if split_at < limit // 2:
                split_at = limit
            piece = remaining[:split_at]
        chunks.append(piece.strip())
        remaining = remaining[split_at:]
    return [chunk for chunk in chunks if chunk]


__all__ = ["MAX_MESSAGE_LENGTH", "WHATSAPP_ACTIONS", "WhatsAppConnector"]
