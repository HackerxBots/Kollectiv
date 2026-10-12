"""Connectors: the services Kollektiv can reach beyond its own stack.

``ConnectorRegistry.from_settings()`` builds every connector that ships with
Kollektiv -- GitHub, Google Workspace (Gmail, Calendar, Drive), Notion, Linear,
Telegram, Discord, Slack, WhatsApp, outbound webhooks and any
``CUSTOM_CONNECTORS`` REST entry -- and exposes them as one catalogue of actions
for the brain, the HTTP API, the MCP server, the gateway and the CLI.

Every connector is registered whether or not it is configured: ``GET
/connectors`` is meant to tell you which ones are one environment variable away
from working, so an unconfigured connector is information, not an error.

Importing this package is cheap: the concrete connectors are imported inside
:meth:`ConnectorRegistry.from_settings` so ``from src.connectors import
ConnectorRegistry`` does not pull in httpx-heavy modules.
"""

from src.connectors.base import Connector, ConnectorAction, ConnectorRegistry, Payload

__all__ = ["Connector", "ConnectorAction", "ConnectorRegistry", "Payload"]
