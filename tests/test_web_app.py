from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from symphony_dbcli.config import DatabaseConfig, default_config
from symphony_dbcli.github import GitHubIssue, PullRequest
from symphony_dbcli.sources import SourceSyncClient
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


def test_fastapi_source_sync_populates_selected_board_backlog(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")

    response = client.post(f"/sources/{source_id}/sync", follow_redirects=False)
    board = client.get(f"/board?source_id={source_id}")
    sources = client.get("/sources")

    assert response.status_code == 303
    assert response.headers["location"] == f"/board?source_id={source_id}&sync=succeeded"
    assert "synced" in sources.text
    assert "Fix completion crash" in board.text
    assert "Improve docs" in board.text
    assert "#245" in board.text
    assert "#8" in board.text
    assert "<span>2</span>" in board.text
    assert "No backlog items" not in board.text


def test_fastapi_source_item_activation_creates_todo_work_item(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")

    form = client.get(f"/source-items/{source_item_id}/activate")
    response = client.post(
        f"/source-items/{source_item_id}/activate",
        data={"task_type": "code", "user_hint": "Prefer unit tests."},
        follow_redirects=False,
    )
    board = client.get(f"/board?source_id={source_id}")
    work_items = client.get("/work-items")
    detail = client.get("/work-items/1")

    assert form.status_code == 200
    assert "Queue Work" in form.text
    assert "Fix completion crash" in form.text
    assert response.status_code == 303
    assert response.headers["location"] == f"/board?source_id={source_id}"
    assert "No backlog items" not in board.text
    assert "No todo items" not in board.text
    assert "work item #1" in board.text
    assert "code" in board.text
    assert "Fix completion crash" in work_items.text
    assert "Prefer unit tests." in detail.text


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
    assert 'action="/sources/' in sources.text
    assert "Open Board" in sources.text


def test_fastapi_sources_reject_invalid_repo(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post("/sources", data={"repo": "not-a-repo"})

    assert response.status_code == 400
    assert "owner/name format" in response.text


def _client(tmp_path: Path, source_sync_client: SourceSyncClient | None = None) -> TestClient:
    config = replace(default_config(), database=DatabaseConfig(path=str(tmp_path / "symphony.db")))
    store = Store(config.database.path)
    store.init()
    return TestClient(
        create_app(
            config,
            store,
            workflow_path="WORKFLOW.md",
            source_sync_client=source_sync_client,
        )
    )


def _add_source(client: TestClient, repo: str) -> int:
    response = client.post("/sources", data={"repo": repo}, follow_redirects=False)
    assert response.status_code == 303
    sources = client.get("/sources")
    marker = 'href="/board?source_id='
    start = sources.text.index(marker, sources.text.index(repo)) + len(marker)
    end = sources.text.index('"', start)
    return int(sources.text[start:end])


def _sync_source(client: TestClient, source_id: int) -> None:
    response = client.post(f"/sources/{source_id}/sync", follow_redirects=False)
    assert response.status_code == 303


def _source_item_id_for(client: TestClient, source_id: int, title: str) -> int:
    board = client.get(f"/board?source_id={source_id}")
    marker = 'href="/source-items/'
    start = board.text.index(marker, board.text.index(title)) + len(marker)
    end = board.text.index("/activate", start)
    return int(board.text[start:end])


class FakeSourceSyncClient:
    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]:
        return [
            GitHubIssue(
                repo=repo,
                number=245,
                title="Fix completion crash",
                body="issue body",
                url=f"https://github.com/{repo}/issues/245",
                state="open",
                labels=["bug"],
                author="alice",
                updated_at="2026-05-25T01:00:00Z",
            )
        ]

    def list_pull_requests(self, repo: str, *, state: str = "open") -> list[PullRequest]:
        return [
            PullRequest(
                number=8,
                url=f"https://github.com/{repo}/pull/8",
                title="Improve docs",
                state=state,
                author="bob",
                updated_at="2026-05-25T02:00:00Z",
                body="pr body",
            )
        ]
