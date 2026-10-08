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
import os
import secrets
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import Settings, get_settings
from src.agents.providers import PROVIDER_PRESETS, resolve_provider
from src.utils.crypto import generate_secret_key
from src.utils.errors import BudgetError, ConfigurationError
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
def build_check_report(settings: Settings) -> Dict[str, Any]:
    """Describe what is configured, without contacting anything.

    Kept separate from :func:`cmd_check` so tests (and the dashboard) can read
    the report as data instead of parsing a terminal.

    Args:
        settings: The resolved settings.

    Returns:
        One block per subsystem: storage, agents, brain, GitHub, connectors and
        the MCP gateway.
    """
    from src.connectors.base import ConnectorRegistry

    registry = ConnectorRegistry.from_settings(settings)
    return {
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
        "connectors": {
            "count": len(registry.names),
            "configured": registry.configured_names(),
        },
        "gateway": {
            "enabled": bool(settings.GATEWAY_ENABLED),
            "url": f"http://{settings.GATEWAY_HOST}:{settings.GATEWAY_PORT}{settings.GATEWAY_MCP_PATH}",
            "require_tokens": bool(settings.GATEWAY_REQUIRE_TOKENS),
        },
    }


async def cmd_check(args: argparse.Namespace) -> int:
    """Validate configuration and report subsystem readiness."""
    settings = get_settings()
    report = build_check_report(settings)

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


def _print_estimate(estimate: Any) -> None:
    """Print a cost estimate the way an operator reads it.

    Args:
        estimate: A :class:`~src.orchestrator.budget.CostEstimate`.
    """
    tokens = estimate.total_tokens
    print(f"tasks    : {estimate.tasks} in {estimate.waves} wave(s), {estimate.agents} agent(s)")
    print(f"calls    : {estimate.brain_calls} brain, {estimate.worker_calls} worker")
    print(f"tokens   : ~{tokens:,} (brain {estimate.brain_tokens_in:,} in / {estimate.brain_tokens_out:,} out,")
    print(f"           worker {estimate.worker_tokens_in:,} in / {estimate.worker_tokens_out:,} out)")
    print(f"cost     : ${estimate.brain_usd:.4f} brain + ${estimate.worker_usd:.4f} workers = ${estimate.total_usd:.4f}")
    if estimate.spent_usd:
        print(f"spent    : ${estimate.spent_usd:.4f} already recorded → projected ${estimate.projected_usd:.4f}")
    cap = f"${estimate.max_usd:.2f}" if estimate.max_usd else "none"
    brain_in = estimate.prices.get("brain_in")
    brain_out = estimate.prices.get("brain_out")
    print(f"cap      : {cap} ({estimate.source}) · verdict {estimate.verdict}")
    print(f"prices   : brain ${brain_in}/M in, ${brain_out}/M out")
    print("Estimate only: arithmetic on the plan and your configured prices, not a quote.")


def _policy_line(policy: Any) -> str:
    """Render a policy as one readable line plus its confirm rules.

    Args:
        policy: A :class:`~src.gateway.policy.Policy`.

    Returns:
        A two-line string: what is allowed, and what needs a second yes.
    """
    sentence = (
        f"policy : {policy.name} · allow {', '.join(policy.allow) or 'nothing'}"
        f" · deny {', '.join(policy.deny) or 'nothing'}"
        f" · {'read-only' if policy.read_only else 'read-write'}"
    )
    for glob in policy.confirm:
        sentence += f"\n         confirm=true for {glob}"
    return sentence


