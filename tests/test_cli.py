from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from symphony_dbcli import cli
from symphony_dbcli.config import DatabaseConfig, default_config, write_workflow


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
    assert os.environ["SYMPHONY_WORKFLOW"] == str(workflow_path)
    assert os.environ["SYMPHONY_PROFILE"] == "local"
    assert os.environ["SYMPHONY_RUN_RUNTIME"] == "1"


def test_serve_no_reload_runs_fastapi_with_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_path = _workflow(tmp_path)
    calls: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        calls["app"] = app
        calls.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = cli.main(["--workflow", str(workflow_path), "serve", "--no-reload"])

    assert result == 0
    assert calls["app"] == "symphony_dbcli.web.app:create_app_from_env"
    assert calls["factory"] is True
    assert calls["host"] == "127.0.0.1"
    assert "reload" not in calls
    assert os.environ["SYMPHONY_WORKFLOW"] == str(workflow_path)
    assert os.environ["SYMPHONY_PROFILE"] == "local"
    assert os.environ["SYMPHONY_RUN_RUNTIME"] == "1"


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

    def fake_run(app: str, **kwargs: object) -> None:
        calls["app"] = app
        calls.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = cli.main(["--workflow", str(workflow_path), "serve-web", "--no-reload"])

    assert result == 0
    assert calls["app"] == "symphony_dbcli.web.app:create_app_from_env"
    assert calls["factory"] is True
    assert "reload" not in calls
    assert os.environ["SYMPHONY_RUN_RUNTIME"] == "0"


def test_fastapi_reload_factory_loads_local_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_path = _workflow(tmp_path)
    env_dir = tmp_path / ".symphony"
    env_dir.mkdir()
    (env_dir / "github-app.env").write_text("SYMPHONY_GITHUB_APP_ID=123\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SYMPHONY_WORKFLOW", str(workflow_path))
    monkeypatch.delenv("SYMPHONY_GITHUB_APP_ID", raising=False)

    from symphony_dbcli.web.app import create_app_from_env

    app = create_app_from_env()

    assert isinstance(app, FastAPI)
    assert os.environ["SYMPHONY_GITHUB_APP_ID"] == "123"


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
