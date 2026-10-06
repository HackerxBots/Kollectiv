"""Connector framework: one registry that gives the agents tools across services.

A *connector* is a small, typed adapter for a service the user already uses
(GitHub, Google Workspace, Notion, a webhook endpoint, or any declarative REST
API). Each connector publishes a catalog of **actions** — name, description and
the parameters they accept — so the brain, the HTTP API and the MCP server can
all list and call the same tools without knowing anything about the service.

Design notes:

* Connectors never raise into the orchestrator: a failing service is logged,
  reported in ``/health`` and skipped.
* Credentials are read from the encrypted :class:`~src.utils.token_store.TokenStore`
  first and fall back to environment settings, so a refreshed OAuth token
  survives restarts without ever touching ``.env``.
* Actions marked ``dangerous`` (sending mail, posting comments, creating pages)
  require an explicit ``confirm=True`` from the caller.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config.settings import Settings, get_settings
from src.utils.errors import ConfigurationError, ConnectorError
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)

#: Type alias for the plain-dict payloads connectors exchange with the outside.
Payload = Dict[str, Any]


@dataclass(frozen=True)
class ConnectorAction:
    """A single callable action exposed by a connector.

    Args:
        name: Machine name used by callers (``snake_case``).
        description: One line the brain/LLM reads when choosing a tool.
        params: Parameter name -> short description. Order is preserved.
        dangerous: True when the action changes something outside Kollektiv
            (sends a message, creates a page, comments on a PR).
    """

    name: str
    description: str
    params: Dict[str, str] = field(default_factory=dict)
    dangerous: bool = False

    def to_dict(self) -> Payload:
        """Return a JSON-serialisable view of the action."""
        return {
            "name": self.name,
            "description": self.description,
            "params": dict(self.params),
            "dangerous": self.dangerous,
        }


class Connector(ABC):
    """Base class for every service adapter.

    Args:
        settings: Optional settings override.
        token_store: Optional encrypted token store (anything exposing
            ``get_token(service, account)``/``save_token``); when omitted the
            connector lazily builds the real one.
        client: Pre-built :class:`httpx.AsyncClient` (used in tests); when
            omitted the connector creates and owns one.
    """

    #: Registry key, also used as the token-store service name.
    name: str = "connector"
    #: Coarse grouping for the UI/README ("code", "email", "productivity", …).
    category: str = "other"
    #: One-line description of the service.
    description: str = ""
    #: Environment variables that configure this connector (documentation only).
    required_env: tuple[str, ...] = ()

    def __init__(
        self,
        settings: Optional[Settings] = None,
        token_store: Any = None,
        client: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._token_store = token_store
        self._client = client
        self._owns_client = client is None
        self._token_expiry: float = 0.0
        self._access_token_cache: str = ""
        self._token_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Contract
    # ------------------------------------------------------------------
    @abstractmethod
    def actions(self) -> List[ConnectorAction]:
        """Return every action this connector exposes."""

    @abstractmethod
    async def call(self, action: str, params: Payload) -> Any:
        """Perform ``action`` with ``params`` and return a JSON-able result."""

    @property
    def is_configured(self) -> bool:
        """True when the connector has everything it needs to run."""
        return True

    def detail(self) -> str:
        """Short human-readable status line (why it is/ isn't usable)."""
        if self.is_configured:
            return "ready"
        missing = ", ".join(self.required_env) or "credentials"
        return f"not configured ({missing})"

    #: A safe, cheap action used by ``kollektiv connectors --probe``.
    probe_action: str = ""
    #: Parameters for the probe action.
    probe_params: Payload = {}

    async def probe(self) -> Payload:
        """Run the read-only probe action and report latency and outcome.

        Returns:
            ``{connector, configured, ok, action, seconds, detail, error}``.
        """
        started = time.perf_counter()
        result: Payload = {
            "connector": self.name,
            "configured": self.is_configured,
            "action": self.probe_action or None,
            "ok": False,
        }
        if not self.is_configured:
            result["error"] = self.detail()
            result["seconds"] = round(time.perf_counter() - started, 3)
            return result
        if not self.probe_action:
            result["ok"] = True
            result["detail"] = "no probe defined for this connector"
            result["seconds"] = round(time.perf_counter() - started, 3)
            return result
        try:
            await self.call(self.probe_action, dict(self.probe_params))
            result["ok"] = True
            result["detail"] = "reachable"
        except Exception as exc:  # noqa: BLE001 - a probe reports, it never raises
            result["error"] = str(exc)
            LOGGER.warning("Connector probe %s.%s failed: %s", self.name, self.probe_action, exc)
        result["seconds"] = round(time.perf_counter() - started, 3)
        return result

    def status(self) -> Payload:
        """Return a JSON-serialisable status record."""
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "configured": self.is_configured,
            "detail": self.detail(),
            "actions": [action.name for action in self.actions()],
            "dangerous_actions": [a.name for a in self.actions() if a.dangerous],
        }

    def catalog(self) -> List[Payload]:
        """Return every action with its connector context attached."""
        return [
            {
                "connector": self.name,
                "category": self.category,
                "configured": self.is_configured,
                **action.to_dict(),
            }
            for action in self.actions()
        ]

    def validate(self, action: str, params: Optional[Payload] = None) -> ConnectorAction:
        """Validate an action name and its parameters before anything runs.

        Typos are the most common cause of "the connector is broken" reports,
        so unknown parameters are rejected with the accepted list instead of
        being silently dropped or forwarded.

        Args:
            action: Action name.
            params: Parameters the caller supplied.

        Returns:
            The matched :class:`ConnectorAction`.

        Raises:
            ConnectorError: Unknown action or unexpected parameters.
        """
        spec = self.action(action)
        if not params:
            return spec
        unexpected = [key for key in params if key not in spec.params]
        if unexpected:
            raise ConnectorError(
                f"{self.name}.{action} does not accept {', '.join(sorted(unexpected))}; "
                f"accepted parameters: {', '.join(spec.params) or 'none'}"
            )
        return spec

    def action(self, name: str) -> ConnectorAction:
        """Look up an action by name (raises :class:`ConnectorError`)."""
        for candidate in self.actions():
            if candidate.name == name:
                return candidate
        available = ", ".join(a.name for a in self.actions()) or "none"
        raise ConnectorError(f"Connector {self.name!r} has no action {name!r} (available: {available})")

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------
    def stored_token(self, account: str = "default") -> Optional[Payload]:
        """Return the encrypted token record for this service, if any."""
        if self._token_store is None:
            return None
        try:
            return self._token_store.get_token(self.name, account)
        except Exception as exc:  # noqa: BLE001 - a missing token is the normal case
            LOGGER.debug("No stored token for %s/%s: %s", self.name, account, exc)
            return None

    def save_token(self, token_data: Payload, account: str = "default") -> None:
        """Persist a (refreshed) token in the encrypted store when available."""
        if self._token_store is None:
            return
        try:
            self._token_store.save_token(self.name, account, token_data)
            LOGGER.info("Stored a refreshed %s token for %s", self.name, account)
        except Exception as exc:  # noqa: BLE001 - never fail the request over storage
            LOGGER.error("Could not persist the %s token: %s", self.name, exc)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def close(self) -> None:
        """Close the owned HTTP client (no-op when one was injected)."""
        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:  # noqa: BLE001 - shutdown is best effort
                LOGGER.debug("Closing %s failed: %s", self.name, exc)

    async def __aenter__(self) -> "Connector":
        """Enter an async context (``async with connector``)."""
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        """Leave an async context, closing the client."""
        await self.close()


