from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from symphony_dbcli.config import DatabaseConfig, default_config
from symphony_dbcli.store import Store
from symphony_dbcli.web.app import create_app


def test_fastapi_dashboard_exposes_navigation_and_board(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert "Symphony DBCLI" in response.text
    assert 'href="/sources"' in response.text
    assert 'href="/workflow"' in response.text
    assert "Backlog" in response.text
    assert "In Review" in response.text
    assert '<link rel="stylesheet" href="/web-static/web.css"' in response.text
    assert "<style>" not in response.text


def test_fastapi_dashboard_hierarchy_routes_render(tmp_path: Path) -> None:
    client = _client(tmp_path)

    routes = {
        "/board": "Board",
        "/sources": "Sources",
        "/work-items": "Work Items",
        "/work-items/42": "Work Item #42",
        "/workers": "Workers",
        "/workflow": "Workflow",
        "/workflow/edit": "Workflow Editor",
        "/ask": "Ask Symphony",
        "/settings": "Settings",
    }

    for path, expected in routes.items():
        response = client.get(path)
        assert response.status_code == 200, path
        assert expected in response.text


def test_fastapi_health_reports_runtime_context(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "profile": "local",
        "database": str(tmp_path / "symphony.db"),
        "workflow_path": "WORKFLOW.md",
    }


def test_fastapi_favicon_does_not_generate_console_noise(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/favicon.ico")

    assert response.status_code == 204


def _client(tmp_path: Path) -> TestClient:
    config = replace(default_config(), database=DatabaseConfig(path=str(tmp_path / "symphony.db")))
    store = Store(config.database.path)
    store.init()
    return TestClient(create_app(config, store, workflow_path="WORKFLOW.md"))