async def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Create the local database, keys and a checklist for the free stack.

    Nothing here requires an account: it prepares everything Kollektiv needs to
    boot (SQLite schema, SECRET_KEY guidance, workspace directories) and then
    prints exactly which free-tier credentials to paste into ``.env``.
    """
    from src.db.models import bind_engine, init_db
    from src.utils.crypto import generate_secret_key

    settings = get_settings()
    created: List[str] = []

    # 1. Database schema (SQLite file or Neon Postgres).
    try:
        init_db(bind_engine(settings))
        created.append(f"database ready ({'postgres' if settings.is_postgres else 'sqlite'})")
    except Exception as exc:  # noqa: BLE001 - report, keep going
        LOGGER.error("Could not initialise the database: %s", exc)
        print(f"error: could not initialise the database: {exc}")
        return 1

    # 2. Workspace directories used by the collector and the state cache.
    workspace = settings.workspace_path
    (workspace / "state").mkdir(parents=True, exist_ok=True)
    created.append(f"workspace ready ({workspace})")

    # 3. A SECRET_KEY for the encrypted token store.
    if not settings.SECRET_KEY:
        created.append("SECRET_KEY is unset (see the value below)")
    else:
        created.append("SECRET_KEY is set")

    report = {
        "environment": settings.ENVIRONMENT,
        "created": created,
        "storage_backend": settings.storage_backend,
        "database": "postgres" if settings.is_postgres else "sqlite",
        "subsystems": {
            "brain": settings.is_brain_configured,
            "agents": settings.is_arena_configured,
            "storage": settings.storage_backend != "none",
            "github": settings.is_github_configured,
            "auth": settings.is_clerk_configured,
            "email": settings.is_resend_configured,
        },
        "warnings": settings.config_warnings(),
    }

    if args.json:
        _print(report, True)
        return 0

    print("Kollektiv bootstrap")
    print("===================")
    for item in created:
        print(f"  ok   {item}")
    print()
    if not settings.SECRET_KEY:
        print("Paste this into .env (keeps stored tokens decryptable):")
        print(f"  SECRET_KEY={generate_secret_key()}")
        print()
    print("Free-tier checklist (all optional, each unlocks one capability):")
    rows = [
        ("Cloudflare R2", "R2_BUCKET + R2_ACCESS_KEY_ID + R2_SECRET_ACCESS_KEY + R2_ENDPOINT",
         settings.storage_backend == "r2", "shared storage for state + artifacts"),
        ("Neon Postgres", "DATABASE_URL=<pooled connection string>",
         settings.is_postgres, "durable database for multi-replica deployments"),
        ("Clerk", "CLERK_SECRET_KEY + CLERK_PUBLISHABLE_KEY (+ AUTH_REQUIRED=true)",
         settings.is_clerk_configured, "user accounts and API authentication"),
        ("Resend", "RESEND_API_KEY + NOTIFY_EMAILS",
         settings.is_resend_configured, "run summaries and alerts by email"),
        ("Groq / DeepSeek", "BRAIN_API_KEY (+ BRAIN_PROVIDER)",
         settings.is_brain_configured, "LLM planning and reviews"),
        ("Worker agents", "kollektiv login --provider <name>  (BYOK, see docs/byok.md)",
         settings.is_arena_configured, "the agents that write the code"),
        ("Google Workspace", "GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + GOOGLE_REFRESH_TOKEN",
         settings.is_google_configured, "Gmail/Calendar/Drive as agent tools"),
        ("Notion", "NOTION_TOKEN",
         settings.is_notion_configured, "pages and databases as agent tools"),
        ("Event webhooks", "EVENT_WEBHOOKS=https://…",
         bool(settings.event_webhook_urls), "push run events to Slack/Discord/n8n/Zapier"),
    ]
    for name, keys, done, why in rows:
        mark = "ok  " if done else "todo"
        print(f"  [{mark}] {name:16} {keys}")
        print(f"           -> {why}")
    print()
    print("Next: `kollektiv check --json` for machine-readable status, then")
    print("      `kollektiv serve-api` and open http://localhost:8000/docs")
    return 0


async def cmd_connectors(args: argparse.Namespace) -> int:
    """List every connector, its status and the actions it exposes."""
    from src.connectors.base import ConnectorRegistry

    settings = get_settings()
    registry = ConnectorRegistry.from_settings(settings)
    try:
        configured: List[str] = registry.configured_names()
        statuses: List[Dict[str, Any]] = registry.statuses()

        if getattr(args, "probe", False):
            probes = await registry.probe_all()
            if args.json:
                _print(
                    {"count": len(registry.names), "configured": configured, "connectors": statuses, "probes": probes},
                    True,
                )
                return 0
            print("Connector probes (read-only)")
            print("============================")
            for probe in probes:
                mark = "ok  " if probe["ok"] else "FAIL"
                detail = probe.get("detail") or probe.get("error") or ""
                print(f"  [{mark}] {probe['connector']:<10} {probe['seconds']:>6.2f}s  {detail}")
            print()
            print(f"{sum(1 for p in probes if p['ok'])}/{len(probes)} reachable.")
            return 0 if all(p["ok"] for p in probes) else 1
        if args.json:
            _print(
                {"count": len(registry.names), "configured": configured, "connectors": statuses},
                True,
            )
            return 0
        print("Connectors")
        print("==========")
        for status in statuses:
            mark = "ok  " if status["configured"] else "todo"
            print(f"  [{mark}] {status['name']:<10} {status['detail']}")
            print(f"           actions: {', '.join(status['actions']) or 'none'}")
        print()
        print(f"{len(configured)}/{len(statuses)} ready. Add services in .env (see the README).")
        return 0
    finally:
        await registry.close()


async def cmd_call(args: argparse.Namespace) -> int:
    """Call one connector action from the command line."""
    from src.connectors.base import ConnectorRegistry

    settings = get_settings()
    registry = ConnectorRegistry.from_settings(settings)
    try:
        try:
            params = json.loads(args.params or "{}")
        except json.JSONDecodeError as exc:
            _print({"error": f"--params is not valid JSON: {exc}"}, True)
            return 2
        try:
            result = await registry.call(args.connector, args.action, params, confirm=args.confirm)
        except Exception as exc:  # noqa: BLE001 - the CLI reports, it does not traceback
            LOGGER.error("Connector call failed: %s", exc)
            _print({"error": str(exc)}, True)
            return 1
        _print({"connector": args.connector, "action": args.action, "result": result}, True)
        return 0
    finally:
        await registry.close()


async def cmd_init_db(args: argparse.Namespace) -> int:
    """Create the database schema (SQLite file or Neon/Postgres)."""
    from src.db.models import bind_engine, init_db

    settings = get_settings()
    init_db(bind_engine(settings))
    _print({"status": "ok", "database": settings.database_url, "driver": "postgres" if settings.is_postgres else "sqlite"})
    return 0


async def cmd_login(args: argparse.Namespace) -> int:
    """Register a worker that runs on your own API key (BYOK).

    The key is encrypted with ``SECRET_KEY`` and stored in the database, and the
    worker is written into ``ARENA_ACCOUNTS`` in the env file. The key itself
    never goes into ``.env``. With ``--key-env NAME`` the key stays in your
    environment instead and nothing is stored. Local providers (Ollama, LM
    Studio) need no key at all.

    Non-interactive use reads the key from ``--token``, ``KOLLEKTIV_TOKEN`` or
    ``--key-env`` so nothing has to be typed at a prompt.
    """
    import getpass
    import re

    from src.db.models import bind_engine
    from src.utils.token_store import TokenStore

    settings = get_settings()
    try:
        preset = resolve_provider(args.provider)
    except ConfigurationError as exc:
        _print({"error": str(exc)}, True)
        return 2
    provider = args.provider.strip().lower()
    kind = preset["kind"]
    name = args.name or provider
    account_id = args.account or re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-") or provider

    key = ""
    if args.key_env:
        key = os.environ.get(args.key_env, "").strip()
        if not key and kind == "key":
            _print({"error": f"The environment variable {args.key_env} is empty or unset."}, True)
            return 2
    else:
        key = (args.token or os.environ.get("KOLLEKTIV_TOKEN", "")).strip()
        if not key and kind == "key":
            if not sys.stdin.isatty():
                _print(
                    {
                        "error": "No API key supplied and stdin is not a terminal.",
                        "next": f"run `kollektiv login --provider {provider}` interactively, "
                        "or set KOLLEKTIV_TOKEN / pass --key-env",
                    },
                    True,
                )
                return 2
            print(f"{preset['label']} ({provider})")
            print(f"  {preset['hint']}")
            key = getpass.getpass("  API key (hidden): ").strip()
            if not key:
                _print({"error": "No API key supplied."}, True)
                return 2

    stored_in_db = bool(key) and not args.key_env
    if stored_in_db:
        store = TokenStore(settings.fernet_secret, engine=bind_engine(settings))
        store.save_token(provider, account_id, {"api_key": key, "provider": provider, "kind": kind})

    entry: Dict[str, str] = {"name": name, "provider": provider, "account_id": account_id}
    if args.model:
        entry["model"] = args.model
    if args.base_url:
        entry["base_url"] = args.base_url
    if args.key_env:
        entry["api_key_env"] = args.key_env
    workers = _read_worker_list(settings.ARENA_ACCOUNTS)
    workers = [w for w in workers if str(w.get("account_id") or w.get("name") or "") != account_id]
    workers.append(entry)
    set_env_value(args.env_file, "ARENA_ACCOUNTS", json.dumps(workers, separators=(",", ":")))

    preview = mask_secret(key) if key else ("from " + args.key_env if args.key_env else "none needed")
    report: Dict[str, Any] = {
        "registered": True,
        "name": name,
        "account_id": account_id,
        "provider": provider,
        "key": preview,
        "key_storage": "encrypted database" if stored_in_db else ("environment" if args.key_env else "none"),
        "env_file": args.env_file,
        "next": "kollektiv check --json | jq .subsystems.agents",
    }
    if args.json:
        _print(report, True)
    else:
        print(f"Registered worker {name!r} on {preset['label']}.")
        print(f"  key: {preview} (stored: {report['key_storage']})")
        print(f"  wrote ARENA_ACCOUNTS to {args.env_file}; keys never go into the env file")
        print(f"  check it: {report['next']}")
    return 0


def _read_worker_list(raw: str) -> List[Dict[str, Any]]:
    """Parse ``ARENA_ACCOUNTS`` as a list of dicts, tolerating an empty or broken value."""
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        LOGGER.warning("ARENA_ACCOUNTS is not valid JSON; it will be rewritten from scratch")
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


async def cmd_logout(args: argparse.Namespace) -> int:
    """Remove a worker's stored key and its ``ARENA_ACCOUNTS`` entry."""
    from src.db.models import bind_engine
    from src.utils.token_store import TokenStore

    settings = get_settings()
    provider = (args.provider or "deepseek").strip().lower()
    account_id = args.account or provider
    store = TokenStore(settings.fernet_secret, engine=bind_engine(settings))
    removed_key = store.delete_token(provider, account_id)
    workers = _read_worker_list(settings.ARENA_ACCOUNTS)
    kept = [w for w in workers if str(w.get("account_id") or w.get("name") or "") != account_id]
    removed_entry = len(kept) != len(workers)
    if removed_entry:
        set_env_value(args.env_file, "ARENA_ACCOUNTS", json.dumps(kept, separators=(",", ":")))
    removed = removed_key or removed_entry
    _print(
        {
            "removed": removed,
            "provider": provider,
            "account": account_id,
            "key_deleted": removed_key,
            "worker_removed_from_env": removed_entry,
        },
        True,
    )
    return 0 if removed else 1


