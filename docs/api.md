# API, MCP and CLI

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

## Interfaces

### HTTP API

Run it with `kollektiv serve-api`, `kollektiv-api`, or
`uvicorn src.api.routes:app --port 8000`. Interactive docs at `/docs`.

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| `GET` | `/health` | — | `{status, subsystems, warnings}` — never fails when degraded |
| `POST` | `/projects` | `{name, description, n_agents}` | `201 {project_id, name, plan}` |
| `GET` | `/projects` | — | `{count, projects: [{project_id, name, status, …}]}` |
| `POST` | `/projects/{id}/run` | `?background=&max_concurrency=` | `{status, tasks_dispatched, completed, failed, artifact}` |
| `POST` | `/projects/{id}/replan` | `?dispatch=&max_new_tasks=` | `{revision, new_tasks, plan, results}` |
| `GET` | `/projects/{id}/status` | `?force=` | The state document as JSON |
| `GET` | `/projects/{id}/files` | — | `[{path, size, source, …}]` |
| `POST` | `/projects/{id}/upload` | `{file_path}` or multipart | `{path, size, url}` |
| `GET` | `/agents/status` | `?probe=true` | `{count, available, agents:[…]}` |
| `GET` | `/storage/status` | — | `{used_gb, free_gb, total_gb, per_account}` |
| `POST` | `/sync` | — | `{commits, prs, archived, errors, state_updated}` |
| `POST` | `/webhooks/github` | GitHub payload + HMAC header | `200`/`202`, or `401` when unsigned |

Error handling is uniform: `404` for unknown projects, `400` for configuration
problems (no agents configured, empty description) and `502` when an upstream
system fails. Everything is logged with the project id and the failing
subsystem.

### MCP server

`kollektiv serve-mcp`, `kollektiv-mcp`, or `python -m src.api.mcp_server`.
Supports `mcp` SDK v1 (`FastMCP`) and v2 (`MCPServer`).

| Tool | Arguments | Result |
| --- | --- | --- |
| `list_projects` | — | Projects with status and task counts |
| `get_project_status` | `project_id` | The shared state document |
| `create_project` | `name`, `description`, `n_agents` | New project + plan |
| `run_project` | `project_id`, `max_concurrency` | Run summary |
| `replan_project` | `project_id`, `dispatch` | Corrective plan |
| `list_files` | `project_id` | Stored artifacts |
| `upload_file` | `project_id`, `file_path` | TeraBox location + URL |
| `get_agent_pool_status` | `probe` | Worker pool snapshot |
| `get_storage_status` | — | Pooled quota |
| `trigger_sync` | — | Sync pass result |

```bash
python -m src.api.mcp_server --transport stdio                 # local clients
python -m src.api.mcp_server --transport sse --port 8001       # remote clients
python -m src.api.mcp_server --transport streamable-http       # 2025+ clients
```

Example client configuration (Claude Desktop style):

```json
{
  "mcpServers": {
    "kollektiv": {
      "command": "python",
      "args": ["-m", "src.api.mcp_server", "--transport", "stdio"],
      "env": {"DATABASE_URL": "sqlite:////absolute/path/data/kollektiv.db"}
    }
  }
}
```

### CLI

```
kollektiv check [--json] [--live]     validate configuration; --live probes the APIs
kollektiv init-db                     create the SQLite schema
kollektiv projects                    list projects
kollektiv plan "<brief>" --agents 3   plan without running
kollektiv run "<brief>"|--project-id  plan (if needed) and execute
       [--name] [--agents] [--max-concurrency] [--export-state PATH]
kollektiv status --project-id prj_…   print the shared state document
kollektiv sync                        one sync pass (GitHub → TeraBox → agents)
kollektiv secret                      print a fresh Fernet key for SECRET_KEY
kollektiv serve-api [--host] [--port] [--reload]
kollektiv serve-mcp [--transport] [--host] [--port]
```

`--log-level DEBUG` is available globally, and `kollektiv check --live` is the
fastest way to prove that tokens, buckets and the brain actually work.

---
