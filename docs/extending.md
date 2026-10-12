# Extending Kollektiv

> Part of the [Kollektiv documentation](README.md) · back to the [README](../README.md)

## Extending Kollektiv

**A new worker shape.** Subclass `ArenaClient` and override `_build_request()`
(or point `base_url` at an OpenAI-compatible gateway) — the pool needs nothing
else. Anything that accepts a prompt and returns text can be a worker.

**A new brain provider.** Any OpenAI-compatible endpoint works by setting
`BRAIN_BASE_URL`/`BRAIN_MODEL`. For a different protocol, implement `complete()`
on a subclass of `OrchestratorBrain` and pass it to `Orchestrator(brain=…)`.

**A new storage backend.** `R2Storage`/`TeraBoxPoolManager` expose
`upload_file/download_file/list_all_files/get_total_quota` (plus
`get_file_url`/`get_status`); implement the same methods — WebDAV, a NAS, B2 —
and pass it as `pool=` or add a branch to `build_storage()`.

**A new tool.** Add a function decorated with `@server.tool()` inside
`create_server()` in `src/api/mcp_server.py` — the SDK generates the schema from
the type hints.

---