async def cmd_accounts(args: argparse.Namespace) -> int:
    """List registered workers and stored keys (keys are always masked)."""
    from src.db.models import bind_engine
    from src.utils.token_store import TokenStore

    settings = get_settings()
    store = TokenStore(settings.fernet_secret, engine=bind_engine(settings))
    rows = []
    for record in store.list_tokens():
        service = str(record.get("service") or "")
        account_id = str(record.get("account_id") or "")
        data = store.get_token(service, account_id) if service and account_id else {}
        key = str(data.get("api_key") or "")
        rows.append(
            {
                "provider": service,
                "account": account_id,
                "key": mask_secret(key) if key else "",
                "expires_at": str(record.get("expires_at") or ""),
            }
        )
    workers = [
        {"name": w.get("name", ""), "provider": w.get("provider", ""), "account_id": w.get("account_id", "")}
        for w in _read_worker_list(settings.ARENA_ACCOUNTS)
    ]
    if args.json:
        _print({"count": len(rows), "keys": rows, "workers": workers}, True)
        return 0
    print("Workers (ARENA_ACCOUNTS)")
    print("========================")
    if not workers:
        print("  none — run `kollektiv login --provider deepseek` (or any provider in docs/byok.md)")
    for worker in workers:
        print(f"  {worker['name'] or '(unnamed)':<14} {worker['provider']:<12} {worker['account_id']}")
    print()
    print("Stored keys (encrypted with SECRET_KEY)")
    print("=======================================")
    if not rows:
        print("  none stored (keys read from environment variables are not stored)")
    for row in rows:
        print(f"  {row['provider']:<12} {row['account']:<14} {row['key']}")
    return 0


def set_env_value(path: str, key: str, value: str) -> bool:
    """Create or update ``KEY=value`` in an env file.

    Rewrites the file in place, preserving comments and ordering, and appends
    the key when it is absent. Nothing else in the file is touched.

    Args:
        path: Path of the env file (usually ``.env``).
        key: Variable name.
        value: New value.

    Returns:
        ``True`` when the file changed, ``False`` when it already said exactly
        that.

    Raises:
        OSError: When the file cannot be read or written.
    """
    target = Path(path)
    line = f"{key}={value}"
    if target.exists():
        original = target.read_text(encoding="utf-8")
    else:
        original = ""
    out: List[str] = []
    replaced = False
    for existing in original.splitlines():
        stripped = existing.strip()
        if stripped.startswith(f"{key}=") and not stripped.startswith("#"):
            if not replaced:
                out.append(line)
                replaced = True
            continue
        out.append(existing)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append("# Added by the Kollektiv CLI")
        out.append(line)
    updated = "\n".join(out).rstrip("\n") + "\n"
    if updated == original:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(updated, encoding="utf-8")
    return True


