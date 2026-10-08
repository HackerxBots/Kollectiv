"""Webhook connector: push Kollektiv events into anything that speaks HTTP.

``EVENT_WEBHOOKS`` is a comma separated list of URLs (Slack/Discord incoming
webhooks, an n8n/Activepieces/Zapier catch hook, your own service). The
orchestrator broadcasts a small JSON event after each run, and agents can call
``notify`` themselves with free-form text.
"""

from __future__ import annotations

from typing import Any, List, Optional

import httpx

from config.settings import Settings
from src.connectors.base import Connector, ConnectorAction, Payload
from src.utils.errors import ConfigurationError, ConnectorError, ConnectorTransientError
from src.utils.logger import get_logger
from src.utils.retry import async_retry

LOGGER = get_logger(__name__)

WEBHOOK_ACTIONS: List[ConnectorAction] = [
    ConnectorAction(
        name="notify",
        description="POST a text/JSON event to every configured webhook URL.",
        params={
            "text": "Human readable message.",
            "url": "Send to one specific URL instead of all (optional).",
            "event": "Structured event payload (optional).",
        },
    ),
    ConnectorAction(
        name="list_targets",
        description="List the configured webhook URLs.",
        params={},
    ),
]


class WebhookConnector(Connector):
    """Fan-out of JSON events to arbitrary HTTP endpoints."""

    name = "webhook"
    category = "automation"
    description = "Outbound webhooks: notify Slack/Discord/n8n/Zapier or your own service."
    required_env = ("EVENT_WEBHOOKS",)

    def __init__(
        self,
        settings: Optional[Settings] = None,
        token_store: Any = None,
        client: Optional[httpx.AsyncClient] = None,
        urls: Optional[List[str]] = None,
    ) -> None:
        super().__init__(settings, token_store=token_store, client=client)
        self._urls = urls if urls is not None else self.settings.event_webhook_urls
        if client is None:
            from src.utils.net import async_client_kwargs

            self._client = httpx.AsyncClient(**async_client_kwargs(self.settings, timeout=httpx.Timeout(20.0, connect=10.0)))

    @property
    def urls(self) -> List[str]:
        """The targets this connector will POST to."""
        return list(self._urls)

    @property
    def is_configured(self) -> bool:
        """True when at least one webhook URL is configured."""
        return bool(self._urls)

    def detail(self) -> str:
        """Describe how many targets are configured."""
        if not self._urls:
            return "not configured (EVENT_WEBHOOKS)"
        return f"{len(self._urls)} target(s): {', '.join(self._urls[:3])}{'…' if len(self._urls) > 3 else ''}"

    #: Listing targets is local; sending a test event is opt-in per probe run.
    probe_action = "list_targets"
    probe_params: Payload = {}

    def actions(self) -> List[ConnectorAction]:
        """Return the webhook actions."""
        return WEBHOOK_ACTIONS

    @async_retry(
        max_retries=3,
        retry_on=(ConnectorTransientError, httpx.TransportError, httpx.TimeoutException),
    )
    async def _post(self, url: str, payload: Payload) -> Payload:
        """POST one payload, translating transport level failures."""
        response = await self._client.post(url, json=payload)
        if response.status_code >= 500 or response.status_code == 429:
            raise ConnectorTransientError(f"Webhook {url} returned {response.status_code}")
        if response.status_code >= 400:
            raise ConnectorError(f"Webhook {url} rejected the payload ({response.status_code})")
        return {"url": url, "status": response.status_code}

    async def call(self, action: str, params: Payload) -> Any:
        """Send a notification or list the targets."""
        if action == "list_targets":
            return {"urls": self.urls}
        if action != "notify":
            raise ConnectorError(f"Unhandled webhook action {action!r}")
        targets = [str(params["url"])] if params.get("url") else self.urls
        if not targets:
            raise ConfigurationError("No webhook URLs configured; set EVENT_WEBHOOKS")
        payload: Payload = {
            "source": "kollektiv",
            "text": str(params.get("text") or ""),
            "event": params.get("event") or {},
        }
        results: List[Payload] = []
        for url in targets:
            try:
                results.append(await self._post(url, payload))
            except Exception as exc:  # noqa: BLE001 - one dead target must not stop the others
                LOGGER.error("Webhook delivery to %s failed: %s", url, exc)
                results.append({"url": url, "error": str(exc)})
        delivered = sum(1 for item in results if item.get("status"))
        LOGGER.info("Webhook notify: %s/%s target(s) delivered", delivered, len(targets))
        return results


__all__ = ["WEBHOOK_ACTIONS", "WebhookConnector"]
