"""The MCP gateway: one URL, per-client tokens, per-client policy.

`python -m src.api.mcp_server` serves *one* client. The gateway serves a team:

* one MCP endpoint (`GATEWAY_MCP_PATH`) that Claude Code, Codex, Cursor, Zed or
  any other MCP client can point at, behind a per-client bearer token;
* one REST surface (`GET /toolkits`, `POST /call`) for scripts, dashboards and
  anything that is not an MCP client;
* named policies (`read-only`, `worker`, `messenger`, `dashboard`, `admin`)
  plus per-tool globs, so "look but do not touch" is a one-word decision;
* a local audit log of tool names, timings and argument *names* — never the
  arguments themselves.

It is optional by design (`GATEWAY_ENABLED=false`): the plain MCP server and the
HTTP API stay first-class, and this gateway is an addition, not a dependency.
The reasoning, including the business half, is in ``docs/monetization.md``.
"""

from __future__ import annotations

from src.gateway.app import create_gateway_app
from src.gateway.audit import GatewayAudit
from src.gateway.auth import ROLES, TOKEN_PREFIX, GatewayAuth
from src.gateway.policy import POLICY_PRESETS, Policy, load_policy_file, resolve_policy
from src.gateway.tools import Tool, build_catalogue, namespaces, toolkit_view

__all__ = [
    "POLICY_PRESETS",
    "ROLES",
    "TOKEN_PREFIX",
    "GatewayAudit",
    "GatewayAuth",
    "Policy",
    "Tool",
    "build_catalogue",
    "create_gateway_app",
    "load_policy_file",
    "namespaces",
    "resolve_policy",
    "toolkit_view",
]