def read_env_flag(path: str, key: str, default: bool = False) -> bool:
    """Read a boolean flag from an env file without importing the settings cache.

    Args:
        path: Path of the env file.
        key: Variable name.
        default: Returned when the file or the key is missing.

    Returns:
        The parsed boolean.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return default
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').lower() in {"1", "true", "yes", "on"}
    return default


def load_env_file(path: str = ".env", *, override: bool = False) -> List[str]:
    """Load an env file into ``os.environ`` and return the keys it set.

    ``pydantic-settings`` reads ``.env`` for *its own* fields, which is enough for
    the API. It is not enough for the desktop shell, the packaged sidecar or a
    bare ``uvicorn`` run, because those read environment variables before
    settings exist. Calling this first makes ``kollektiv keys`` a complete answer:
    write the file once, and every entry point finds it.

    Real environment variables always win unless ``override`` is asked for, so a
    container or a systemd unit keeps control.

    Args:
        path: Env file to read; a missing file is not an error.
        override: Replace variables that are already set.

    Returns:
        The names of the variables that were set from the file.
    """
    target = Path(path)
    if not target.exists():
        return []
    applied: List[str] = []
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or (key in os.environ and not override):
            continue
        os.environ[key] = value
        applied.append(key)
    return applied


async def cmd_gateway(args: argparse.Namespace) -> int:
    """Manage the MCP gateway: clients, tokens, policies, audit, serve.

    Args:
        args: Parsed arguments; ``action`` selects the sub-behaviour.

    Returns:
        A process exit code.
    """
    from src.db.models import init_db
    from src.gateway.app import create_gateway_app
    from src.gateway.audit import GatewayAudit
    from src.gateway.auth import GatewayAuth
    from src.gateway.policy import POLICY_PRESETS, Policy
    from src.gateway.tools import build_catalogue, toolkit_view

    settings = get_settings()
    action = args.action
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 - report, do not traceback
        LOGGER.warning("Could not initialise the database schema: %s", exc)

    if action == "serve":
        import uvicorn

        # A packaged sidecar and a desktop launch have no shell profile to read,
        # so pick up .env here: one file, every entry point.
        applied = load_env_file(getattr(args, "env_file", ".env"))
        if applied:
            LOGGER.info("Loaded %s value(s) from %s: %s", len(applied), args.env_file, ", ".join(applied))
            settings = get_settings()

        app = create_gateway_app(
            settings.model_copy(update={"GATEWAY_ENABLED": True}, deep=True)
            if not settings.GATEWAY_ENABLED
            else settings
        )
        host = args.host or settings.GATEWAY_HOST
        port = args.port or settings.GATEWAY_PORT
        print(f"Kollektiv gateway on http://{host}:{port}  (MCP at {settings.GATEWAY_MCP_PATH})")
        if not settings.GATEWAY_REQUIRE_TOKENS:
            print("warning: GATEWAY_REQUIRE_TOKENS=false — every call is anonymous")
        elif not await GatewayAuth(settings).clients():
            print("warning: no clients yet — run `kollektiv gateway init` first")
        uvicorn.run(app, host=host, port=port, log_level=settings.LOG_LEVEL.lower())
        return 0

    auth = GatewayAuth(settings)
    audit = GatewayAudit(settings)

    if action in {"init", "token"}:
        name = args.name or settings.GATEWAY_DEFAULT_CLIENT
        role = args.role or "dashboard"
        try:
            issued = await auth.issue(name, label=args.label or name, role=role, rotate=args.rotate)
        except Exception as exc:  # noqa: BLE001 - a duplicate name is a normal answer
            _print({"error": str(exc)}, args.json)
            return 1
        mcp_url = f"http://<your-host>:{settings.GATEWAY_PORT}{settings.GATEWAY_MCP_PATH}"
        payload = {
            "client": issued["name"],
            "role": issued["role"],
            "token": issued["token"],
            "policy": issued["policy"],
            "mcp_url": mcp_url,
            "claude_code": f"claude mcp add kollektiv --transport http {mcp_url} --header \"Authorization: Bearer {issued['token']}\"",
            "note": "This token is shown once. Store it in the client, not in git.",
        }
        if args.json:
            _print(payload, True)
            return 0
        policy = payload["policy"]
        print(f"client   : {payload['client']} (role {payload['role']})")
        print(f"token    : {payload['token']}")
        print()
        print("This token is shown once. Point a client at the gateway:")
        print(f"  MCP URL      : {mcp_url}")
        print(f"  Claude Code  : {payload['claude_code']}")
        print('  Generic MCP  : {"headers": {"Authorization": "Bearer <token>"}}')
        print()
        print(
            f"Policy ({policy['name']}): allow {' , '.join(policy['allow']) or 'nothing'}"
            f" · deny {', '.join(policy['deny']) or 'nothing'}"
            f" · {'read-only' if policy['read_only'] else 'read-write'}"
        )
        for glob in policy["confirm"]:
            print(f"  confirm=true required for {glob}")
        print()
        print("Start it with: kollektiv gateway serve")
        return 0

    if action == "clients":
        rows = await auth.clients()
        if args.json:
            _print({"count": len(rows), "clients": rows}, True)
            return 0
        if not rows:
            print("no gateway clients yet — `kollektiv gateway init`")
            return 0
        for row in rows:
            seen = (row["last_seen"] or "never")[:19]
            print(f"  {row['name']:<16} {row['role']:<10} {row['calls']:>5} calls  last {seen}  policy {row['policy']['name']}")
        return 0

    if action == "revoke":
        name = args.name or settings.GATEWAY_DEFAULT_CLIENT
        removed = await auth.revoke(name)
        _print({"client": name, "revoked": removed}, args.json)
        return 0 if removed else 1

    if action == "policy":
        name = args.name or settings.GATEWAY_DEFAULT_CLIENT
        client_row = await auth.get(name)
        if client_row is None:
            _print({"error": f"unknown client {name!r}"}, args.json)
            return 1
        if args.preset or args.allow or args.deny or args.confirm or args.read_only:
            current = auth.policy_for(client_row)
            preset = Policy.preset(args.preset) if args.preset else None
            chosen = Policy(
                allow=tuple(args.allow.split(",")) if args.allow else (preset.allow if preset else current.allow),
                deny=tuple(args.deny.split(",")) if args.deny else (preset.deny if preset else current.deny),
                confirm=tuple(args.confirm.split(",")) if args.confirm else (preset.confirm if preset else current.confirm),
                read_only=bool(args.read_only) or bool(preset and preset.read_only),
                name=args.preset or "custom",
            )
            from src.db.models import GatewayClientRecord, session_scope

            with session_scope() as session:
                stored = session.get(GatewayClientRecord, name)
                if stored is None:
                    _print({"error": f"unknown client {name!r}"}, args.json)
                    return 1
                stored.policy = chosen.to_json()
                stored.role = args.preset or stored.role
                session.add(stored)
                session.commit()
            if args.json:
                _print({"client": name, "policy": chosen.to_dict(), "applied": True}, True)
                return 0
            print(f"applied: {chosen.name} → {name}")
            _print(_policy_line(chosen), False)
            return 0

        current = auth.policy_for(client_row)
        if args.json:
            _print({"client": name, "role": client_row.role, "policy": current.to_dict()}, True)
            return 0
        print(f"client : {name} (role {client_row.role})")
        print(_policy_line(current))
        return 0

    if action == "presets":
        presets = {key: Policy.from_dict(value, name=key).to_dict() for key, value in POLICY_PRESETS.items()}
        if args.json:
            _print({"presets": presets}, True)
            return 0
        for name, policy in presets.items():
            flags = "read-only" if policy["read_only"] else "read-write"
            print(f"  {name:<11} allow {', '.join(policy['allow']) or 'nothing':<22} {flags}")
            for glob in policy["confirm"]:
                print(f"  {'':<11} confirm=true for {glob}")
        print()
        print("Apply one with: kollektiv gateway policy --preset read-only")
        return 0

    if action == "tools":
        from src.orchestrator.app import Orchestrator

        orchestrator = Orchestrator(settings)
        try:
            await orchestrator.start()
            catalogue = build_catalogue(orchestrator, settings=settings)
        finally:
            await orchestrator.stop()
        if args.json:
            _print({"count": len(catalogue), "toolkits": toolkit_view(catalogue)}, True)
            return 0
        for group in toolkit_view(catalogue):
            print(f"{group['namespace']} ({group['count']})")
            for tool in group["tools"]:
                flag = " ⚠" if tool["dangerous"] else ""
                print(f"  {tool['tool']:<44}{flag}")
        return 0

    if action == "audit":
        if args.clear:
            purged = await audit.clear()
            _print({"cleared": purged}, args.json)
            return 0
        rows = await audit.recent(limit=args.limit or settings.GATEWAY_AUDIT_LIMIT, client=args.name or None)
        stats = await audit.stats(args.name or None)
        if args.json:
            _print({"stats": stats, "rows": rows}, True)
            return 0
        print(f"{stats['calls']} call(s) · {stats['failures']} failure(s) · {stats['denied']} denied")
        for row in rows[: args.limit or 25]:
            when = (row["at"] or "")[11:19]
            verdict = "denied" if row["denied"] else ("ok" if row["ok"] else "failed")
            print(f"  {when}  {row['client']:<14} {row['tool']:<40} {verdict:<7} {row['ms']:>5}ms")
        return 0

    if action == "status":
        clients = await auth.clients()
        stats = await audit.stats()
        payload = {
            "enabled": bool(settings.GATEWAY_ENABLED),
            "host": settings.GATEWAY_HOST,
            "port": settings.GATEWAY_PORT,
            "mcp_path": settings.GATEWAY_MCP_PATH,
            "require_tokens": bool(settings.GATEWAY_REQUIRE_TOKENS),
            "policy_file": settings.GATEWAY_POLICY_PATH or None,
            "clients": [{"name": row["name"], "role": row["role"], "calls": row["calls"]} for row in clients],
            "audit": stats,
            "note": "tokens live encrypted in your own database; the audit log never stores call arguments",
        }
        if args.json:
            _print(payload, True)
            return 0
        print(f"gateway   : {'enabled' if payload['enabled'] else 'disabled'} ({payload['host']}:{payload['port']})")
        print(f"MCP path  : {payload['mcp_path']}   tokens required: {payload['require_tokens']}")
        print(f"clients   : {len(payload['clients'])}")
        for row in payload["clients"]:
            print(f"  {row['name']:<16} {row['role']:<10} {row['calls']:>5} calls")
        print(f"audit     : {stats['calls']} call(s), {stats['denied']} denied")
        print("start it  : kollektiv gateway serve")
        return 0

    _print({"error": f"unknown gateway action {action!r}"}, args.json)
    return 2


async def cmd_resume(args: argparse.Namespace) -> int:
    """Print the resume briefing for a project (see GET /projects/{id}/handoff)."""
    from src.orchestrator.app import Orchestrator

    settings = get_settings()
    orchestrator = Orchestrator(settings)
    try:
        await orchestrator.start()
        try:
            handoff = await orchestrator.get_handoff(args.project_id, write=not args.no_write)
        except KeyError as exc:
            _print({"error": str(exc)}, True)
            return 1
        if args.json:
            _print({key: value for key, value in handoff.items() if key != "markdown"}, True)
        else:
            print(handoff["markdown"])
            if handoff.get("written_to"):
                print()
                print(f"(also written to {handoff['written_to']})")
        return 0
    finally:
        await orchestrator.stop()


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
    """Plan (optionally) and execute a project.

    With ``--dry-run`` nothing is dispatched: the project is planned (when it is
    new), estimated against the configured caps, and the numbers are printed.
    """
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
            agents = args.agents or orchestrator.project_config().n_agents or 3
            record = await orchestrator.create_project(args.name or "", args.description, agents)
            project_id = record["project_id"]
            if not args.json:
                print(f"Project {project_id} planned with {len(record['plan'].get('tasks', []))} task(s)")

        if args.dry_run:
            estimate = await orchestrator.estimate_project_cost(project_id)
            if args.json:
                _print(estimate.to_dict(), True)
            else:
                print("Dry run — nothing was dispatched.")
                _print_estimate(estimate)
            return 0 if estimate.verdict != "over" else 3

        summary = await orchestrator.run_project(
            project_id, max_concurrency=args.concurrency, allow_over_budget=args.allow_over_budget
        )
        _print(summary, args.json)

        if args.export_state:
            path = await orchestrator.state.export_to_path(args.export_state)
            if not args.json:
                print(f"State written to {path}")
        return 0 if summary.get("status") == "completed" else 2
    except BudgetError as exc:
        print(f"budget: {exc.message}", file=sys.stderr)
        print("Raise the cap in .kollektiv.yml (budget.max_usd), or pass --allow-over-budget.", file=sys.stderr)
        return 3
    finally:
        await orchestrator.stop()


async def cmd_estimate(args: argparse.Namespace) -> int:
    """Estimate a project's cost without running it."""
    from src.orchestrator.app import Orchestrator

    orchestrator = Orchestrator()
    try:
        estimate = await orchestrator.estimate_project_cost(args.project_id, n_agents=args.agents or None)
    except KeyError:
        print(f"Unknown project: {args.project_id}", file=sys.stderr)
        return 1
    finally:
        await orchestrator.stop()
    if args.json:
        _print(estimate.to_dict(), True)
    else:
        _print_estimate(estimate)
    return 0 if estimate.verdict != "over" else 3


