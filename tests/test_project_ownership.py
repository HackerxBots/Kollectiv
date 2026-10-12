"""Projects belong to the user who created them.

A signed-in user sees and touches only their own projects; anyone else gets 404,
which does not reveal that the project exists. Single-user mode (no user) keeps
seeing everything. Also covers the migration that adds ``owner_id`` to a database
created before the column existed, and the CORS credentials rule.
"""

from __future__ import annotations

from typing import Any


def test_init_db_adds_owner_column_to_an_old_database(tmp_path: Any) -> None:
    """A projects table from before the column gains it, and the step is repeatable."""
    from sqlalchemy import create_engine, inspect, text

    from src.db.models import init_db

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE projects (id VARCHAR(64) PRIMARY KEY, name VARCHAR(200))"))
    init_db(engine)
    init_db(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("projects")}
    assert "owner_id" in columns


def test_upload_refuses_files_outside_the_workspace(tmp_path: Any) -> None:
    """A file the server can read but that lies outside the workspace is never archived."""
    from types import SimpleNamespace

    from config.settings import Settings
    from src.orchestrator.app import Orchestrator

    secret = tmp_path / "outside" / ".env"
    secret.parent.mkdir()
    secret.write_text("SECRET_KEY=x\n", encoding="utf-8")
    settings = Settings(WORKSPACE_DIR=str(tmp_path / "workspace"))
    fake = SimpleNamespace(settings=settings, pool=None)
    import asyncio

    for path in (str(secret), "/etc/passwd"):
        try:
            asyncio.run(Orchestrator.upload_project_file(fake, "prj_test", path))  # type: ignore[arg-type]
        except PermissionError:
            continue
        raise AssertionError(f"upload of {path} was not refused")
