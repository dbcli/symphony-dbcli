from __future__ import annotations

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

    def fake_run(app: FastAPI, *, host: str, port: int) -> None:
        calls["host"] = host
        calls["port"] = port

    monkeypatch.setattr("symphony_dbcli.web.app.create_app", fake_create_app)
    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = cli.main(["--workflow", str(workflow_path), "serve"])

    assert result == 0
    assert calls["workflow_path"] == str(workflow_path)
    assert calls["database"] == str(tmp_path / "symphony.db")
    assert calls["store_path"] == str(tmp_path / "symphony.db")
    assert calls["run_runtime"] is True
    assert calls["host"] == "127.0.0.1"


def test_serve_web_runs_fastapi_without_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr("symphony_dbcli.web.app.create_app", fake_create_app)
    monkeypatch.setattr(uvicorn, "run", lambda app, *, host, port: None)

    result = cli.main(["--workflow", str(workflow_path), "serve-web"])

    assert result == 0
    assert calls["run_runtime"] is False


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