async def cmd_budget(args: argparse.Namespace) -> int:
    """Show the local spend ledger: tokens and dollars, per project and today."""
    from src.orchestrator.app import Orchestrator

    orchestrator = Orchestrator()
    try:
        report = await orchestrator.budget_report()
    finally:
        await orchestrator.stop()
    if args.json:
        _print(report, True)
        return 0
    cap = report["max_usd"] or "none"
    daily = report["daily_max_usd"] or "none"
    today = report["today"]
    print(f"budget   : {'on' if report['enabled'] else 'off'}  ·  cap per project: {cap} USD  ·  daily cap: {daily} USD")
    print(
        f"today    : ${today['usd']:.4f}  ·  {today['runs']} run(s)  ·  "
        f"{today['tokens_in']:,} in / {today['tokens_out']:,} out tokens"
    )
    print(f"all time : ${report['total']['usd']:.4f}  ·  {report['total']['runs']} run(s)  ·  {report['total']['days']} day(s)")
    if report["projects"]:
        print("projects :")
        for name in report["projects"]:
            print(f"  {name}")
    prices = report["prices_per_mtok"]
    print(
        f"prices   : brain ${prices['brain_in']}/M in, ${prices['brain_out']}/M out"
        f"  ·  workers ${prices['worker_in']}/M in, ${prices['worker_out']}/M out"
    )
    print(f"daily cap: {report['daily_cap']['detail']}")
    config = report.get("project_config") or {}
    if config.get("path"):
        print(f"config   : {config['path']}")
    print(f"ledger   : {report['note']}")
    return 0


