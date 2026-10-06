"""Command line interface for Kollektiv.

Installed as the ``kollektiv`` console script, or run with
``python -m src.api.cli``.

Commands
--------
``check``          validate configuration and report what is missing
``init-db``        create the SQLite schema
``plan``           plan a project from a description and print the task list
``run``            plan (optionally) and execute a project end to end
``status``         print a project's shared state
``projects``       list known projects
``sync``           run the GitHub -> TeraBox -> agents sync once
``serve-api``      run the FastAPI app with uvicorn
``serve-mcp``      run the MCP server
``secret``         print a fresh SECRET_KEY

Examples
--------
::

    kollektiv check
    kollektiv run "Build a URL shortener with FastAPI and tests" --agents 3 --name shortener
    kollektiv status prj_1234 --watch
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Dict, List, Optional

from config.settings import get_settings
from src.utils.crypto import generate_secret_key
from src.utils.logger import configure_logging, get_logger

LOGGER = get_logger(__name__)


def _print(payload: Any, as_json: bool = False) -> None:
    """Print a payload as JSON or as readable text."""
    if as_json:
        print(json.dumps(payload, default=str, indent=2))
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                print(f"{key}:")
                print(json.dumps(value, default=str, indent=2)[:4000])
            else:
                print(f"{key}: {value}")
        return
    print(payload)


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------
async def cmd_check(args: argparse.Namespace) -> int:
    """Validate configuration and report subsystem readiness."""
    settings = get_settings()
    report: Dict[str, Any] = {
        "environment": settings.ENVIRONMENT,
        "warnings": settings.config_warnings(),
        "storage": {
            "configured": settings.is_terabox_configured,
            "accounts": len(settings.terabox_account_list()),
        },
        "agents": {
            "configured": settings.is_arena_configured,
            "accounts": len(settings.arena_account_list()),
        },
        "brain": {"configured": settings.is_brain_configured, "provider": settings.BRAIN_PROVIDER},
        "github": {"configured": settings.is_github_configured, "repo": settings.GITHUB_REPO},
    }

    if args.live:
        from src.orchestrator.app import Orchestrator

        orchestrator = Orchestrator(settings)
        try:
            report["github"]["connection"] = await orchestrator.github.check_connection()
            report["storage"]["connection"] = await orchestrator.pool.initialize()
            report["agents"]["connection"] = await orchestrator.agent_pool.initialize()
        finally:
            await orchestrator.stop()

    _print(report, args.json)
    if args.json:
        return 0
    ok = all(
        [
            report["brain"]["configured"],
            report["storage"]["configured"] or not args.require_storage,
            report["agents"]["configured"] or not args.require_agents,
        ]
    )
    print()
    print("Result:", "OK" if ok else "INCOMPLETE (see warnings above)")
    return 0 if ok else 1


async def cmd_init_db(args: argparse.Namespace) -> int:
    """Create the SQLite schema."""
    from src.db.models import get_engine, init_db

    init_db(get_engine())
    _print({"status": "ok", "database": get_settings().DATABASE_URL})
    return 0


async def cmd_projects(args: argparse.Namespace) -> int:
    """List known projects."""
    from src.orchestrator.app import Orchestrator

    orchestrator = Orchestrator()
    try:
        _print(await orchestrator.list_projects(), args.json)
    finally:
        await orchestrator.stop()
    return 0


async def cmd_plan(args: argparse.Namespace) -> int:
    """Plan a project without dispatching it."""
    from src.orchestrator.app import Orchestrator

    orchestrator = Orchestrator()
    try:
        record = await orchestrator.create_project(args.name or "", args.description, args.agents)
        _print(
            {
                "project_id": record["project_id"],
                "name": record["name"],
                "waves": record["plan"].get("waves"),
                "tasks": [
                    {
                        "id": task.get("id"),
                        "title": task.get("title"),
                        "priority": task.get("priority"),
                        "dependencies": task.get("dependencies"),
                    }
                    for task in record["plan"].get("tasks", [])
                ],
            },
            args.json,
        )
    finally:
        await orchestrator.stop()
    return 0


async def cmd_run(args: argparse.Namespace) -> int:
    """Plan (optionally) and execute a project."""
    from src.orchestrator.app import Orchestrator

    orchestrator = Orchestrator()
    try:
        start_report = await orchestrator.start()
        if start_report.get("warnings") and not args.json:
            for warning in start_report["warnings"]:
                print(f"warning: {warning}")

        if args.project_id:
            project_id = args.project_id
        else:
            record = await orchestrator.create_project(args.name or "", args.description, args.agents)
            project_id = record["project_id"]
            if not args.json:
                print(f"Project {project_id} planned with {len(record['plan'].get('tasks', []))} task(s)")

        summary = await orchestrator.run_project(project_id, max_concurrency=args.concurrency)
        _print(summary, args.json)

        if args.export_state:
            path = await orchestrator.state.export_to_path(args.export_state)
            if not args.json:
                print(f"State written to {path}")
        return 0 if summary.get("status") == "completed" else 2
    finally:
        await orchestrator.stop()


async def cmd_status(args: argparse.Namespace) -> int:
    """Print a project's shared state."""
    from src.orchestrator.app import Orchestrator

    orchestrator = Orchestrator()
    try:
        status = await orchestrator.get_project_status(args.project_id)
        _print(status, args.json)
    except KeyError:
        print(f"Unknown project: {args.project_id}", file=sys.stderr)
        return 1
    finally:
        await orchestrator.stop()
    return 0


