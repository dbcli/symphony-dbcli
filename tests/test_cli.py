from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from symphony_dbcli import cli
from symphony_dbcli.config import DatabaseConfig, WorkflowConfig, default_config, write_workflow
from symphony_dbcli.store import Store


def test_serve_runs_fastapi_with_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_path = _workflow(tmp_path)
    calls: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        calls["app"] = app
        calls.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = cli.main(["--workflow", str(workflow_path), "serve"])

    assert result == 0
    assert calls["app"] == "symphony_dbcli.web.app:create_app_from_env"
    assert calls["factory"] is True
    assert calls["reload"] is True
    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 8765
    assert calls["reload_includes"] == ["*.py", "*.html", "*.css", "*.js"]
    reload_excludes = calls["reload_excludes"]
    assert isinstance(reload_excludes, list)
    assert str(workflow_path.parent / ".symphony") in reload_excludes
    assert str(workflow_path.parent / ".venv") in reload_excludes
    assert ".symphony/**" in reload_excludes
    assert ".venv/**" in reload_excludes
    assert calls["reload_dirs"] == [str(Path("src").resolve())]
    assert os.environ["SYMPHONY_WORKFLOW"] == str(workflow_path)
    assert os.environ["SYMPHONY_PROFILE"] == "local"
    assert os.environ["SYMPHONY_RUN_RUNTIME"] == "1"


def test_serve_no_reload_builds_fastapi_app_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_path = _workflow(tmp_path)
    calls: dict[str, object] = {}

    def fake_create_app(
        config: WorkflowConfig,
        store: Store,
        *,
        workflow_path: str,
        run_runtime: bool,
    ) -> FastAPI:
        calls["workflow_path"] = workflow_path
        calls["database"] = config.database.path
        calls["store_path"] = store.path
        calls["run_runtime"] = run_runtime
        return FastAPI()

    def fake_run(app: FastAPI, **kwargs: object) -> None:
        calls["app"] = app
        calls.update(kwargs)

    monkeypatch.setattr("symphony_dbcli.web.app.create_app", fake_create_app)
    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = cli.main(["--workflow", str(workflow_path), "serve", "--no-reload"])

    assert result == 0
    assert isinstance(calls["app"], FastAPI)
    assert calls["workflow_path"] == str(workflow_path)
    assert calls["database"] == str(tmp_path / "symphony.db")
    assert calls["store_path"] == str(tmp_path / "symphony.db")
    assert calls["run_runtime"] is True
    assert calls["host"] == "127.0.0.1"
    assert "reload" not in calls


def test_serve_web_runs_fastapi_without_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_path = _workflow(tmp_path)
    calls: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        calls["app"] = app
        calls.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = cli.main(["--workflow", str(workflow_path), "serve-web"])

    assert result == 0
    assert calls["app"] == "symphony_dbcli.web.app:create_app_from_env"
    assert calls["factory"] is True
    assert calls["reload"] is True
    assert os.environ["SYMPHONY_RUN_RUNTIME"] == "0"


def test_serve_web_no_reload_runs_fastapi_without_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_path = _workflow(tmp_path)
    calls: dict[str, object] = {}

    def fake_create_app(
        config: WorkflowConfig,
        store: Store,
        *,
        workflow_path: str,
        run_runtime: bool,
    ) -> FastAPI:
        calls["run_runtime"] = run_runtime
        return FastAPI()

    def fake_run(app: FastAPI, **kwargs: object) -> None:
        calls["app"] = app
        calls.update(kwargs)

    monkeypatch.setattr("symphony_dbcli.web.app.create_app", fake_create_app)
    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = cli.main(["--workflow", str(workflow_path), "serve-web", "--no-reload"])

    assert result == 0
    assert calls["run_runtime"] is False
    assert isinstance(calls["app"], FastAPI)
    assert "reload" not in calls


def _workflow(tmp_path: Path) -> Path:
    workflow_path = tmp_path / "WORKFLOW.md"
    config = replace(default_config(), database=DatabaseConfig(path=str(tmp_path / "symphony.db")))
    write_workflow(workflow_path, config)
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(
            'path = ".symphony/symphony.db"',
            f'path = "{(tmp_path / "symphony.db").as_posix()}"',
        ),
        encoding="utf-8",
    )
    return workflow_path