async def cmd_init_config(args: argparse.Namespace) -> int:
    """Write a commented ``.kollektiv.yml`` starter file."""
    from src.utils.project_config import CONFIG_NAMES, STARTER_TEMPLATE, load_project_config

    target = Path(args.path or CONFIG_NAMES[0])
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    try:
        target.write_text(STARTER_TEMPLATE, encoding="utf-8")
    except OSError as exc:
        print(f"could not write {target}: {exc}", file=sys.stderr)
        return 1
    config = load_project_config(str(target))
    if config.problems:
        print(f"{target} was written but does not parse: {'; '.join(config.problems)}", file=sys.stderr)
        return 1
    print(f"wrote {target} ({len(STARTER_TEMPLATE.splitlines())} lines)")
    print("Edit it, then `kollektiv estimate --project-id prj_…` to see the effect.")
    return 0


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
async def cmd_keys(args: argparse.Namespace) -> int:
    """Generate the three local secrets Kollektiv needs, and write them to .env.

    This is the answer to "what do I have to do myself?" — once:

    * ``SECRET_KEY`` — Fernet key for ``TokenStore`` (worker tokens, gateway
      tokens, connector credentials are encrypted with it). Lose it and stored
      tokens must be re-entered; that is the whole point.
    * ``SESSION_TOKEN`` — the shared secret the service uses for its own
      internal callbacks.
    * ``GATEWAY_ADMIN_TOKEN`` — a ``kgw_…`` admin token for an AI client (or the
      desktop shell) to reach ``kollektiv gateway serve``.

    Existing values are **kept** unless ``--rotate`` is given, so running this
    twice does not lock you out of anything. The file is edited in place
    (comments and ordering preserved) and printed with the secret partially
    masked, because a terminal history is not a secrets manager.
    """
    from cryptography.fernet import Fernet

    from src.gateway.auth import GatewayAuth
    from src.gateway.policy import Policy

    env_path = Path(args.env_file)
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    changed: List[str] = []
    kept: List[str] = []

    def present(key: str) -> bool:
        """Return whether the env file already sets ``key`` to something."""
        for line in existing.splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{key}=") and not stripped.startswith("#"):
                return bool(stripped.split("=", 1)[1].strip())
        return False

    secret = ""
    if args.rotate or not present("SECRET_KEY"):
        secret = Fernet.generate_key().decode("utf-8")
        set_env_value(str(env_path), "SECRET_KEY", secret)
        changed.append("SECRET_KEY")
    else:
        kept.append("SECRET_KEY")

    session = ""
    if args.rotate or not present("SESSION_TOKEN"):
        session = secrets.token_urlsafe(32)
        set_env_value(str(env_path), "SESSION_TOKEN", session)
        changed.append("SESSION_TOKEN")
    else:
        kept.append("SESSION_TOKEN")

    gateway_token = ""
    if args.rotate or not present("GATEWAY_ADMIN_TOKEN"):
        settings = get_settings()
        auth = GatewayAuth(settings)
        try:
            issued = await auth.issue(
                "desktop",
                label="Desktop shell and local AI clients",
                role="admin",
                policy=Policy.preset("admin"),
                rotate=True,
            )
            gateway_token = str(issued["token"])
        except Exception as exc:  # noqa: BLE001 - report, do not crash the rest
            print(f"kollektiv: could not issue a gateway token: {exc}", file=sys.stderr)
        else:
            set_env_value(str(env_path), "GATEWAY_ADMIN_TOKEN", gateway_token)
            changed.append("GATEWAY_ADMIN_TOKEN")
    else:
        kept.append("GATEWAY_ADMIN_TOKEN")

    payload = {
        "env_file": str(env_path),
        "written": changed,
        "kept": kept,
        "secret_key": secret,
        "session_token": session,
        "gateway_admin_token": gateway_token,
        "masked": {
            "secret_key": mask_secret(secret),
            "session_token": mask_secret(session),
            "gateway_admin_token": mask_secret(gateway_token),
        },
        "note": "Secrets are written to the env file and shown once. Keep that file out of git.",
    }
    if args.json:
        _print(payload, True)
        return 0

    def show(label: str, value: str) -> str:
        """Mask all but the last four characters of a secret."""
        return mask_secret(value) if value else "(kept the existing value)"

    print(f"env file : {env_path}")
    print(f"secret   : {show('SECRET_KEY', secret)}")
    print(f"session  : {show('SESSION_TOKEN', session)}")
    print(f"gateway  : {show('GATEWAY_ADMIN_TOKEN', gateway_token)}")
    if kept:
        print(f"kept     : {', '.join(kept)} (--rotate to replace)")
    print("next     : kollektiv bootstrap && kollektiv serve-api")
    return 0