async def cmd_sync(args: argparse.Namespace) -> int:
    """Run the sync engine once."""
    from src.orchestrator.app import Orchestrator

    orchestrator = Orchestrator()
    try:
        await orchestrator.pool.initialize()
        summary = await orchestrator.trigger_sync()
        _print(summary, args.json)
        return 0 if not summary.get("errors") else 2
    finally:
        await orchestrator.stop()


def cmd_secret(args: argparse.Namespace) -> int:
    """Print a freshly generated SECRET_KEY."""
    print(generate_secret_key())
    return 0


def cmd_serve_api(args: argparse.Namespace) -> int:
    """Run the FastAPI application with uvicorn."""
    import uvicorn

    from src.api.routes import app

    settings = get_settings()
    uvicorn.run(
        app,
        host=args.host or settings.API_HOST,
        port=args.port or settings.API_PORT,
        log_level=(args.log_level or settings.LOG_LEVEL).lower(),
        proxy_headers=True,
    )
    return 0


def cmd_serve_mcp(args: argparse.Namespace) -> int:
    """Run the MCP server."""
    from src.api.mcp_server import create_server

    settings = get_settings()
    server = create_server()
    transport = args.transport or settings.MCP_TRANSPORT
    try:
        if transport == "stdio":
            server.run("stdio")
        else:
            try:
                server.run(transport, host=args.host or settings.MCP_HOST, port=args.port or settings.MCP_PORT)
            except TypeError:  # pragma: no cover - SDK v1 signature
                server.settings.host = args.host or settings.MCP_HOST
                server.settings.port = args.port or settings.MCP_PORT
                server.run(transport)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 0
    return 0


# ----------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with every subcommand."""
    parser = argparse.ArgumentParser(
        prog="kollektiv",
        description="Multi-agent collaborative dev team orchestrator",
    )
    parser.add_argument("--log-level", default=None, help="Log level (DEBUG, INFO, ...)")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Validate configuration and readiness")
    check.add_argument("--json", action="store_true", help="Machine readable output")
    check.add_argument("--live", action="store_true", help="Also contact GitHub/TeraBox/agents")
    check.add_argument("--require-storage", action="store_true", help="Fail when TeraBox is unconfigured")
    check.add_argument("--require-agents", action="store_true", help="Fail when no agents are configured")

    sub.add_parser("init-db", help="Create the SQLite schema")
    sub.add_parser("projects", help="List projects").add_argument("--json", action="store_true")

    plan = sub.add_parser("plan", help="Plan a project without running it")
    plan.add_argument("description", help="What to build")
    plan.add_argument("--name", default="", help="Project name")
    plan.add_argument("--agents", type=int, default=3, help="Number of worker agents to plan for")
    plan.add_argument("--json", action="store_true")

    run = sub.add_parser("run", help="Plan and execute a project")
    run.add_argument("description", nargs="?", default="", help="What to build")
    run.add_argument("--project-id", default="", help="Run an existing project instead")
    run.add_argument("--name", default="", help="Project name")
    run.add_argument("--agents", type=int, default=3, help="Number of worker agents")
    run.add_argument("--concurrency", type=int, default=None, help="Max simultaneous agents")
    run.add_argument("--export-state", default="", help="Write PROJECT_STATE.md to this path")
    run.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="Print a project's shared state")
    status.add_argument("project_id")
    status.add_argument("--json", action="store_true")

    sync = sub.add_parser("sync", help="Run the GitHub -> TeraBox sync once")
    sync.add_argument("--json", action="store_true")

    sub.add_parser("secret", help="Print a new SECRET_KEY")

    serve_api = sub.add_parser("serve-api", help="Run the FastAPI app")
    serve_api.add_argument("--host", default=None)
    serve_api.add_argument("--port", type=int, default=None)

    serve_mcp = sub.add_parser("serve-mcp", help="Run the MCP server")
    serve_mcp.add_argument("--transport", default=None, choices=["stdio", "sse", "streamable-http"])
    serve_mcp.add_argument("--host", default=None)
    serve_mcp.add_argument("--port", type=int, default=None)

    return parser


#: Dispatch table mapping command names to coroutine/plain functions.
COMMANDS = {
    "check": cmd_check,
    "init-db": cmd_init_db,
    "projects": cmd_projects,
    "plan": cmd_plan,
    "run": cmd_run,
    "status": cmd_status,
    "sync": cmd_sync,
    "secret": cmd_secret,
    "serve-api": cmd_serve_api,
    "serve-mcp": cmd_serve_mcp,
}


def main(argv: Optional[List[str]] = None) -> int:
    """Run the CLI.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        A process exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)

    handler = COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse enforces the choices
        parser.error(f"unknown command: {args.command}")
        return 2

    try:
        if asyncio.iscoroutinefunction(handler):
            return int(asyncio.run(handler(args)) or 0)
        result = handler(args)
        return int(result) if isinstance(result, (int, float)) else 0
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\nInterrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - the CLI reports and exits non-zero
        LOGGER.error("%s failed: %s", args.command, exc, exc_info=args.log_level == "DEBUG")
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
