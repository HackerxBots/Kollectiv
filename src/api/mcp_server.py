"""MCP server exposing Kollektiv's tools to AI models.

Run it with::

    python -m src.api.mcp_server
    # or
    python -m src.api.mcp_server --transport streamable-http --port 8001

Tools exposed
-------------
``list_projects``            every known project
``get_project_status``       the shared ``PROJECT_STATE.md`` for one project
``create_project``           create + plan a project
``run_project``              dispatch the plan to the workers
``list_files``               files stored for a project
``upload_file``              archive a local file to TeraBox
``get_agent_pool_status``    worker agent status
``get_storage_status``       TeraBox pool quota
``trigger_sync``             run the GitHub -> TeraBox -> agents sync

The official ``mcp`` Python SDK renamed ``FastMCP`` to ``MCPServer`` in v2, so
:func:`create_server` imports whichever class is available. Both expose the
same decorator API used below.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Optional

from config.settings import Settings, get_settings
from src.orchestrator.app import Orchestrator
from src.utils.logger import configure_logging, get_logger

LOGGER = get_logger(__name__)

SERVER_NAME = "kollektiv"
SERVER_INSTRUCTIONS = (
    "Kollektiv orchestrates a team of AI worker agents. Use create_project to plan work, "
    "run_project to execute it, and get_project_status to follow progress."
)


def _load_server_class() -> Any:
    """Return the MCP server class from whichever SDK version is installed.

    Returns:
        ``mcp.server.mcpserver.MCPServer`` (SDK v2) or
        ``mcp.server.fastmcp.FastMCP`` (SDK v1).

    Raises:
        RuntimeError: When the ``mcp`` package is missing.
    """
    try:
        from mcp.server.mcpserver import MCPServer  # type: ignore[import-not-found]

        return MCPServer
    except ImportError:  # pragma: no cover - depends on the installed SDK
        pass
    try:
        # SDK v1 only; absent (and untyped) when v2 is installed.
        from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found,attr-defined]

        return FastMCP
    except ImportError as exc:  # pragma: no cover - depends on the installed SDK
        raise RuntimeError(
            "The 'mcp' package is required for the MCP server. Install it with: pip install mcp"
        ) from exc


def create_server(orchestrator: Optional[Orchestrator] = None, settings: Optional[Settings] = None) -> Any:
    """Build the MCP server with every Kollektiv tool registered.

    Args:
        orchestrator: Optional pre-built orchestrator (created on first tool call
            when omitted, so the server starts even before credentials are set).
        settings: Optional settings override.

    Returns:
        A configured MCP server instance.
    """
    resolved = settings or get_settings()
    server_class = _load_server_class()
    try:
        server = server_class(
            name=SERVER_NAME,
            instructions=SERVER_INSTRUCTIONS,
            **({"version": "0.1.0"} if server_class.__name__ == "MCPServer" else {}),
        )
    except TypeError:  # pragma: no cover - older SDK signatures
        server = server_class(SERVER_NAME)

    state: Dict[str, Any] = {"orchestrator": orchestrator}

    async def get_orchestrator() -> Orchestrator:
        """Return the lazily started orchestrator singleton."""
        instance = state.get("orchestrator")
        if instance is None:
            instance = Orchestrator(resolved)
            await instance.start()
            state["orchestrator"] = instance
            LOGGER.info("MCP server initialised the orchestrator")
        return instance

    def _json(payload: Any) -> str:
        """Serialise a tool result as pretty JSON."""
        return json.dumps(payload, default=str, indent=2)

    @server.tool()
    async def list_projects() -> str:
        """List every Kollektiv project with its status."""
        instance = await get_orchestrator()
        projects = await instance.list_projects()
        return _json({"count": len(projects), "projects": projects})

    @server.tool()
    async def get_project_status(project_id: str) -> str:
        """Return the shared project state (tasks, agents, files, history).

        Args:
            project_id: Identifier returned by ``create_project``.
        """
        instance = await get_orchestrator()
        try:
            status = await instance.get_project_status(project_id)
        except KeyError:
            return _json({"error": f"unknown project: {project_id}"})
        return _json(status)

    @server.tool()
    async def create_project(name: str, description: str, n_agents: int = 3) -> str:
        """Create a project and plan it with the LLM brain.

        Args:
            name: Short project name.
            description: What to build.
            n_agents: Number of worker agents to plan for.
        """
        instance = await get_orchestrator()
        try:
            record = await instance.create_project(name, description, n_agents)
        except (ValueError, Exception) as exc:  # noqa: BLE001 - tools report, never raise
            LOGGER.error("create_project failed: %s", exc)
            return _json({"error": str(exc)})
        return _json(
            {
                "project_id": record["project_id"],
                "name": record["name"],
                "n_agents": record["n_agents"],
                "task_count": len(record["plan"].get("tasks", [])),
                "tasks": [
                    {"id": task.get("id"), "title": task.get("title"), "priority": task.get("priority")}
                    for task in record["plan"].get("tasks", [])
                ],
                "status": record["status"],
            }
        )

    @server.tool()
    async def run_project(project_id: str, max_concurrency: int = 0) -> str:
        """Dispatch a project's plan to the worker agents and wait for results.

        Args:
            project_id: Identifier returned by ``create_project``.
            max_concurrency: Optional cap on simultaneous agents (0 = default).
        """
        instance = await get_orchestrator()
        try:
            summary = await instance.run_project(
                project_id, max_concurrency=max_concurrency or None
            )
        except KeyError:
            return _json({"error": f"unknown project: {project_id}"})
        except Exception as exc:  # noqa: BLE001 - tools report, never raise
            LOGGER.error("run_project failed: %s", exc)
            return _json({"error": str(exc)})
        return _json(
            {
                "project_id": summary.get("project_id"),
                "status": summary.get("status"),
                "tasks_dispatched": summary.get("tasks_dispatched"),
                "completed": summary.get("completed"),
                "failed": summary.get("failed"),
                "artifact": summary.get("artifact"),
            }
        )

    @server.tool()
    async def replan_project(project_id: str, dispatch: bool = False) -> str:
        """Replace failed tasks with corrective ones.

        Args:
            project_id: Identifier returned by ``create_project``.
            dispatch: Also run the corrective tasks immediately.
        """
        instance = await get_orchestrator()
        try:
            outcome = await instance.replan_project(project_id, dispatch=dispatch)
        except KeyError:
            return _json({"error": f"unknown project: {project_id}"})
        except Exception as exc:  # noqa: BLE001 - tools report, never raise
            LOGGER.error("replan_project failed: %s", exc)
            return _json({"error": str(exc)})
        return _json({key: value for key, value in outcome.items() if key != "plan"})

    @server.tool()
    async def get_handoff(project_id: str) -> str:
        """Resume briefing for a project: progress, next actions, blockers, history."""
        instance = await get_orchestrator()
        try:
            handoff = await instance.get_handoff(project_id)
        except Exception as exc:  # noqa: BLE001 - report, never crash the MCP server
            return json.dumps({"error": str(exc)}, indent=2)
        return json.dumps({key: value for key, value in handoff.items() if key != "markdown"}, indent=2)

    @server.tool()
    async def list_files(project_id: str) -> str:
        """List the files stored for a project (TeraBox + local index).

        Args:
            project_id: Identifier returned by ``create_project``.
        """
        instance = await get_orchestrator()
        try:
            files = await instance.get_project_files(project_id)
        except Exception as exc:  # noqa: BLE001 - tools report, never raise
            return _json({"error": str(exc)})
        return _json({"project_id": project_id, "count": len(files), "files": files})

    @server.tool()
    async def upload_file(project_id: str, file_path: str) -> str:
        """Archive a local file into the project's TeraBox folder.

        Args:
            project_id: Identifier returned by ``create_project``.
            file_path: Absolute or relative path of a local file.
        """
        instance = await get_orchestrator()
        try:
            result = await instance.upload_project_file(project_id, file_path)
        except FileNotFoundError as exc:
            return _json({"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - tools report, never raise
            LOGGER.error("upload_file failed: %s", exc)
            return _json({"error": str(exc)})
        return _json({"project_id": project_id, "upload": result})

    @server.tool()
    async def list_connectors() -> str:
        """List the services Kollektiv can call (GitHub, Google, Notion, …) and their actions."""
        instance = await get_orchestrator()
        registry = getattr(instance, "connectors", None)
        if registry is None:
            return json.dumps({"count": 0, "connectors": []}, indent=2)
        return json.dumps(
            {
                "count": len(registry.names),
                "configured": registry.configured_names(),
                "connectors": registry.statuses(),
                "actions": registry.catalog(),
            },
            indent=2,
        )

    @server.tool()
    async def call_connector(name: str, action: str, params_json: str = "{}", confirm: bool = False) -> str:
        """Call one connector action. ``params_json`` is a JSON object of parameters."""
        instance = await get_orchestrator()
        registry = getattr(instance, "connectors", None)
        if registry is None:
            return json.dumps({"error": "the connector registry is not available"}, indent=2)
        try:
            params = json.loads(params_json or "{}")
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"params_json is not valid JSON: {exc}"}, indent=2)
        try:
            result = await registry.call(name, action, params, confirm=confirm)
        except Exception as exc:  # noqa: BLE001 - report, never crash the MCP server
            return json.dumps({"error": str(exc)}, indent=2)
        return json.dumps({"connector": name, "action": action, "result": result}, indent=2)

    @server.tool()
    async def get_agent_pool_status(probe: bool = False) -> str:
        """Return worker agent status (busy/idle/rate limited and counters).

        Args:
            probe: Also perform a lightweight liveness request per agent.
        """
        instance = await get_orchestrator()
        try:
            agents = await instance.get_agents_status(probe=probe)
        except Exception as exc:  # noqa: BLE001 - tools report, never raise
            return _json({"error": str(exc)})
        return _json({"count": len(agents), "agents": agents})

    @server.tool()
    async def get_storage_status() -> str:
        """Return the aggregate TeraBox quota across the storage pool."""
        instance = await get_orchestrator()
        try:
            status = await instance.get_storage_status()
        except Exception as exc:  # noqa: BLE001 - tools report, never raise
            return _json({"error": str(exc)})
        return _json(status)

    @server.tool()
    async def trigger_sync() -> str:
        """Run the GitHub -> TeraBox -> agents synchronisation immediately."""
        instance = await get_orchestrator()
        try:
            summary = await instance.trigger_sync()
        except Exception as exc:  # noqa: BLE001 - tools report, never raise
            LOGGER.error("trigger_sync failed: %s", exc)
            return _json({"error": str(exc)})
        return _json(summary)

    @server.tool()
    async def estimate_cost(project_id: str, n_agents: int = 0) -> str:
        """Estimate what running a project will cost, before running it.

        The number is arithmetic on the plan (task count and description length),
        the configured prices and what the project has already spent — an
        estimate, labelled as one. Use it to decide whether a run is worth it,
        or to check it against the project's ``.kollektiv.yml`` cap.

        Args:
            project_id: Identifier returned by ``create_project``.
            n_agents: Estimate a different agent count (0 = the project's own).
        """
        instance = await get_orchestrator()
        try:
            estimate = await instance.estimate_project_cost(project_id, n_agents=n_agents or None)
        except KeyError:
            return _json({"error": f"unknown project: {project_id}"})
        except Exception as exc:  # noqa: BLE001 - tools report, never raise
            LOGGER.error("estimate_cost failed: %s", exc)
            return _json({"error": str(exc)})
        return _json(estimate.to_dict())

    @server.tool()
    async def budget_report() -> str:
        """Return the local spend ledger: tokens, dollars, caps and today's total.

        The tally lives in this deployment's own database. Nothing is uploaded
        and the ledger stores tokens and dollars — never prompts, files or
        identifiers.
        """
        instance = await get_orchestrator()
        try:
            report = await instance.budget_report()
        except Exception as exc:  # noqa: BLE001 - tools report, never raise
            LOGGER.error("budget_report failed: %s", exc)
            return _json({"error": str(exc)})
        return _json(report)

    @server.tool()
    async def sponsor_line(context: str = "waiting") -> str:
        """Return the opt-in sponsor line for a dead-time moment, if enabled.

        A no-op unless the deployment set ``SPONSORS_ENABLED=true``: the answer
        is ``{"line": null}`` and nothing is fetched, shown or recorded. Drawing
        a line accrues it in the local ledger, which never leaves the machine.

        Args:
            context: Dead-time context: ``waiting``, ``between-tasks`` or
                ``rate-limit``. Any other value deliberately yields no line.
        """
        from src.sponsors.line import SponsorLineMux

        try:
            line = await SponsorLineMux(settings=resolved).next_line(context=context)
        except Exception as exc:  # noqa: BLE001 - a broken sponsor must not break a tool call
            LOGGER.error("sponsor_line failed: %s", exc)
            return _json({"error": str(exc)})
        return _json({"line": line})

    @server.tool()
    async def sponsor_ledger() -> str:
        """Return the local sponsor ledger: impressions, cents earned, threshold.

        The tally lives in this deployment's own database. Nothing is sent
        anywhere by this call, and ``kollektiv sponsors forget`` deletes it.
        """
        from src.sponsors.ledger import SponsorLedger

        try:
            summary = await SponsorLedger(resolved).summary()
        except Exception as exc:  # noqa: BLE001 - tools report, never raise
            LOGGER.error("sponsor_ledger failed: %s", exc)
            return _json({"error": str(exc)})
        return _json(summary)

    # Stash helpers for tests / embedding.
    server.kollektiv_state = state  # type: ignore[attr-defined]
    server.get_orchestrator = get_orchestrator  # type: ignore[attr-defined]
    return server


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line arguments for the MCP server."""
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Run the Kollektiv MCP server")
    parser.add_argument(
        "--transport",
        default=settings.MCP_TRANSPORT,
        choices=["stdio", "sse", "streamable-http"],
        help="MCP transport to serve (default: %(default)s)",
    )
    parser.add_argument("--host", default=settings.MCP_HOST, help="Bind address (default: %(default)s)")
    parser.add_argument("--port", type=int, default=settings.MCP_PORT, help="Bind port (default: %(default)s)")
    parser.add_argument("--log-level", default=settings.LOG_LEVEL, help="Log level (default: %(default)s)")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for ``python -m src.api.mcp_server``.

    Returns:
        A process exit code.
    """
    args = parse_args(argv)
    configure_logging(args.log_level)
    server = create_server()
    if args.transport == "stdio":
        LOGGER.info("Starting the Kollektiv MCP server (%s transport)", args.transport)
    else:
        LOGGER.info(
            "Starting the Kollektiv MCP server on %s:%s (%s transport)",
            args.host,
            args.port,
            args.transport,
        )
    try:
        if args.transport == "stdio":
            server.run("stdio")
        else:
            # ``run`` forwards host/port to the underlying ASGI transport.
            try:
                server.run(args.transport, host=args.host, port=args.port)
            except TypeError:
                # SDK v1 expects these on the constructor.
                server.settings.host = args.host  # type: ignore[attr-defined]
                server.settings.port = args.port  # type: ignore[attr-defined]
                server.run(args.transport)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        LOGGER.info("MCP server interrupted")
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero
        LOGGER.error("MCP server failed: %s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