def mask_secret(value: str) -> str:
    """Return a secret with everything but its last four characters hidden."""
    if not value:
        return ""
    tail = value[-4:] if len(value) > 4 else ""
    return f"{'*' * 8}{tail}"


async def cmd_links(args: argparse.Namespace) -> int:
    """List agent-connector grants: who may use which service.

    The connectors command lists *actions*; this lists *grants*. A connector
    with no grant is open to every caller, so a fresh install needs none.
    """
    from src.orchestrator.app import Orchestrator

    orchestrator = Orchestrator()
    try:
        links = await orchestrator.list_links()
        connectors = orchestrator.connector_names()
    finally:
        await orchestrator.stop()
    if args.json:
        _print({"count": len(links), "connectors": connectors, "links": links}, True)
        return 0
    if not links:
        print("links    : none — every connector is open to every caller")
        print(f"connectors: {', '.join(connectors) or '(registry unavailable)'}")
        print("link one : kollektiv link <agent_id> <connector>")
        return 0
    print(f"links    : {len(links)}")
    for link in links:
        note = f"  — {link['note']}" if link["note"] else ""
        print(f"  {link['link_id']}  {link['agent_name'] or link['agent_id']} -> {link['connector']}{note}")
    return 0


async def cmd_link(args: argparse.Namespace) -> int:
    """Grant one agent access to one connector."""
    from src.orchestrator.app import Orchestrator

    orchestrator = Orchestrator()
    try:
        link = await orchestrator.link_agent(args.agent_id, args.connector, note=args.note, created_by="cli")
    except KeyError as exc:
        print(f"kollektiv: {exc}", file=sys.stderr)
        print("agents    : kollektiv accounts   (or GET /agents/status)", file=sys.stderr)
        print("connectors: kollektiv connectors", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"kollektiv: {exc}", file=sys.stderr)
        return 1
    finally:
        await orchestrator.stop()
    if args.json:
        _print(link, True)
        return 0
    print(f"linked   : {link['agent_name'] or link['agent_id']} -> {link['connector']}  ({link['link_id']})")
    return 0


