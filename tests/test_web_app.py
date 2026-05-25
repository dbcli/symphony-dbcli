from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from symphony_dbcli.config import DatabaseConfig, default_config
from symphony_dbcli.db import create_db_engine, create_session_factory
from symphony_dbcli.github import GitHubIssue, PullRequest
from symphony_dbcli.models import (
    SourceItem,
    SourceItemLink,
    WorkItem,
    WorkItemLink,
    WorkItemRun,
    WorkItemStateEvent,
)
from symphony_dbcli.review_actions import issue_link_marker
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
    assert 'name="backlog_q"' in response.text
    assert 'name="todo_q"' in response.text
    assert "dbcli/litecli" in response.text
    assert "Sync Source" in response.text
    assert "auto dispatch" in response.text
    assert "data-theme-toggle" in response.text
    assert "Switch to dark mode" in response.text
    assert '<link rel="stylesheet" href="/web-static/web.css"' in response.text
    assert '<script src="/web-static/vendor/htmx.min.js"' in response.text
    assert '<script src="/web-static/vendor/sortable.min.js"' in response.text
    assert "<style>" not in response.text


def test_fastapi_dashboard_hierarchy_routes_render(tmp_path: Path) -> None:
    client = _client(tmp_path)

    routes = {
        "/board": "Board",
        "/sources": "Sources",
        "/sources/new": "Add Source",
        "/work-items": "Work Items",
        "/operations": "Operations",
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
    assert f'href="/board/source/{litecli_id}"' in default_board.text
    assert f'href="/board/source/{pgcli_id}"' in default_board.text
    assert 'aria-label="Work board for dbcli/litecli"' in litecli_board.text
    assert 'aria-label="Work board for dbcli/pgcli"' in pgcli_board.text
    assert "No backlog items" in pgcli_board.text


def test_fastapi_board_search_preserves_selected_source(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=RepoScopedSearchSyncClient())
    fixture_id = _add_source(client, "amjith/symphony-dbcli-e2e-fixture")
    litecli_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, fixture_id)
    _sync_source(client, litecli_id)

    default_board = client.get("/board")
    litecli_board = client.get(f"/board/source/{litecli_id}")
    search = client.get(f"/board/source/{litecli_id}?backlog_q=completion")

    assert "Board · amjith/symphony-dbcli-e2e-fixture" in default_board.text
    assert f'action="/board/source/{litecli_id}"' in litecli_board.text
    assert search.status_code == 200
    assert "Board · dbcli/litecli" in search.text
    assert 'aria-label="Work board for dbcli/litecli"' in search.text
    assert "LiteCLI completion search hit" in search.text
    assert "Fixture default issue" not in search.text


