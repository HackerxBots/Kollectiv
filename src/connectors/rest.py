"""Declarative REST connectors: any JSON API becomes a tool without writing code.

``CUSTOM_CONNECTORS`` is a JSON list; each entry describes a service, its auth
and one action per endpoint:

```json
[
  {
    "name": "slack",
    "category": "chat",
    "description": "Post to Slack",
    "base_url": "https://slack.com/api",
    "auth": "bearer",
    "token": "xoxb-…",
    "actions": [
      {
        "name": "post_message",
        "method": "POST",
        "path": "/chat.postMessage",
        "description": "Send a message to a channel",
        "params": {"channel": "Channel id", "text": "Message body"}
      },
      {
        "name": "channel_history",
        "method": "GET",
        "path": "/conversations.history",
        "params": {"channel": "Channel id", "limit": "How many messages"}
      }
    ]
  }
]
```

``{placeholder}`` segments in ``path`` are filled from the call parameters; the
remaining parameters become the query string (GET/DELETE) or the JSON body
(POST/PUT/PATCH). ``auth`` is one of ``bearer``, ``header`` (uses
``header_name``, default ``Authorization``), ``query`` (uses ``query_name``,
default ``api_key``) or ``none``. Tokens may come from the encrypted token store
(service name = connector name, key ``access_token``) and win over the inline
value.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from config.settings import Settings
from src.connectors.base import Connector, ConnectorAction, Payload
from src.utils.errors import ConfigurationError, ConnectorError, ConnectorTransientError
from src.utils.logger import get_logger
from src.utils.retry import async_retry

LOGGER = get_logger(__name__)

_TIMEOUT = 30.0


def build_rest_connectors(settings: Settings) -> List["RestConnector"]:
    """Build one :class:`RestConnector` per valid ``CUSTOM_CONNECTORS`` entry."""
    connectors: List[RestConnector] = []
    for config in settings.custom_connector_configs:
        try:
            connectors.append(RestConnector(config, settings=settings))
        except ConfigurationError as exc:
            LOGGER.error("Skipping CUSTOM_CONNECTORS entry %r: %s", config.get("name"), exc)
    return connectors


class RestConnector(Connector):
    """A generic JSON API client driven by configuration.

    Args:
        config: One entry of ``CUSTOM_CONNECTORS``.
        settings: Optional settings override.
        token_store: Optional encrypted token store.
        client: Optional pre-built ``httpx.AsyncClient`` (tests).
    """

    category = "custom"

    def __init__(
        self,
        config: Payload,
        settings: Optional[Settings] = None,
        token_store: Any = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        super().__init__(settings, token_store=token_store, client=client)
        self.config = dict(config)
        self.name = str(self.config.get("name") or "").strip()
        self.category = str(self.config.get("category") or "custom")
        self.description = str(self.config.get("description") or f"Custom REST connector {self.name}")
        if not self.name:
            raise ConfigurationError("CUSTOM_CONNECTORS entries need a 'name'")
        self.base_url = str(self.config.get("base_url") or "").rstrip("/")
        if not self.base_url:
            raise ConfigurationError(f"CUSTOM_CONNECTORS entry {self.name!r} needs a 'base_url'")
        self.auth = str(self.config.get("auth") or "none").lower()
        if self.auth not in ("none", "", "bearer", "header", "query"):
            raise ConfigurationError(
                f"CUSTOM_CONNECTORS entry {self.name!r} has unknown auth {self.auth!r} "
                "(use none, bearer, header or query)"
            )
        self._endpoints: Dict[str, Payload] = {}
        self._actions = self._parse_actions(self.config.get("actions"))
        if not self._actions:
            raise ConfigurationError(f"CUSTOM_CONNECTORS entry {self.name!r} needs at least one action")
        if client is None:
            from src.utils.net import async_client_kwargs

            self._client = httpx.AsyncClient(
                **async_client_kwargs(self.settings, base_url=self.base_url, timeout=httpx.Timeout(_TIMEOUT, connect=10.0))
            )

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @property
    def required_env(self) -> tuple[str, ...]:  # type: ignore[override]
        """Environment variable that configures this connector."""
        return ("CUSTOM_CONNECTORS",)

    def _parse_actions(self, raw: Any) -> Dict[str, ConnectorAction]:
        """Turn the ``actions`` list into a name -> action mapping.

        Endpoint details (method, path, fixed body) are kept separately in
        ``self._endpoints`` because :class:`ConnectorAction` is immutable.
        """
        actions: Dict[str, ConnectorAction] = {}
        for entry in raw or []:
            if not isinstance(entry, dict) or not entry.get("name") or not entry.get("path"):
                continue
            name = str(entry["name"])
            actions[name] = ConnectorAction(
                name=name,
                description=str(entry.get("description") or name),
                params={str(k): str(v) for k, v in (entry.get("params") or {}).items()},
                dangerous=bool(entry.get("dangerous", False)),
            )
            self._endpoints[name] = {
                "method": str(entry.get("method") or "GET").upper(),
                "path": str(entry["path"]),
                "body": {str(k): v for k, v in (entry.get("body") or {}).items()},
            }
        return actions

    @property
    def token(self) -> str:
        """Auth token: encrypted store first, then the inline config value."""
        stored = self.stored_token() or {}
        return str(stored.get("access_token") or self.config.get("token") or "")

    @property
    def is_configured(self) -> bool:
        """True when the connector has every credential it asked for."""
        return self.auth == "none" or bool(self.token)

    def actions(self) -> List[ConnectorAction]:
        """Return the configured actions."""
        return list(self._actions.values())

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _auth_headers(self, params: Dict[str, Any]) -> Dict[str, str]:
        """Return auth headers (and pop the query-string key when needed)."""
        if self.auth == "bearer":
            return {"Authorization": f"Bearer {self.token}"}
        if self.auth == "header":
            name = str(self.config.get("header_name") or "Authorization")
            return {name: self.token}
        if self.auth == "query":
            query_name = str(self.config.get("query_name") or "api_key")
            params[query_name] = self.token
            return {}
        if self.auth in ("none", ""):
            return {}
        raise ConfigurationError(f"CUSTOM_CONNECTORS entry {self.name!r} has unknown auth {self.auth!r}")

    @async_retry(
        max_retries=3,
        retry_on=(ConnectorTransientError, httpx.TransportError, httpx.TimeoutException),
    )
    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Perform the HTTP request and decode the JSON (or text) response."""
        response = await self._client.request(method, path, **kwargs)
        if response.status_code >= 500 or response.status_code == 429:
            raise ConnectorTransientError(f"{self.name} returned {response.status_code}")
        if response.status_code >= 400:
            raise ConnectorError(f"{self.name} returned {response.status_code}: {response.text[:200]}")
        if not response.content:
            return {"status": response.status_code}
        try:
            return response.json()
        except ValueError:
            return {"status": response.status_code, "text": response.text[:20_000]}

    async def call(self, action: str, params: Payload) -> Any:
        """Call the configured endpoint for ``action``."""
        if action not in self._actions:
            available = ", ".join(self._actions) or "none"
            raise ConnectorError(f"{self.name} has no action {action!r} (available: {available})")

        endpoint = self._endpoints[action]
        method: str = str(endpoint["method"])
        path: str = str(endpoint["path"])
        body: Dict[str, Any] = dict(endpoint["body"])
        remaining: Dict[str, Any] = {**body, **{k: v for k, v in params.items() if k not in ("confirm",)}}

        # Fill {placeholders} in the path from the parameters.
        for key in list(remaining):
            token = "{" + key + "}"
            if token in path:
                path = path.replace(token, str(remaining.pop(key)))

        headers = self._auth_headers(remaining)
        if method in ("GET", "DELETE", "HEAD"):
            response = await self._request(method, path, params=remaining or None, headers=headers)
        else:
            response = await self._request(method, path, json=remaining or None, headers=headers)
        return {"connector": self.name, "action": action, "result": response}


__all__ = ["RestConnector", "build_rest_connectors"]