async def cmd_unlink(args: argparse.Namespace) -> int:
    """Revoke an agent-connector grant (by link id)."""
    from src.orchestrator.app import Orchestrator

    orchestrator = Orchestrator()
    try:
        removed = await orchestrator.unlink_agent(args.link_id)
    except KeyError as exc:
        print(f"kollektiv: {exc}", file=sys.stderr)
        print("list them: kollektiv links", file=sys.stderr)
        return 1
    finally:
        await orchestrator.stop()
    print(f"unlinked : {removed['agent_name'] or removed['agent_id']} -/-> {removed['connector']}")
    return 0


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

    sub.add_parser("init-db", help="Create the database schema")
    bootstrap = sub.add_parser(
        "bootstrap", help="Prepare the database/workspace and print the free-tier checklist"
    )
    bootstrap.add_argument("--json", action="store_true", help="Machine-readable report")
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
    run.add_argument("--agents", type=int, default=0, help="Number of worker agents (0 = .kollektiv.yml or 3)")
    run.add_argument("--concurrency", type=int, default=None, help="Max simultaneous agents")
    run.add_argument("--export-state", default="", help="Write PROJECT_STATE.md to this path")
    run.add_argument("--dry-run", action="store_true", help="Plan and estimate, dispatch nothing")
    run.add_argument("--allow-over-budget", action="store_true", help="Run even above a configured cost cap")
    run.add_argument("--json", action="store_true")

    estimate = sub.add_parser("estimate", help="Estimate a project's cost before running it")
    estimate.add_argument("--project-id", required=True)
    estimate.add_argument("--agents", type=int, default=0, help="Estimate a different agent count")
    estimate.add_argument("--json", action="store_true")

    budget = sub.add_parser("budget", help="Local spend ledger: tokens, dollars, caps")
    budget.add_argument("--json", action="store_true")

    init_config = sub.add_parser("init-config", help="Write a commented .kollektiv.yml")
    init_config.add_argument("--path", default="", help="Where to write it (default ./.kollektiv.yml)")
    init_config.add_argument("--force", action="store_true", help="Overwrite an existing file")

    keys = sub.add_parser("keys", help="Generate SECRET_KEY, SESSION_TOKEN and an admin gateway token")
    keys.add_argument("--env-file", default=".env", help="Env file to write (default: .env)")
    keys.add_argument("--rotate", action="store_true", help="Replace existing values instead of keeping them")
    keys.add_argument("--json", action="store_true")

    links = sub.add_parser("links", help="List agent-connector grants (who may use which service)")
    links.add_argument("--json", action="store_true")
    link = sub.add_parser("link", help="Grant one agent access to one connector")
    link.add_argument("agent_id", help="Account id from `kollektiv accounts`")
    link.add_argument("connector", help="Connector name from `kollektiv connectors`")
    link.add_argument("--note", default="", help="Why it was granted (shown in the dashboard)")
    link.add_argument("--json", action="store_true")
    unlink = sub.add_parser("unlink", help="Revoke an agent-connector grant (by link id)")
    unlink.add_argument("link_id", help="Link id from `kollektiv links`")

    status = sub.add_parser("status", help="Print a project's shared state")
    status.add_argument("project_id")
    status.add_argument("--json", action="store_true")

    sync = sub.add_parser("sync", help="Run the GitHub -> TeraBox sync once")
    sync.add_argument("--json", action="store_true")

    connectors = sub.add_parser("connectors", help="List the available service connectors")
    connectors.add_argument("--json", action="store_true", help="Machine-readable report")
    connectors.add_argument("--probe", action="store_true", help="Run a read-only reachability probe on each service")
    call = sub.add_parser("call", help="Call a connector action")
    call.add_argument("connector", help="Connector name (see `kollektiv connectors`)")
    call.add_argument("action", help="Action name")
    call.add_argument("--params", default="{}", help="JSON object of parameters")
    call.add_argument("--confirm", action="store_true", help="Allow dangerous actions")
    login = sub.add_parser("login", help="Register a worker on your own API key (BYOK)")
    login.add_argument("--provider", default="deepseek", help=f"One of: {', '.join(sorted(PROVIDER_PRESETS))}")
    login.add_argument("--name", default="", help="Display name for the worker (default: the provider)")
    login.add_argument("--account", default="", help="Stable id for the stored key (default: from --name)")
    login.add_argument("--token", default="", help="API key (otherwise prompted; KOLLEKTIV_TOKEN also works)")
    login.add_argument("--key-env", default="", help="Read the key from this variable, e.g. DEEPSEEK_API_KEY (nothing stored)")
    login.add_argument("--base-url", default="", help="Override the provider endpoint (required for custom)")
    login.add_argument("--model", default="", help="Override the provider's default model")
    login.add_argument("--env-file", default=".env", help="Env file that receives the ARENA_ACCOUNTS entry")
    login.add_argument("--json", action="store_true", help="Machine-readable report")
    logout = sub.add_parser("logout", help="Remove a worker's stored key and its ARENA_ACCOUNTS entry")
    logout.add_argument("--provider", default="deepseek", help="Provider of the worker to remove")
    logout.add_argument("--account", default="", help="Account id of the worker (default: the provider name)")
    logout.add_argument("--env-file", default=".env", help="Env file that holds ARENA_ACCOUNTS")
    accounts = sub.add_parser("accounts", help="List registered workers and stored keys (masked)")
    accounts.add_argument("--json", action="store_true", help="Machine-readable report")
    resume = sub.add_parser("resume", help="Print the resume briefing for a project")
    resume.add_argument("--project-id", required=True, help="Project identifier")
    resume.add_argument("--json", action="store_true", help="Structured output (no markdown)")
    resume.add_argument("--no-write", action="store_true", help="Do not refresh HANDOFF.md")
    sub.add_parser("secret", help="Print a new SECRET_KEY")

    gateway = sub.add_parser("gateway", help="MCP gateway: clients, tokens, policies, audit")
    gateway.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["status", "init", "serve", "token", "clients", "revoke", "policy", "presets", "tools", "audit"],
        help="What to do (default: status)",
    )
    gateway.add_argument("--name", default="", help="Client name (default: GATEWAY_DEFAULT_CLIENT)")
    gateway.add_argument("--label", default="", help="Human label for the client")
    gateway.add_argument(
        "--role",
        default="",
        choices=["admin", "dashboard", "worker", "messenger", "read-only", "client"],
        help="Policy preset for a new client",
    )
    gateway.add_argument("--rotate", action="store_true", help="Re-key an existing client instead of failing")
    gateway.add_argument("--env-file", default=".env", help="Env file to load before serving (default: .env)")
    gateway.add_argument("--preset", default="", help="Apply a policy preset (policy action)")
    gateway.add_argument("--allow", default="", help="Comma-separated allow globs")
    gateway.add_argument("--deny", default="", help="Comma-separated deny globs")
    gateway.add_argument("--confirm", default="", help="Comma-separated globs that need confirm=true")
    gateway.add_argument("--read-only", action="store_true", help="Refuse every tool that changes data")
    gateway.add_argument("--limit", type=int, default=0, help="How many audit rows to show")
    gateway.add_argument("--clear", action="store_true", help="With 'audit': delete the log")
    gateway.add_argument("--json", action="store_true", help="Machine-readable output")
    gateway.add_argument("--host", default=None, help="Serve host (default: GATEWAY_HOST)")
    gateway.add_argument("--port", type=int, default=None, help="Serve port (default: GATEWAY_PORT)")

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
    "connectors": cmd_connectors,
    "login": cmd_login,
    "logout": cmd_logout,
    "accounts": cmd_accounts,
    "gateway": cmd_gateway,
    "estimate": cmd_estimate,
    "budget": cmd_budget,
    "keys": cmd_keys,
    "links": cmd_links,
    "link": cmd_link,
    "unlink": cmd_unlink,
    "init-config": cmd_init_config,
    "resume": cmd_resume,
    "call": cmd_call,
    "bootstrap": cmd_bootstrap,
    "projects": cmd_projects,
    "plan": cmd_plan,
    "run": cmd_run,
    "status": cmd_status,
    "sync": cmd_sync,
    "secret": cmd_secret,
    "serve-api": cmd_serve_api,
    "serve-mcp": cmd_serve_mcp,
}


def _run_async(coro: Any) -> Any:
    """Run ``coro`` to completion, even when an event loop is already running.

    ``asyncio.run`` refuses to start inside a running loop, which is exactly the
    situation when the CLI is embedded (notebooks, tests, a host application
    that already owns a loop). In that case the coroutine is executed in a
    worker thread with its own loop.

    Args:
        coro: The coroutine to execute.

    Returns:
        Whatever the coroutine returns.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


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
            return int(_run_async(handler(args)) or 0)
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