def test_fastapi_source_sync_populates_selected_board_backlog(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")

    response = client.post(f"/sources/{source_id}/sync", follow_redirects=False)
    board = client.get(f"/board?source_id={source_id}")
    sources = client.get("/sources")

    assert response.status_code == 303
    assert response.headers["location"] == f"/board/source/{source_id}?sync=succeeded"
    assert "synced" in sources.text
    assert "Fix completion crash" in board.text
    assert "Improve docs" in board.text
    assert "#245" in board.text
    assert "#8" in board.text
    assert "<span>2</span>" in board.text
    assert "No backlog items" not in board.text


def test_fastapi_board_paginates_backlog_by_latest_github_update(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=ManyBacklogSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)

    first_page = client.get(f"/board?source_id={source_id}")
    second_page = client.get(f"/board?source_id={source_id}&backlog_page=2")

    assert first_page.status_code == 200
    assert "1-20 of 25" in first_page.text
    assert "Backlog issue 025" in first_page.text
    assert "Backlog issue 006" in first_page.text
    assert "Backlog issue 005" not in first_page.text
    assert "backlog_page=2" in first_page.text
    assert second_page.status_code == 200
    assert "21-25 of 25" in second_page.text
    assert "Backlog issue 005" in second_page.text
    assert "Backlog issue 001" in second_page.text
    assert "Backlog issue 006" not in second_page.text


def test_fastapi_board_searches_backlog_with_sqlite_fts_body_matches(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=SearchableSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)

    board = client.get(f"/board?source_id={source_id}&backlog_q=ftsneedle")

    assert board.status_code == 200
    assert "Cache cleanup" in board.text
    assert "Another bug" not in board.text
    assert 'value="ftsneedle"' in board.text
    assert "1-1 of 1" in board.text


def test_fastapi_board_searches_work_columns_with_sqlite_fts(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=SearchableSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    matching_source_item_id = _source_item_id_for(client, source_id, "Cache cleanup")
    other_source_item_id = _source_item_id_for(client, source_id, "Another bug")
    _activate_source_item(client, matching_source_item_id, task_type="code")
    _activate_source_item(client, other_source_item_id, task_type="code")

    board = client.get(f"/board?source_id={source_id}&todo_q=ftsneedle")

    assert board.status_code == 200
    assert "Cache cleanup" in board.text
    assert "Another bug" not in board.text
    assert 'value="ftsneedle"' in board.text
    assert "work item #1" in board.text
    assert "work item #2" not in board.text


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
    assert response.headers["location"] == f"/board/source/{source_id}"
    assert "No backlog items" not in board.text
    assert "No todo items" not in board.text
    assert "work item #1" in board.text
    assert 'data-work-item-id="1"' in board.text
    assert "code" in board.text
    assert "Fix completion crash" in work_items.text
    assert "Prefer unit tests." in detail.text


def test_fastapi_operations_page_lists_operation_runs(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")

    _activate_source_item(
        client,
        source_item_id,
        task_type="operations",
        user_hint="Restart the fixture service.",
    )
    response = client.get("/operations")

    assert response.status_code == 200
    assert "Fix completion crash" in response.text
    assert "Restart the fixture service." in response.text
    assert 'href="/work-items/1"' in response.text


def test_fastapi_work_item_move_records_review_rerun_reasons(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")
    _activate_source_item(client, source_item_id, task_type="code")

    review_move = client.post(
        "/work-items/1/move",
        data={"target_state": "in_review"},
        follow_redirects=False,
    )
    rerun_move = client.post(
        "/work-items/1/move",
        data={
            "target_state": "in_progress",
            "reasons": ["address_pr_comments", "fix_ci"],
            "note": "Reviewer asked for tests.",
        },
        follow_redirects=False,
    )
    detail = client.get("/work-items/1")
    board = client.get(f"/board?source_id={source_id}")
    events, runs = _work_item_events_and_runs(tmp_path)

    assert review_move.status_code == 303
    assert rerun_move.status_code == 303
    assert "In Progress" in detail.text
    assert "Reviewer asked for tests." not in detail.text
    assert "work item #1" in board.text
    assert "No in progress items" not in board.text
    assert [event.to_state for event in events[-2:]] == ["in_review", "in_progress"]
    assert runs[-1].status == "queued"
    assert runs[-1].trigger == "rerun"
    assert runs[-1].user_hint == "Reviewer asked for tests."
    assert json.loads(runs[-1].reasons_json) == ["address_pr_comments", "fix_ci"]


def test_fastapi_groups_linked_issue_pr_and_selects_active_pr(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=LinkedPullRequestSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Linked issue")

    board = client.get(f"/board?source_id={source_id}")
    form = client.get(f"/source-items/{source_item_id}/activate")
    _activate_source_item(client, source_item_id, task_type="code")
    detail = client.get("/work-items/1")
    source_links, work_item, work_item_links = _linked_pr_records(tmp_path)

    assert "1 linked PR" in board.text
    assert "Linked PR" in board.text
    assert "Review/fix" in board.text
    assert "Unlinked PR" in board.text
    assert '<option value="code" selected>Code</option>' in form.text
    assert "1 linked PR" in form.text
    assert "Active PR" in detail.text
    assert "PR #12" in detail.text
    assert work_item.active_pr_source_item_id == source_links[0].linked_source_item_id
    assert {link.relationship for link in work_item_links} == {"primary_issue", "linked_pr", "active_pr"}


def test_fastapi_sync_attaches_newly_linked_pr_to_active_issue_work_item(tmp_path: Path) -> None:
    sync_client = LinkedPullRequestSyncClient(linked=False)
    client = _client(tmp_path, source_sync_client=sync_client)
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Linked issue")
    _activate_source_item(client, source_item_id, task_type="code")

    sync_client.linked = True
    _sync_source(client, source_id)
    detail = client.get("/work-items/1")
    source_links, work_item, work_item_links = _linked_pr_records(tmp_path)

    assert "Active PR" in detail.text
    assert work_item.active_pr_source_item_id == source_links[0].linked_source_item_id
    assert {link.relationship for link in work_item_links} == {"primary_issue", "linked_pr", "active_pr"}


def test_fastapi_source_item_ignore_hides_backlog_card(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")

    response = client.post(
        f"/source-items/{source_item_id}/ignore",
        data={"note": "Not relevant right now."},
        follow_redirects=False,
    )
    board = client.get(f"/board?source_id={source_id}")
    source_item = _source_item(tmp_path, source_item_id)

    assert response.status_code == 303
    assert "Fix completion crash" not in board.text
    assert source_item.disposition == "ignored"
    assert source_item.disposition_note == "Not relevant right now."


def test_fastapi_archive_work_item_hides_card(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")
    _activate_source_item(client, source_item_id, task_type="code")

    response = client.post(
        "/work-items/1/archive",
        data={"note": "Handled elsewhere."},
        follow_redirects=False,
    )
    board = client.get(f"/board?source_id={source_id}")
    work_item = _work_item(tmp_path, 1)

    assert response.status_code == 303
    assert "work item #1" not in board.text
    assert work_item.state == "done"
    assert work_item.disposition == "archived"
    assert work_item.outcome == "archived_by_user"


def test_fastapi_sync_marks_work_item_done_when_issue_closes(tmp_path: Path) -> None:
    sync_client = ClosableIssueSyncClient()
    client = _client(tmp_path, source_sync_client=sync_client)
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Closable issue")
    _activate_source_item(client, source_item_id, task_type="code")

    sync_client.open_issue = False
    _sync_source(client, source_id)
    board = client.get(f"/board?source_id={source_id}")
    work_item = _work_item(tmp_path, 1)

    assert "Done" in board.text
    assert "work item #1" in board.text
    assert work_item.state == "done"
    assert work_item.outcome == "issue_closed_external"


def test_fastapi_source_filters_can_be_edited_and_applied_to_sync(tmp_path: Path) -> None:
    sync_client = FilteredSourceSyncClient()
    client = _client(tmp_path, source_sync_client=sync_client)
    source_id = _add_source(client, "dbcli/litecli")

    edit = client.get(f"/sources/{source_id}/edit")
    update = client.post(
        f"/sources/{source_id}",
        data={
            "display_name": "LiteCLI filtered",
            "enabled": "true",
            "labels": "bug, support",
            "authors": "alice",
            "updated_after": "2026-05-01",
            "updated_before": "",
            "stale_after_days": "",
        },
        follow_redirects=False,
    )
    sources = client.get("/sources")
    sync = client.post(f"/sources/{source_id}/sync", follow_redirects=False)
    board = client.get(f"/board?source_id={source_id}")

    assert edit.status_code == 200
    assert "Edit Source" in edit.text
    assert update.status_code == 303
    assert update.headers["location"] == "/sources"
    assert sync_client.issue_labels == ["bug", "support"]
    assert sync.status_code == 303
    assert "LiteCLI filtered" in sources.text
    assert "labels: bug, support" in sources.text
    assert "authors: alice" in sources.text
    assert "Matching issue" in board.text
    assert "Matching PR" in board.text
    assert "Wrong author" not in board.text
    assert "Wrong label" not in board.text


def test_fastapi_source_filter_validation_returns_edit_form(tmp_path: Path) -> None:
    client = _client(tmp_path)
    source_id = _add_source(client, "dbcli/litecli")

    response = client.post(
        f"/sources/{source_id}",
        data={
            "display_name": "dbcli/litecli",
            "enabled": "true",
            "updated_after": "05/01/2026",
        },
    )

    assert response.status_code == 400
    assert "Updated after must use YYYY-MM-DD." in response.text
    assert "05/01/2026" in response.text


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


def test_fastapi_ask_renders_inline_board_answer(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _add_source(client, "dbcli/litecli")

    response = client.get("/ask?q=What%20is%20the%20board%20status%3F")

    assert response.status_code == 200
    assert "Board status across 1 source(s)" in response.text
    assert 'href="/board"' in response.text
    assert 'href="/work-items"' in response.text


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
    marker = 'href="/board/source/'
    start = sources.text.index(marker, sources.text.index(repo)) + len(marker)
    end = sources.text.index('"', start)
    return int(sources.text[start:end])


def _sync_source(client: TestClient, source_id: int) -> None:
    response = client.post(f"/sources/{source_id}/sync", follow_redirects=False)
    assert response.status_code == 303


def _activate_source_item(
    client: TestClient,
    source_item_id: int,
    *,
    task_type: str = "research",
    user_hint: str = "",
) -> None:
    response = client.post(
        f"/source-items/{source_item_id}/activate",
        data={"task_type": task_type, "user_hint": user_hint},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _source_item_id_for(client: TestClient, source_id: int, title: str) -> int:
    board = client.get(f"/board?source_id={source_id}")
    marker = 'href="/source-items/'
    start = board.text.index(marker, board.text.index(title)) + len(marker)
    end = board.text.index("/activate", start)
    return int(board.text[start:end])


def _work_item_events_and_runs(tmp_path: Path) -> tuple[list[WorkItemStateEvent], list[WorkItemRun]]:
    session_factory = create_session_factory(create_db_engine(str(tmp_path / "symphony.db")))
    with session_factory() as session:
        events = list(session.scalars(select(WorkItemStateEvent).order_by(WorkItemStateEvent.id.asc())))
        runs = list(session.scalars(select(WorkItemRun).order_by(WorkItemRun.id.asc())))
    return events, runs


def _linked_pr_records(tmp_path: Path) -> tuple[list[SourceItemLink], WorkItem, list[WorkItemLink]]:
    session_factory = create_session_factory(create_db_engine(str(tmp_path / "symphony.db")))
    with session_factory() as session:
        source_links = list(session.scalars(select(SourceItemLink).order_by(SourceItemLink.id.asc())))
        work_item = session.scalars(select(WorkItem)).one()
        work_item_links = list(session.scalars(select(WorkItemLink).order_by(WorkItemLink.id.asc())))
    return source_links, work_item, work_item_links


def _source_item(tmp_path: Path, source_item_id: int) -> SourceItem:
    session_factory = create_session_factory(create_db_engine(str(tmp_path / "symphony.db")))
    with session_factory() as session:
        return session.scalars(select(SourceItem).where(SourceItem.id == source_item_id)).one()


def _work_item(tmp_path: Path, work_item_id: int) -> WorkItem:
    session_factory = create_session_factory(create_db_engine(str(tmp_path / "symphony.db")))
    with session_factory() as session:
        return session.scalars(select(WorkItem).where(WorkItem.id == work_item_id)).one()


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


class ManyBacklogSyncClient:
    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]:
        return [
            GitHubIssue(
                repo=repo,
                number=number,
                title=f"Backlog issue {number:03d}",
                body=f"Body for backlog issue {number:03d}",
                url=f"https://github.com/{repo}/issues/{number}",
                state="open",
                labels=["bug"],
                author="alice",
                updated_at=f"2026-05-{number:02d}T01:00:00Z",
            )
            for number in range(1, 26)
        ]

    def list_pull_requests(self, repo: str, *, state: str = "open") -> list[PullRequest]:
        return []


class SearchableSourceSyncClient:
    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]:
        return [
            GitHubIssue(
                repo=repo,
                number=245,
                title="Cache cleanup",
                body="The issue contents mention ftsneedle only in the body.",
                url=f"https://github.com/{repo}/issues/245",
                state="open",
                labels=["bug"],
                author="alice",
                updated_at="2026-05-25T01:00:00Z",
            ),
            GitHubIssue(
                repo=repo,
                number=246,
                title="Another bug",
                body="Plain issue body.",
                url=f"https://github.com/{repo}/issues/246",
                state="open",
                labels=["bug"],
                author="bob",
                updated_at="2026-05-24T01:00:00Z",
            ),
        ]

    def list_pull_requests(self, repo: str, *, state: str = "open") -> list[PullRequest]:
        return []


class RepoScopedSearchSyncClient:
    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]:
        if repo == "dbcli/litecli":
            return [
                GitHubIssue(
                    repo=repo,
                    number=245,
                    title="LiteCLI completion search hit",
                    body="completion search should stay scoped to litecli",
                    url=f"https://github.com/{repo}/issues/245",
                    state="open",
                    labels=["bug"],
                    author="alice",
                    updated_at="2026-05-25T01:00:00Z",
                )
            ]
        return [
            GitHubIssue(
                repo=repo,
                number=3,
                title="Fixture default issue",
                body="fixture-only backlog card",
                url=f"https://github.com/{repo}/issues/3",
                state="open",
                labels=["bug"],
                author="bob",
                updated_at="2026-05-24T01:00:00Z",
            )
        ]

    def list_pull_requests(self, repo: str, *, state: str = "open") -> list[PullRequest]:
        return []


class FilteredSourceSyncClient:
    def __init__(self) -> None:
        self.issue_labels: list[str] | None = None

    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]:
        self.issue_labels = labels
        return [
            GitHubIssue(
                repo=repo,
                number=245,
                title="Matching issue",
                body="issue body",
                url=f"https://github.com/{repo}/issues/245",
                state="open",
                labels=["bug", "support"],
                author="alice",
                updated_at="2026-05-25T01:00:00Z",
            ),
            GitHubIssue(
                repo=repo,
                number=246,
                title="Wrong author",
                body="issue body",
                url=f"https://github.com/{repo}/issues/246",
                state="open",
                labels=["bug", "support"],
                author="mallory",
                updated_at="2026-05-25T01:00:00Z",
            ),
            GitHubIssue(
                repo=repo,
                number=247,
                title="Wrong label",
                body="issue body",
                url=f"https://github.com/{repo}/issues/247",
                state="open",
                labels=["bug"],
                author="alice",
                updated_at="2026-05-25T01:00:00Z",
            ),
        ]

    def list_pull_requests(self, repo: str, *, state: str = "open") -> list[PullRequest]:
        return [
            PullRequest(
                number=8,
                url=f"https://github.com/{repo}/pull/8",
                title="Matching PR",
                state="open",
                author="alice",
                labels=["bug", "support"],
                updated_at="2026-05-25T02:00:00Z",
                body="pr body",
            ),
            PullRequest(
                number=9,
                url=f"https://github.com/{repo}/pull/9",
                title="Wrong label PR",
                state=state,
                author="alice",
                labels=["support"],
                updated_at="2026-05-25T02:00:00Z",
                body="pr body",
            ),
        ]


class LinkedPullRequestSyncClient:
    def __init__(self, *, linked: bool = True) -> None:
        self.linked = linked

    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]:
        return [
            GitHubIssue(
                repo=repo,
                number=245,
                title="Linked issue",
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
                number=12,
                url=f"https://github.com/{repo}/pull/12",
                title="Linked PR",
                state=state,
                author="alice",
                updated_at="2026-05-25T02:00:00Z",
                body=issue_link_marker(repo, 245) if self.linked else "regular body",
            ),
            PullRequest(
                number=13,
                url=f"https://github.com/{repo}/pull/13",
                title="Unlinked PR",
                state=state,
                author="bob",
                updated_at="2026-05-25T02:00:00Z",
                body="regular body",
            ),
        ]


class ClosableIssueSyncClient:
    def __init__(self) -> None:
        self.open_issue = True

    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]:
        if not self.open_issue:
            return []
        return [
            GitHubIssue(
                repo=repo,
                number=245,
                title="Closable issue",
                body="issue body",
                url=f"https://github.com/{repo}/issues/245",
                state="open",
                labels=["bug"],
                author="alice",
                updated_at="2026-05-25T01:00:00Z",
            )
        ]

    def list_pull_requests(self, repo: str, *, state: str = "open") -> list[PullRequest]:
        return []
