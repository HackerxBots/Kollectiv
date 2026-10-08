"""The gateway's tool catalogue: everything a client can call, namespaced.

The catalogue is built from things Kollektiv already has — the orchestrator's
operations, the connector registry's actions and a few gateway-native reads —
and gives each one a **namespaced name** so two services can never collide:

```
projects.list                projects.create            projects.run
projects.status              projects.replan            projects.handoff
projects.files               projects.file_url          projects.upload_file
storage.status               agents.status              sync.run
connectors.list              connectors.probe
connectors.telegram.send_message
connectors.discord.send_message
connectors.slack.post_message
connectors.linear.create_issue
connectors.whatsapp.send_message
gateway.health               gateway.audit              gateway.tools
```

Every entry carries the metadata a policy needs: the namespace, a one-line
description, whether it changes anything outside Kollektiv (``dangerous``) and,
for connector actions, the accepted parameter names straight from the
connector's own declaration. That is what lets ``GET /toolkits`` be a real
discovery surface for a model, and lets a policy refuse ``*.delete`` without
anyone maintaining a second list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from config.settings import Settings, get_settings
from src.connectors.base import ConnectorRegistry
from src.utils.logger import get_logger

LOGGER = get_logger(__name__)

#: Handler signature: takes the call parameters, returns something JSON-able.
Handler = Callable[[Dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    """One callable tool in the gateway catalogue.

    Attributes:
        name: Namespaced name, e.g. ``connectors.telegram.send_message``.
        namespace: First segment of :attr:`name`.
        description: One line a model or an operator reads.
        params: Parameter name -> short description (empty for native tools).
        dangerous: True when the tool changes something outside Kollektiv.
        handler: Coroutine taking the call parameters.
        source: ``orchestrator``, ``connector`` or ``gateway``.
    """

    name: str
    namespace: str
    description: str
    handler: Handler
    params: Dict[str, str] = field(default_factory=dict)
    dangerous: bool = False
    source: str = "orchestrator"

    def to_dict(self) -> Dict[str, Any]:
        """Return the catalogue entry as JSON."""
        return {
            "tool": self.name,
            "namespace": self.namespace,
            "description": self.description,
            "params": dict(self.params),
            "dangerous": self.dangerous,
            "source": self.source,
        }


def _require(params: Dict[str, Any], key: str) -> Any:
    """Return ``params[key]`` or raise a ValueError naming what is missing.

    Args:
        params: The call parameters.
        key: The required key.

    Returns:
        The value.

    Raises:
        ValueError: When the key is missing or empty.
    """
    value = params.get(key)
    if value in (None, ""):
        raise ValueError(f"missing required parameter {key!r}")
    return value


def build_catalogue(
    orchestrator: Any,
    registry: Optional[ConnectorRegistry] = None,
    settings: Optional[Settings] = None,
) -> Dict[str, Tool]:
    """Assemble every tool the gateway exposes.

    Args:
        orchestrator: A started :class:`~src.orchestrator.app.Orchestrator`.
        registry: Optional connector registry; one is built from settings when
            omitted so the gateway still works when it is used standalone.
        settings: Optional settings override.

    Returns:
        Mapping from namespaced tool name to :class:`Tool`. Order is preserved
        (dicts keep insertion order), which is the order ``GET /toolkits`` shows.
    """
    resolved = settings or get_settings()
    connectors = registry if registry is not None else ConnectorRegistry.from_settings(resolved)
    catalogue: Dict[str, Tool] = {}

    def add(tool: Tool) -> None:
        """Register one tool, warning on a name collision instead of losing it."""
        if tool.name in catalogue:  # pragma: no cover - defensive
            LOGGER.warning("Tool %s is declared twice; the first definition wins", tool.name)
            return
        catalogue[tool.name] = tool

    # -- projects ------------------------------------------------------
    async def projects_list(_: Dict[str, Any]) -> Any:
        return await orchestrator.list_projects()

    async def projects_create(params: Dict[str, Any]) -> Any:
        return await orchestrator.create_project(
            str(params.get("name") or ""),
            str(_require(params, "description")),
            int(params.get("n_agents") or resolved.DEFAULT_AGENT_COUNT),
        )

    async def projects_run(params: Dict[str, Any]) -> Any:
        return await orchestrator.run_project(
            str(_require(params, "project_id")),
            max_concurrency=params.get("max_concurrency"),
        )

    async def projects_status(params: Dict[str, Any]) -> Any:
        return await orchestrator.get_project_status(str(_require(params, "project_id")))

    async def projects_replan(params: Dict[str, Any]) -> Any:
        return await orchestrator.replan_project(
            str(_require(params, "project_id")), dispatch=bool(params.get("dispatch", False))
        )

    async def projects_handoff(params: Dict[str, Any]) -> Any:
        return await orchestrator.get_handoff(str(_require(params, "project_id")), write=bool(params.get("write", True)))

    async def projects_files(params: Dict[str, Any]) -> Any:
        return await orchestrator.get_project_files(str(_require(params, "project_id")))

    async def projects_upload(params: Dict[str, Any]) -> Any:
        return await orchestrator.upload_project_file(
            str(_require(params, "project_id")), str(_require(params, "file_path"))
        )

    add(Tool("projects.list", "projects", "List every project with its status.", projects_list))
    add(
        Tool(
            "projects.create",
            "projects",
            "Create a project from a brief and generate its plan.",
            projects_create,
            params={"name": "Optional project name.", "description": "What to build.", "n_agents": "Worker count."},
            dangerous=True,
        )
    )
    add(
        Tool(
            "projects.run",
            "projects",
            "Dispatch a project's plan to the worker pool.",
            projects_run,
            params={"project_id": "Project id.", "max_concurrency": "Cap on simultaneous agents."},
            dangerous=True,
        )
    )
    add(
        Tool(
            "projects.status",
            "projects",
            "Read the shared project state document.",
            projects_status,
            params={"project_id": "Project id."},
        )
    )
    add(
        Tool(
            "projects.replan",
            "projects",
            "Ask the brain for a corrective plan.",
            projects_replan,
            params={"project_id": "Project id.", "dispatch": "Also run the new tasks."},
            dangerous=True,
        )
    )
    add(
        Tool(
            "projects.handoff",
            "projects",
            "Return the resume briefing (done, next, blockers).",
            projects_handoff,
            params={"project_id": "Project id.", "write": "Also write HANDOFF.md."},
        )
    )
    add(
        Tool(
            "projects.files",
            "projects",
            "List the artifacts stored for a project.",
            projects_files,
            params={"project_id": "Project id."},
        )
    )
    add(
        Tool(
            "projects.upload_file",
            "projects",
            "Upload a local file into the project's shared storage.",
            projects_upload,
            params={"project_id": "Project id.", "file_path": "Local path to upload."},
            dangerous=True,
        )
    )

    # -- storage, agents, sync ----------------------------------------
    async def storage_status(_: Dict[str, Any]) -> Any:
        return await orchestrator.get_storage_status()

    async def agents_status(params: Dict[str, Any]) -> Any:
        return await orchestrator.get_agents_status(probe=bool(params.get("probe", False)))

    async def sync_run(_: Dict[str, Any]) -> Any:
        return await orchestrator.trigger_sync()

    add(Tool("storage.status", "storage", "Pooled storage quota and per-account health.", storage_status))
    add(
        Tool(
            "agents.status",
            "agents",
            "Worker pool snapshot (busy/idle, done, failures).",
            agents_status,
            params={"probe": "Also ping each worker."},
        )
    )
    add(Tool("sync.run", "sync", "Run the GitHub → storage → agents sync pass.", sync_run, dangerous=True))

    # -- connectors ----------------------------------------------------
    async def connectors_list(_: Dict[str, Any]) -> Any:
        return connectors.summary() if hasattr(connectors, "summary") else {"count": 0}

    async def connectors_probe(params: Dict[str, Any]) -> Any:
        name = str(params["connector"]) if params.get("connector") else ""
        if name:
            return await connectors.probe(name)
        return await connectors.probe_all()

    add(
        Tool(
            "connectors.list",
            "connectors",
            "List services, what is configured and the actions each exposes.",
            connectors_list,
            params={"connector": "Optional: probe only this one."},
        )
    )
    add(
        Tool(
            "connectors.probe",
            "connectors",
            "Check reachability of one connector, or all of them.",
            connectors_probe,
            params={"connector": "Optional connector name; omit to probe all."},
        )
    )

    # One tool per connector action: `connectors.telegram.send_message` and so on.
    # ``names``/``get`` are the registry's public surface; reaching into its
    # private dict silently produced an empty connector namespace once already.
    for connector_name in connectors.names:
        connector = connectors.get(connector_name)
        for action in connector.actions():
            tool_name = f"connectors.{connector.name}.{action.name}"

            def make(connector_name: str = connector.name, action_name: str = action.name) -> Handler:
                """Bind one connector action into a handler (late-binding safe)."""

                async def handler(params: Dict[str, Any]) -> Any:
                    return await connectors.call(
                        connector_name,
                        action_name,
                        {key: value for key, value in params.items() if key != "confirm"},
                        confirm=bool(params.get("confirm", False)),
                    )

                return handler

            add(
                Tool(
                    name=tool_name,
                    namespace="connectors",
                    description=f"{connector.name}: {action.description}",
                    handler=make(),
                    params=dict(action.params),
                    dangerous=action.dangerous,
                    source="connector",
                )
            )

    # -- gateway native ------------------------------------------------
    async def gateway_health(_: Dict[str, Any]) -> Any:
        return await orchestrator.health()

    async def gateway_tools(_: Dict[str, Any]) -> Any:
        return [tool.to_dict() for tool in catalogue.values()]

    async def gateway_audit(params: Dict[str, Any]) -> Any:
        from src.gateway.audit import GatewayAudit

        return await GatewayAudit(resolved).recent(
            limit=int(params.get("limit") or resolved.GATEWAY_AUDIT_LIMIT),
            client=str(params.get("client") or "") or None,
        )

    add(Tool("gateway.health", "gateway", "The orchestrator's health report.", gateway_health))
    add(Tool("gateway.tools", "gateway", "The catalogue you are reading right now.", gateway_tools))
    add(
        Tool(
            "gateway.audit",
            "gateway",
            "Recent gateway calls (local audit log, no arguments recorded).",
            gateway_audit,
            params={"limit": "How many rows.", "client": "Filter by client name."},
        )
    )

    LOGGER.debug("Gateway catalogue: %s tools across %s namespaces", len(catalogue), len(namespaces(catalogue)))
    return catalogue


def namespaces(catalogue: Dict[str, Tool]) -> List[str]:
    """Return the distinct namespaces in a catalogue, in first-seen order.

    Args:
        catalogue: The tool catalogue.

    Returns:
        Namespace names, e.g. ``["projects", "connectors", "gateway"]``.
    """
    seen: List[str] = []
    for tool in catalogue.values():
        if tool.namespace not in seen:
            seen.append(tool.namespace)
    return seen


def toolkit_view(catalogue: Dict[str, Tool]) -> List[Dict[str, Any]]:
    """Group a catalogue by namespace for ``GET /toolkits``.

    Args:
        catalogue: The tool catalogue.

    Returns:
        ``[{"namespace": "projects", "count": 9, "tools": [...]}, ...]``.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for tool in catalogue.values():
        grouped.setdefault(tool.namespace, []).append(tool.to_dict())
    return [
        {"namespace": name, "count": len(tools), "tools": tools}
        for name, tools in grouped.items()
    ]


__all__ = ["Handler", "Tool", "build_catalogue", "namespaces", "toolkit_view"]
