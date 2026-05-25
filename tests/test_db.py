from __future__ import annotations

from pathlib import Path

from symphony_dbcli.db import create_db_engine, create_session_factory, sqlite_url


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
