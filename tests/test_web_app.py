from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from symphony_dbcli.config import DatabaseConfig, default_config
from symphony_dbcli.store import Store
from symphony_dbcli.web.app import create_app


def test_fastapi_dashboard_exposes_navigation_and_board(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _add_source(client, "dbcli/litecli")

    response = client.get("/")

    assert response.status_code == 200
    assert "Symphony DBCLI" in response.text
    assert 'href="/sources"' in response.text
    assert 'href="/workflow"' in response.text
    assert "Backlog" in response.text
    assert "In Review" in response.text
    assert "dbcli/litecli" in response.text
    assert "Sync Source" in response.text
    assert "disabled" in response.text
    assert "auto dispatch" in response.text
    assert "data-theme-toggle" in response.text
    assert "Switch to dark mode" in response.text
    assert '<link rel="stylesheet" href="/web-static/web.css"' in response.text
    assert "<style>" not in response.text


def test_fastapi_dashboard_hierarchy_routes_render(tmp_path: Path) -> None:
    client = _client(tmp_path)

    routes = {
        "/board": "Board",
        "/sources": "Sources",
        "/sources/new": "Add Source",
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


def test_fastapi_board_empty_state_links_to_source_creation(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/board")

    assert response.status_code == 200
    assert "Add a source to start a board." in response.text
    assert 'href="/sources/new"' in response.text
    assert "Backlog" not in response.text


def test_fastapi_board_is_scoped_by_source(tmp_path: Path) -> None:
    client = _client(tmp_path)
    litecli_id = _add_source(client, "dbcli/litecli")
    pgcli_id = _add_source(client, "dbcli/pgcli")

    default_board = client.get("/board")
    litecli_board = client.get(f"/board?source_id={litecli_id}")
    pgcli_board = client.get(f"/board?source_id={pgcli_id}")

    assert default_board.status_code == 200
    assert "Board · dbcli/litecli" in default_board.text
    assert f'href="/board?source_id={litecli_id}"' in default_board.text
    assert f'href="/board?source_id={pgcli_id}"' in default_board.text
    assert 'aria-label="Work board for dbcli/litecli"' in litecli_board.text
    assert 'aria-label="Work board for dbcli/pgcli"' in pgcli_board.text
    assert "No backlog items" in pgcli_board.text


def test_fastapi_sources_can_be_added_and_listed(tmp_path: Path) -> None:
    client = _client(tmp_path)

    new_page = client.get("/sources/new")
    assert new_page.status_code == 200
    assert 'action="/sources"' in new_page.text

    response = client.post(
        "/sources",
        data={"repo": "dbcli/litecli"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/sources"

    sources = client.get("/sources")
    assert sources.status_code == 200
    assert "dbcli/litecli" in sources.text
    assert "never" in sources.text
    assert "Open Board" in sources.text


def test_fastapi_sources_reject_invalid_repo(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post("/sources", data={"repo": "not-a-repo"})

    assert response.status_code == 400
    assert "owner/name format" in response.text


def _client(tmp_path: Path) -> TestClient:
    config = replace(default_config(), database=DatabaseConfig(path=str(tmp_path / "symphony.db")))
    store = Store(config.database.path)
    store.init()
    return TestClient(create_app(config, store, workflow_path="WORKFLOW.md"))


def _add_source(client: TestClient, repo: str) -> int:
    response = client.post("/sources", data={"repo": repo}, follow_redirects=False)
    assert response.status_code == 303
    sources = client.get("/sources")
    marker = 'href="/board?source_id='
    start = sources.text.index(marker, sources.text.index(repo)) + len(marker)
    end = sources.text.index('"', start)
    return int(sources.text[start:end])
