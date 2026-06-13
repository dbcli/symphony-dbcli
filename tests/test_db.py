from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from sqlalchemy import Engine, text
from sqlalchemy import inspect as sqlalchemy_inspect

from symphony_dbcli.cli import main
from symphony_dbcli.config import DatabaseConfig, default_config, write_workflow
from symphony_dbcli.db import create_db_engine, create_session_factory, sqlite_url
from symphony_dbcli.models import create_model_tables
from symphony_dbcli.work_items import WorkItemRepository


def test_sqlite_url_handles_memory_relative_and_absolute_paths(tmp_path: Path) -> None:
    assert sqlite_url(":memory:") == "sqlite+pysqlite:///:memory:"
    assert sqlite_url(".symphony/symphony.db") == "sqlite+pysqlite:///.symphony/symphony.db"
    assert sqlite_url(str(tmp_path / "symphony.db")).startswith("sqlite+pysqlite:////")


def test_engine_factory_creates_parent_directory(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "symphony.db"

    engine = create_db_engine(str(database_path))
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        assert session.bind is engine
    assert database_path.parent.exists()


def test_model_table_creation_repairs_existing_sqlite_columns() -> None:
    engine = create_db_engine(":memory:")
    _create_legacy_work_item_tables(engine)

    create_model_tables(engine)

    _assert_work_item_columns_repaired(engine)
    assert WorkItemRepository(create_session_factory(engine)).list_operations() == []


def test_init_db_repairs_existing_model_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "symphony.db"
    workflow_path = tmp_path / "WORKFLOW.md"
    config = replace(default_config(), database=DatabaseConfig(path=str(database_path)))
    write_workflow(workflow_path, config)
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(
            'path = ".symphony/symphony.db"',
            f'path = "{database_path.as_posix()}"',
        ),
        encoding="utf-8",
    )
    engine = create_db_engine(str(database_path))
    _create_legacy_work_item_tables(engine)

    result = main(["--workflow", str(workflow_path), "init-db"])

    assert result == 0
    _assert_work_item_columns_repaired(engine)


def _create_legacy_work_item_tables(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE work_items (
                    id INTEGER PRIMARY KEY,
                    source_id INTEGER NOT NULL,
                    primary_source_item_id INTEGER NOT NULL,
                    title VARCHAR(500) NOT NULL,
                    state VARCHAR(32) NOT NULL,
                    task_type VARCHAR(32) NOT NULL,
                    user_hint TEXT NOT NULL,
                    outcome VARCHAR(64) NOT NULL,
                    created_at VARCHAR(32) NOT NULL,
                    updated_at VARCHAR(32) NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE work_item_runs (
                    id INTEGER PRIMARY KEY,
                    work_item_id INTEGER NOT NULL,
                    task_type VARCHAR(32) NOT NULL,
                    "trigger" VARCHAR(32) NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    reasons_json TEXT NOT NULL,
                    user_hint TEXT NOT NULL,
                    started_at VARCHAR(32),
                    completed_at VARCHAR(32),
                    created_at VARCHAR(32) NOT NULL,
                    updated_at VARCHAR(32) NOT NULL
                )
                """
            )
        )


def _assert_work_item_columns_repaired(engine: Engine) -> None:
    inspector = sqlalchemy_inspect(engine)
    work_item_columns = {column["name"] for column in inspector.get_columns("work_items")}
    run_columns = {column["name"] for column in inspector.get_columns("work_item_runs")}
    assert {"active_pr_source_item_id", "disposition", "disposition_note", "disposition_at"} <= (
        work_item_columns
    )
    assert {"attempt_id", "workflow_instance_id", "source_attempt_id", "codex_thread_id"} <= run_columns