class ConnectorRegistry:
    """Holds every connector and exposes them as one catalogue of tools.

    Args:
        settings: Optional settings override.
        token_store: Optional encrypted token store shared by the connectors.
    """

    def __init__(self, settings: Optional[Settings] = None, token_store: Any = None) -> None:
        self.settings = settings or get_settings()
        self._token_store = token_store
        self._connectors: Dict[str, Connector] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    @property
    def token_store(self) -> Any:
        """Lazily build the shared encrypted token store."""
        if self._token_store is None:
            from src.utils.token_store import TokenStore

            try:
                self._token_store = TokenStore()
            except ConfigurationError as exc:  # pragma: no cover - no SECRET_KEY
                LOGGER.warning("Token store unavailable for connectors: %s", exc)
                self._token_store = False
        return self._token_store or None

    def register(self, connector: Connector) -> Connector:
        """Add (or replace) a connector and return it."""
        self._connectors[connector.name] = connector
        return connector

    @property
    def names(self) -> List[str]:
        """Registered connector names, sorted."""
        return sorted(self._connectors)

    def get(self, name: str) -> Connector:
        """Return a connector by name (raises :class:`ConnectorError`)."""
        try:
            return self._connectors[name]
        except KeyError:
            raise ConnectorError(
                f"Unknown connector {name!r} (available: {', '.join(self.names) or 'none'})"
            ) from None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def statuses(self) -> List[Payload]:
        """Return one status record per connector."""
        return [self._connectors[name].status() for name in self.names]

    def catalog(self) -> List[Payload]:
        """Return every action of every connector."""
        actions: List[Payload] = []
        for name in self.names:
            actions.extend(self._connectors[name].catalog())
        return actions

    def configured_names(self) -> List[str]:
        """Names of the connectors that are ready to use."""
        return [name for name in self.names if self._connectors[name].is_configured]

    def summary(self) -> Payload:
        """Compact overview for ``/health`` and ``kollektiv check``."""
        return {
            "count": len(self._connectors),
            "configured": self.configured_names(),
            "actions": sum(len(c.actions()) for c in self._connectors.values()),
            "event_webhooks": len(self.settings.event_webhook_urls),
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def call(
        self,
        name: str,
        action: str,
        params: Optional[Payload] = None,
        confirm: bool = False,
    ) -> Any:
        """Call ``action`` on connector ``name``.

        Args:
            name: Connector name.
            action: Action name.
            params: Action parameters (defaults to none).
            confirm: Required ``True`` for actions flagged ``dangerous``.

        Returns:
            The action's JSON-serialisable result.

        Raises:
            ConnectorError: Unknown connector/action, unconfigured connector or
                a dangerous action that was not confirmed.
        """
        connector = self.get(name)
        spec = connector.validate(action, params)
        if not connector.is_configured:
            raise ConnectorError(f"Connector {name!r} is not configured: {connector.detail()}")
        if spec.dangerous and not confirm:
            raise ConnectorError(
                f"Action {name}.{action} changes data outside Kollektiv; pass confirm=True to run it"
            )
        async with self._lock:
            LOGGER.info("Connector call %s.%s", name, action)
            return await connector.call(action, params or {})

    async def probe(self, name: str) -> Payload:
        """Probe one connector (read-only, never raises)."""
        return await self.get(name).probe()

    async def probe_all(self) -> List[Payload]:
        """Probe every connector and return one report per service."""
        return [await self._connectors[name].probe() for name in self.names]

    async def broadcast(self, event: Payload) -> List[Payload]:
        """Send ``event`` to every configured event webhook (never raises)."""
        if not self.settings.event_webhook_urls or "webhook" not in self._connectors:
            return []
        try:
            delivered = await self.call("webhook", "notify", {"event": event})
        except Exception as exc:  # noqa: BLE001 - notifications are best effort
            LOGGER.error("Event broadcast failed: %s", exc)
            return []
        return delivered if isinstance(delivered, list) else []

    async def close(self) -> None:
        """Close every connector, swallowing individual failures."""
        for name in self.names:
            try:
                await self._connectors[name].close()
            except Exception as exc:  # noqa: BLE001 - shutdown is best effort
                LOGGER.debug("Closing connector %s failed: %s", name, exc)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_settings(
        cls, settings: Optional[Settings] = None, token_store: Any = None
    ) -> "ConnectorRegistry":
        """Build the registry described by ``settings``.

        Every connector is always registered (so the UI can show what exists and
        what is missing); ``configured`` tells the caller which ones work.
        """
        from src.connectors.github import GitHubConnector
        from src.connectors.google_workspace import GoogleWorkspaceConnector
        from src.connectors.notion import NotionConnector
        from src.connectors.rest import build_rest_connectors
        from src.connectors.webhook import WebhookConnector

        resolved = settings or get_settings()
        registry = cls(resolved, token_store=token_store)
        store = token_store
        registry.register(GitHubConnector(resolved, token_store=store))
        registry.register(GoogleWorkspaceConnector(resolved, token_store=store))
        registry.register(NotionConnector(resolved, token_store=store))
        registry.register(WebhookConnector(resolved, token_store=store))
        for custom in build_rest_connectors(resolved):
            if custom.name in registry.names:
                LOGGER.warning("CUSTOM_CONNECTORS entry %r shadows a built-in connector", custom.name)
            registry.register(custom)
        LOGGER.debug("Connector registry ready: %s", ", ".join(registry.names))
        return registry


__all__ = ["Connector", "ConnectorAction", "ConnectorRegistry", "Payload"]
