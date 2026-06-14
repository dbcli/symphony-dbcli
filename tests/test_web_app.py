from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from starlette.requests import Request
from starlette.responses import RedirectResponse

from symphony_dbcli.config import DatabaseConfig, WorkflowConfig, default_config
from symphony_dbcli.db import create_db_engine, create_session_factory
from symphony_dbcli.github import GitHubIssue, PullRequest
from symphony_dbcli.models import (
    ChatMessage,
    ChatThread,
    Source,
    SourceItem,
    SourceItemLink,
    SourceSyncRun,
    WorkItem,
    WorkItemLink,
    WorkItemRun,
    WorkItemStateEvent,
)
from symphony_dbcli.review_actions import issue_link_marker, source_item_link_marker
from symphony_dbcli.runtime import RuntimeCycleResult, RuntimeStatus, RuntimeWorkerView
from symphony_dbcli.sources import LocalTicketCreate, SourceRepository, SourceSyncClient
from symphony_dbcli.store import IssueSnapshot, Store
from symphony_dbcli.web.app import create_app
from symphony_dbcli.web.dependencies import (
    WebRuntime,
    _format_compact_localtime,
    _format_compact_localtime_seconds,
    _format_localtime,
    _format_relative_time,
    _format_tokens,
)
from symphony_dbcli.web.routers import attempts, work_items
from symphony_dbcli.work_items import WorkItemActivation, WorkItemRepository


def _legacy_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "symphony.db")
    store.init()
    return store


def _seed_legacy_issue(store: Store) -> None:
    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/litecli",
            number=245,
            title="Logging support question",
            url="https://github.com/dbcli/litecli/issues/245",
            state="open",
            labels=["symphony:todo"],
            task_type="research",
        )
    )


def _open_attempt_gate(store: Store, attempt_id: int, *, transition_name: str, gate: str) -> int:
    attempt = store.attempt_by_id(attempt_id)
    assert attempt is not None
    instance_id = store.create_workflow_instance(
        repo=str(attempt["repo"]),
        issue_number=int(attempt["issue_number"]),
        task_type=str(attempt["task_type"]),
        workflow_version_id=None,
        initial_state="review",
        attempt_id=attempt_id,
    )
    return int(
        store.open_workflow_gate(
            instance_id=instance_id,
            workflow_version_id=None,
            gate=gate,
            transition_name=transition_name,
            state="review",
            prompt="Review the generated output.",
        )
    )


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
    assert 'id="dashboard-main"' in response.text
    assert 'id="board-columns"' in response.text
    assert 'id="modal-root"' in response.text
    assert 'name="q"' in response.text
    assert 'role="radiogroup" aria-label="Item type"' in response.text
    assert 'name="kind"' in response.text
    assert 'value="all"' in response.text
    assert 'value="issue"' in response.text
    assert 'value="pull_request"' in response.text
    assert 'hx-trigger="change"' in response.text
    assert 'hx-trigger="input changed delay:200ms, search"' in response.text
    assert 'hx-target="#board-columns"' in response.text
    assert 'hx-select="#board-columns"' in response.text
    assert 'hx-swap="outerHTML"' in response.text
    assert 'hx-push-url="true"' in response.text
    assert "dbcli/litecli" in response.text
    assert "Sync Source" in response.text
    assert "data-theme-toggle" in response.text
    assert "data-theme-toggle-label>&#9790;</span>" in response.text
    assert "Switch to dark mode" in response.text
    assert '<link rel="stylesheet" href="/web-static/web.css?v=' in response.text
    assert '<script src="/web-static/vendor/htmx.min.js"' in response.text
    assert '<script src="/web-static/vendor/sortable.min.js"' in response.text
    assert '<script src="/web-static/web.js?v=' in response.text
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


def test_localtime_filter_formats_utc_timestamps_as_pacific_time() -> None:
    assert _format_localtime("2026-05-25T12:00:00+00:00") == "2026-05-25 5:00:00 AM PDT"
    assert _format_localtime("2026-01-25T12:00:00Z") == "2026-01-25 4:00:00 AM PST"
    assert _format_localtime("") == "-"
    assert _format_localtime("not-a-date") == "not-a-date"


def test_compact_localtime_filter_formats_utc_timestamps_as_pacific_time() -> None:
    assert _format_compact_localtime("2026-06-06T18:01:09+00:00") == "Jun 06, 11:01am"
    assert _format_compact_localtime("2026-01-25T12:00:00Z") == "Jan 25, 4:00am"
    assert _format_compact_localtime("") == "-"
    assert _format_compact_localtime("not-a-date") == "not-a-date"


def test_compact_localtime_seconds_filter_formats_utc_timestamps_as_pacific_time() -> None:
    assert _format_compact_localtime_seconds("2026-06-06T18:01:09+00:00") == "Jun 06, 11:01:09am"
    assert _format_compact_localtime_seconds("2026-01-25T12:00:00Z") == "Jan 25, 4:00:00am"
    assert _format_compact_localtime_seconds("") == "-"
    assert _format_compact_localtime_seconds("not-a-date") == "not-a-date"


def test_relative_time_filter_formats_recent_timestamps() -> None:
    assert _format_relative_time((datetime.now(UTC) - timedelta(hours=2, minutes=5)).isoformat()) == "~2h ago"
    assert _format_relative_time((datetime.now(UTC) - timedelta(days=3, hours=2)).isoformat()) == "~3d ago"
    assert _format_relative_time(datetime.now(UTC).isoformat()) == "just now"
    assert _format_relative_time("") == "-"
    assert _format_relative_time("not-a-date") == "not-a-date"


def test_tokens_filter_formats_large_counts() -> None:
    assert _format_tokens(999) == "999"
    assert _format_tokens(42_500) == "42.5K"
    assert _format_tokens(4_213_116) == "4.2M"


def test_fastapi_workflow_page_renders_vertical_flowchart(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/workflow")

    assert response.status_code == 200
    assert "Workflow Flowchart" in response.text
    assert 'aria-label="Vertical workflow flowchart"' in response.text
    assert "data-workflow-controls" in response.text
    assert "data-workflow-flowchart" in response.text
    assert "Conditional branch" in response.text
    assert '<polygon points="' in response.text
    assert "fix_issue" in response.text
    assert "task.type == &#34;code&#34;" in response.text
    assert "create_draft_pr" in response.text
    assert "Pending Gates" in response.text


def test_fastapi_workers_page_shows_runtime_status(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    client = _client(tmp_path, runtime=runtime)

    response = client.get("/workers")

    assert response.status_code == 200
    assert "Run workflow cycle now" in response.text
    assert 'action="/workflow/run-cycle"' in response.text
    assert "Leader" in response.text
    assert "Running" in response.text
    assert "Enabled" in response.text
    assert "2026-05-25 5:01:00 AM PDT" in response.text
    assert "3 queued, 1 running" in response.text
    assert "worker-42" in response.text
    assert "dbcli/litecli#245" in response.text
    assert "No cycle has run in this FastAPI process yet." in response.text


def test_fastapi_manual_cycle_endpoint_calls_runtime(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    client = _client(tmp_path, runtime=runtime)

    response = client.post("/workflow/run-cycle")

    assert response.status_code == 200
    assert runtime.triggers == ["manual"]
    assert "Workflow Cycle" in response.text
    assert "Completed" in response.text
    assert "Issues Synced" in response.text
    assert "2" in response.text
    assert "Workers Started" in response.text


def test_fastapi_workers_page_handles_missing_runtime(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/workers")

    assert response.status_code == 200
    assert "Not attached" in response.text
    assert "Runtime service is not attached to FastAPI yet." in response.text


def test_fastapi_lifespan_starts_and_stops_attached_runtime(tmp_path: Path) -> None:
    config = replace(default_config(), database=DatabaseConfig(path=str(tmp_path / "symphony.db")))
    store = Store(config.database.path)
    store.init()
    runtime = FakeRuntime()

    with TestClient(create_app(config, store, runtime=runtime, run_runtime=True)) as client:
        assert client.get("/api/health").status_code == 200
        assert runtime.started is True
        assert runtime.stopped is False

    assert runtime.stopped is True


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


def test_fastapi_attempt_events_stream_replays_live_events(tmp_path: Path) -> None:
    client = _client(tmp_path)
    store = _legacy_store(tmp_path)
    _seed_legacy_issue(store)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="running",
    )
    store.record_timeline_event(
        attempt_id,
        phase="codex",
        event_type="started",
        message="Started codex app-server",
    )
    store.record_codex_event(
        attempt_id,
        thread_id="thread-245",
        event_type="agent/message/delta",
        payload={"threadId": "thread-245", "delta": "Hello from Codex."},
    )
    store.record_error(
        attempt_id,
        phase="codex",
        error_type="CodexRunnerError",
        message="app-server failed",
    )

    response = client.get(f"/api/attempts/{attempt_id}/events?once=true")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: attempt" in response.text
    assert '"status":"running"' in response.text
    assert "event: timeline" in response.text
    assert '"message":"Started codex app-server"' in response.text
    assert "event: codex" in response.text
    assert '"eventType":"agent/message/delta"' in response.text
    assert '"outputDelta":"Hello from Codex."' in response.text
    assert "event: error" in response.text
    assert '"message":"app-server failed"' in response.text


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
    assert '<div class="source-card-list kanban-list is-empty" data-state="todo">' in pgcli_board.text
    assert '<div class="empty-state">No items</div>' in pgcli_board.text
    assert 'aria-label="Backlog pagination"' not in pgcli_board.text


def test_fastapi_board_search_preserves_selected_source(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=RepoScopedSearchSyncClient())
    fixture_id = _add_source(client, "amjith/symphony-dbcli-e2e-fixture")
    litecli_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, fixture_id)
    _sync_source(client, litecli_id)

    default_board = client.get("/board")
    litecli_board = client.get(f"/board/source/{litecli_id}")
    search = client.get(f"/board/source/{litecli_id}?q=completion")

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
    assert "<time" in board.text
    assert 'title="' in board.text
    assert "just now" in board.text
    assert "#245" in board.text
    assert "#8" in board.text
    assert "data-source-item-id=" in board.text
    assert "<span>2</span>" in board.text
    assert 'aria-label="Backlog pagination"' not in board.text


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
    assert 'aria-label="Backlog pagination"' in first_page.text
    assert "backlog_page=2" in first_page.text
    assert second_page.status_code == 200
    assert "21-25 of 25" in second_page.text
    assert "Backlog issue 005" in second_page.text
    assert "Backlog issue 001" in second_page.text
    assert "Backlog issue 006" not in second_page.text
    assert 'aria-label="Backlog pagination"' in second_page.text


def test_fastapi_board_paginates_done_work_items(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=ManyClosedIssueSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)

    for source_item_id in _source_item_ids_for_source(tmp_path, source_id):
        _activate_source_item(client, source_item_id, task_type="code")
    for work_item_id in _work_item_ids_for_source(tmp_path, source_id):
        response = client.post(
            f"/work-items/{work_item_id}/move",
            data={"target_state": "done"},
            follow_redirects=False,
        )
        assert response.status_code == 303

    first_page = client.get(f"/board/source/{source_id}")
    second_page = client.get(f"/board/source/{source_id}?done_page=2")

    assert first_page.status_code == 200
    assert 'aria-label="Done pagination"' in first_page.text
    assert "1-20 of 25" in first_page.text
    assert "Closed issue 025" in first_page.text
    assert "Closed issue 006" in first_page.text
    assert "Closed issue 005" not in first_page.text
    assert "done_page=2" in first_page.text
    assert second_page.status_code == 200
    assert 'aria-label="Done pagination"' in second_page.text
    assert "21-25 of 25" in second_page.text
    assert "Closed issue 005" in second_page.text
    assert "Closed issue 001" in second_page.text
    assert "Closed issue 006" not in second_page.text


def test_fastapi_board_searches_backlog_with_sqlite_fts_body_matches(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=SearchableSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)

    board = client.get(f"/board?source_id={source_id}&q=ftsneedle%20")

    assert board.status_code == 200
    assert "Cache cleanup" in board.text
    assert "Another bug" not in board.text
    assert 'value="ftsneedle "' in board.text
    assert 'aria-label="Backlog pagination"' not in board.text


def test_fastapi_board_searches_work_columns_with_sqlite_fts(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=SearchableSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    matching_source_item_id = _source_item_id_for(client, source_id, "Cache cleanup")
    other_source_item_id = _source_item_id_for(client, source_id, "Another bug")
    _activate_source_item(client, matching_source_item_id, task_type="code")
    _activate_source_item(client, other_source_item_id, task_type="code")

    board = client.get(f"/board?source_id={source_id}&q=ftsneedle%20")

    assert board.status_code == 200
    assert "Cache cleanup" in board.text
    assert "Another bug" not in board.text
    assert 'value="ftsneedle "' in board.text
    assert "work item #1" in board.text
    assert "work item #2" not in board.text


def test_fastapi_board_filters_by_source_item_kind(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    issue_source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")
    pr_source_item_id = _source_item_id_for(client, source_id, "Improve docs")
    _activate_source_item(client, issue_source_item_id, task_type="code")
    _activate_source_item(client, pr_source_item_id, task_type="code")

    all_board = client.get(f"/board?source_id={source_id}")
    issues_board = client.get(f"/board?source_id={source_id}&kind=issue")
    prs_board = client.get(f"/board?source_id={source_id}&kind=pull_request")

    assert all_board.status_code == 200
    assert "Fix completion crash" in all_board.text
    assert "Improve docs" in all_board.text
    assert issues_board.status_code == 200
    assert "Fix completion crash" in issues_board.text
    assert "Improve docs" not in issues_board.text
    assert prs_board.status_code == 200
    assert "Improve docs" in prs_board.text
    assert "Fix completion crash" not in prs_board.text


def test_fastapi_work_item_start_creates_attempt_from_header_flow(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    config = replace(default_config(), database=DatabaseConfig(path=str(tmp_path / "symphony.db")))
    store = Store(config.database.path)
    store.init()
    workflow_version_id = store.record_workflow_version("WORKFLOW.md", "test workflow", config)
    client = TestClient(
        create_app(
            config,
            store,
            workflow_path="WORKFLOW.md",
            workflow_version_id=workflow_version_id,
            runtime=runtime,
        )
    )
    source_id = _add_source(client, "dbcli/litecli")

    response = client.post(
        "/work-items",
        data={
            "message": "Can we add a retry button for failed workflow actions?",
            "source_id": str(source_id),
        },
        follow_redirects=False,
    )
    attempt = client.get("/attempts/1")
    board = client.get(f"/board/source/{source_id}")
    threads, messages, source_items, work_items, links, runs = _chat_records(tmp_path)
    attempt_row = store.attempt_by_id(1)
    workflow_instance = store.workflow_instance_for_attempt(1)
    run_claim = WorkItemRepository(
        create_session_factory(create_db_engine(str(tmp_path / "symphony.db")))
    ).next_queued_run()

    assert response.status_code == 303
    assert response.headers["location"] == "/attempts/1"
    assert attempt.status_code == 200
    assert 'aria-label="Attempt conversation"' in attempt.text
    assert "Can we add a retry button" in attempt.text
    assert "data-chat-submit-form" in attempt.text
    assert 'href="/attempts/1"' in board.text
    assert "Can we add a retry button for failed workflow actions?" in board.text
    assert len(threads) == 1
    assert [message.role for message in messages] == ["user"]
    assert source_items[0].kind == "conversation"
    assert source_items[0].source_id == source_id
    assert work_items[0].state == "in_progress"
    assert work_items[0].task_type == "code"
    assert links[0].relationship == "conversation"
    assert len(runs) == 1
    assert runs[0].attempt_id == 1
    assert runs[0].workflow_instance_id is not None
    assert runs[0].status == "queued"
    assert attempt_row is not None
    assert attempt_row["status"] == "queued"
    assert attempt_row["work_item_id"] == work_items[0].id
    assert attempt_row["work_item_run_id"] == runs[0].id
    assert attempt_row["workflow_version_id"] == workflow_version_id
    assert workflow_instance is not None
    assert workflow_instance["workflow_version_id"] == workflow_version_id
    assert run_claim is None
    assert runtime.triggers == ["chat_implementation"]


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
    assert "work item #1" in board.text
    assert 'data-work-item-id="1"' in board.text
    assert "code" in board.text
    assert "Fix completion crash" in work_items.text
    assert 'href="/work-items/1">Fix completion crash</a>' in work_items.text
    assert ">#1</a> Fix completion crash" not in work_items.text
    assert "Prefer unit tests." in detail.text
    assert "<dt>ID</dt>" not in detail.text
    assert "Runs / Attempts" in detail.text
    assert "not claimed" in detail.text
    assert "Actions" in detail.text
    assert "Move Work Item" in detail.text
    assert 'hx-get="/work-items/1/move-form?target_state=todo&return_to=/work-items/1"' in detail.text
    assert f'hx-get="/work-items/1/archive-form?return_to=/board/source/{source_id}"' in detail.text
    assert 'id="move-work"' not in detail.text
    assert '<form class="stacked-form" method="post" action="/work-items/1/archive">' not in detail.text


def test_fastapi_source_item_activate_form_renders_modal(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")

    response = client.get(f"/source-items/{source_item_id}/activate-form?return_to=/board/source/{source_id}")

    assert response.status_code == 200
    assert 'class="modal-backdrop"' in response.text
    assert 'role="dialog"' in response.text
    assert f'hx-post="/source-items/{source_item_id}/activate"' in response.text
    assert 'hx-target="#modal-root"' in response.text
    assert f'name="return_to" value="/board/source/{source_id}"' in response.text
    assert "Fix completion crash" in response.text
    assert "Queue Work" in response.text


def test_fastapi_htmx_source_item_activation_redirects_to_return_target(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")

    response = client.post(
        f"/source-items/{source_item_id}/activate",
        data={
            "task_type": "code",
            "user_hint": "Drag queued this.",
            "return_to": f"/board/source/{source_id}",
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    board = client.get(f"/board/source/{source_id}")

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == f"/board/source/{source_id}"
    assert "work item #1" in board.text
    assert "Drag queued this." not in board.text


def test_fastapi_work_item_detail_links_attempts(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")
    _activate_source_item(client, source_item_id, task_type="code")
    store = Store(str(tmp_path / "symphony.db"))
    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/litecli",
            number=245,
            title="Fix completion crash",
            url="https://github.com/dbcli/litecli/issues/245",
            state="open",
            labels=[],
            task_type="code",
        )
    )
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        work_item_id=1,
        work_item_run_id=1,
        status="review",
    )
    workflow_instance_id = store.create_workflow_instance(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        initial_state="review",
        attempt_id=attempt_id,
        work_item_id=1,
        work_item_run_id=1,
    )
    session_factory = create_session_factory(create_db_engine(str(tmp_path / "symphony.db")))
    with session_factory() as session:
        run = session.get(WorkItemRun, 1)
        assert run is not None
        run.attempt_id = attempt_id
        run.workflow_instance_id = workflow_instance_id
        run.status = "needs_review"
        run.started_at = "2026-06-06T18:01:00Z"
        run.completed_at = "2026-06-06T18:02:00Z"
        session.commit()

    detail = client.get("/work-items/1")

    assert detail.status_code == 200
    assert '<nav class="breadcrumbs" aria-label="Breadcrumb">' in detail.text
    assert f'href="/board/source/{source_id}">Board</a>' in detail.text
    assert '<span aria-current="page">Work Item #1</span>' in detail.text
    assert "Runs / Attempts" in detail.text
    assert "<th>Run</th>" not in detail.text
    assert f'href="/attempts/{attempt_id}"' in detail.text
    assert f"Attempt #{attempt_id}" in detail.text
    assert "needs_review" in detail.text
    assert (
        '<time datetime="2026-06-06T18:01:00Z" title="2026-06-06 11:01:00 AM PDT">Jun 06, 11:01am</time>'
    ) in detail.text
    assert (
        '<time datetime="2026-06-06T18:02:00Z" title="2026-06-06 11:02:00 AM PDT">Jun 06, 11:02am</time>'
    ) in detail.text


def test_fastapi_work_item_detail_includes_related_attempts_without_runs(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")
    _activate_source_item(client, source_item_id, task_type="code")
    store = Store(str(tmp_path / "symphony.db"))
    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/litecli",
            number=245,
            title="Fix completion crash",
            url="https://github.com/dbcli/litecli/issues/245",
            state="open",
            labels=[],
            task_type="code",
        )
    )
    linked_attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        work_item_id=1,
        status="review",
    )
    legacy_attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="research",
        workflow_version_id=None,
        status="failed",
    )

    detail = client.get("/work-items/1")

    assert detail.status_code == 200
    assert f'href="/attempts/{linked_attempt_id}"' in detail.text
    assert f"Attempt #{linked_attempt_id}" in detail.text
    assert f'href="/attempts/{legacy_attempt_id}"' in detail.text
    assert f"Attempt #{legacy_attempt_id}" in detail.text
    assert "<th>Run</th>" not in detail.text
    assert "no run" not in detail.text


def test_fastapi_attempt_page_queues_codex_adjustment(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient(), runtime=runtime)
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")
    _activate_source_item(client, source_item_id, task_type="code")
    store = Store(str(tmp_path / "symphony.db"))
    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/litecli",
            number=245,
            title="Fix completion crash",
            url="https://github.com/dbcli/litecli/issues/245",
            state="open",
            labels=[],
            task_type="code",
        )
    )
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        work_item_id=1,
        work_item_run_id=1,
        status="review",
    )
    store.record_worker_result(
        attempt_id=attempt_id,
        repo="dbcli/litecli",
        issue_number=245,
        result_type="code_changes",
        title="Code Changes",
        body="Changed the parser.",
    )
    session_factory = create_session_factory(create_db_engine(str(tmp_path / "symphony.db")))
    with session_factory() as session:
        run = session.get(WorkItemRun, 1)
        assert run is not None
        run.attempt_id = attempt_id
        run.status = "needs_review"
        session.commit()

    detail = client.get(f"/attempts/{attempt_id}")
    form = client.get(f"/attempts/{attempt_id}/adjustment-form?return_to=/attempts/{attempt_id}")
    response = client.post(
        f"/attempts/{attempt_id}/adjustment",
        data={
            "note": "Tighten the validation edge case.",
            "return_to": f"/attempts/{attempt_id}",
        },
        follow_redirects=False,
    )
    _, runs = _work_item_events_and_runs(tmp_path)
    work_item = _work_item(tmp_path, 1)

    assert detail.status_code == 200
    assert "Send Follow-up" in detail.text
    assert detail.text.index('aria-label="Attempt conversation"') < detail.text.index(
        'class="chat-message is-user attempt-composer"'
    )
    assert f'action="/attempts/{attempt_id}/adjustment"' in detail.text
    assert 'name="note"' in detail.text
    assert '<nav class="breadcrumbs" aria-label="Breadcrumb">' in detail.text
    assert f'href="/board/source/{source_id}">Board</a>' in detail.text
    assert 'href="/work-items/1">Work Item #1</a>' in detail.text
    assert f'<span aria-current="page">Attempt {attempt_id}</span>' in detail.text
    assert form.status_code == 200
    assert "Queue Adjustment" in form.text
    assert response.status_code == 303
    assert response.headers["location"] == f"/attempts/{attempt_id}"
    assert runtime.triggers == ["attempt_adjustment"]
    assert work_item.state == "in_progress"
    assert runs[-1].trigger == "adjustment"
    assert runs[-1].source_attempt_id == attempt_id
    assert runs[-1].status == "queued"
    assert "Tighten the validation edge case." in runs[-1].user_hint
    assert json.loads(runs[-1].reasons_json) == ["revise_implementation"]


def test_fastapi_attempt_page_allows_adjustment_for_research_attempt(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")
    _activate_source_item(client, source_item_id, task_type="research")
    store = Store(str(tmp_path / "symphony.db"))
    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/litecli",
            number=245,
            title="Fix completion crash",
            url="https://github.com/dbcli/litecli/issues/245",
            state="open",
            labels=[],
            task_type="research",
        )
    )
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="research",
        workflow_version_id=None,
        work_item_id=1,
        work_item_run_id=1,
        status="review",
    )

    detail = client.get(f"/attempts/{attempt_id}")

    assert detail.status_code == 200
    assert "Send Follow-up" in detail.text
    assert f'action="/attempts/{attempt_id}/adjustment"' in detail.text
    assert 'placeholder="Send a follow-up for this attempt"' in detail.text


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
    runtime = FakeRuntime()
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient(), runtime=runtime)
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
    assert runtime.triggers == ["board_move"]
    assert "In Progress" in detail.text
    assert "Reviewer asked for tests." in detail.text
    assert "work item #1" in board.text
    assert [event.to_state for event in events[-2:]] == ["in_review", "in_progress"]
    assert runs[-1].status == "queued"
    assert runs[-1].trigger == "rerun"
    assert runs[-1].user_hint == "Reviewer asked for tests."
    assert json.loads(runs[-1].reasons_json) == ["address_pr_comments", "fix_ci"]


def test_fastapi_work_item_move_schedules_in_progress_cycle_after_response(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    config = replace(default_config(), database=DatabaseConfig(path=str(tmp_path / "symphony.db")))
    store = Store(config.database.path)
    store.init()
    app = create_app(config, store, runtime=runtime)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/work-items/1/move",
            "headers": [],
            "app": app,
        }
    )
    response = RedirectResponse("/work-items/1", status_code=303)

    scheduled_response = work_items._schedule_cycle_after_in_progress_move(request, "in_progress", response)

    assert scheduled_response is response
    assert response.background is not None
    assert runtime.triggers == []

    anyio.run(response.background)

    assert runtime.triggers == ["board_move"]


def test_fastapi_work_item_move_only_schedules_cycle_for_in_progress(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    config = replace(default_config(), database=DatabaseConfig(path=str(tmp_path / "symphony.db")))
    store = Store(config.database.path)
    store.init()
    app = create_app(config, store, runtime=runtime)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/work-items/1/move",
            "headers": [],
            "app": app,
        }
    )
    response = RedirectResponse("/work-items/1", status_code=303)

    scheduled_response = work_items._schedule_cycle_after_in_progress_move(request, "in_review", response)

    assert scheduled_response is response
    assert response.background is None
    assert runtime.triggers == []


def test_fastapi_review_rerun_does_not_reuse_activation_hint(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")
    _activate_source_item(
        client,
        source_item_id,
        task_type="code",
        user_hint="Use the initial model context only.",
    )

    client.post(
        "/work-items/1/move",
        data={"target_state": "in_review"},
        follow_redirects=False,
    )
    response = client.post(
        "/work-items/1/move",
        data={"target_state": "in_progress", "reasons": ["fix_ci"]},
        follow_redirects=False,
    )
    _events, runs = _work_item_events_and_runs(tmp_path)

    assert response.status_code == 303
    assert runs[-1].trigger == "rerun"
    assert runs[-1].user_hint == ""
    assert json.loads(runs[-1].reasons_json) == ["fix_ci"]


def test_fastapi_work_item_move_form_presets_review_rerun_target(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")
    _activate_source_item(client, source_item_id, task_type="code")

    client.post(
        "/work-items/1/move",
        data={"target_state": "in_review"},
        follow_redirects=False,
    )
    response = client.get("/work-items/1?target_state=in_progress")

    assert response.status_code == 200
    assert (
        'hx-get="/work-items/1/move-form?target_state=in_progress&return_to=/work-items/1"' in response.text
    )
    assert "Add rerun reasons and optional model context" not in response.text
    assert '<option value="in_progress" selected>In Progress</option>' not in response.text


def test_fastapi_work_item_move_form_renders_modal(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")
    _activate_source_item(client, source_item_id, task_type="code")
    client.post(
        "/work-items/1/move",
        data={"target_state": "in_review"},
        follow_redirects=False,
    )

    response = client.get(
        f"/work-items/1/move-form?target_state=in_progress&return_to=/board/source/{source_id}"
    )

    assert response.status_code == 200
    assert 'class="modal-backdrop"' in response.text
    assert 'role="dialog"' in response.text
    assert 'hx-post="/work-items/1/move"' in response.text
    assert 'hx-target="#modal-root"' in response.text
    assert f'name="return_to" value="/board/source/{source_id}"' in response.text
    assert '<option value="in_progress" selected>In Progress</option>' in response.text
    assert "Add rerun reasons and optional model context" in response.text


def test_fastapi_work_item_archive_form_renders_modal(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")
    _activate_source_item(client, source_item_id, task_type="code")

    response = client.get(f"/work-items/1/archive-form?return_to=/board/source/{source_id}")

    assert response.status_code == 200
    assert 'class="modal-backdrop"' in response.text
    assert 'role="dialog"' in response.text
    assert 'hx-post="/work-items/1/archive"' in response.text
    assert 'hx-target="#modal-root"' in response.text
    assert f'name="return_to" value="/board/source/{source_id}"' in response.text
    assert "Archive Work" in response.text
    assert "Optional archive reason" in response.text


def test_fastapi_htmx_work_item_move_redirects_to_return_target(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")
    _activate_source_item(client, source_item_id, task_type="code")
    client.post(
        "/work-items/1/move",
        data={"target_state": "in_review"},
        follow_redirects=False,
    )

    response = client.post(
        "/work-items/1/move",
        data={
            "target_state": "in_progress",
            "reasons": ["fix_ci"],
            "note": "CI is failing.",
            "return_to": f"/board/source/{source_id}",
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    events, runs = _work_item_events_and_runs(tmp_path)

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == f"/board/source/{source_id}"
    assert events[-1].to_state == "in_progress"
    assert runs[-1].user_hint == "CI is failing."


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


def test_fastapi_sync_attaches_marker_linked_pr_to_active_local_ticket_work_item(tmp_path: Path) -> None:
    sync_client = LocalTicketLinkedPullRequestSyncClient()
    client = _client(tmp_path, source_sync_client=sync_client)
    source_id = _add_source(client, "dbcli/litecli")
    session_factory = create_session_factory(create_db_engine(str(tmp_path / "symphony.db")))
    source_item = SourceRepository(session_factory).create_local_ticket(
        LocalTicketCreate(
            source_id=source_id,
            title="Remove codex review action",
            body="Remove the GitHub action that runs Codex review on PRs.",
        )
    )
    WorkItemRepository(session_factory).activate_source_item(
        WorkItemActivation(source_item_id=source_item.id, task_type="code")
    )
    sync_client.source_item_id = source_item.id

    _sync_source(client, source_id)
    detail = client.get("/work-items/1")
    source_links, work_item, work_item_links = _linked_pr_records(tmp_path)

    assert "Active PR" in detail.text
    assert work_item.active_pr_source_item_id == source_links[0].linked_source_item_id
    assert {link.relationship for link in source_links} == {"ticket_pr"}
    assert source_links[0].marker == source_item_link_marker(source_item.id)
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


def test_fastapi_htmx_archive_work_item_redirects_to_return_target(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=FakeSourceSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Fix completion crash")
    _activate_source_item(client, source_item_id, task_type="code")

    response = client.post(
        "/work-items/1/archive",
        data={"note": "Handled elsewhere.", "return_to": f"/board/source/{source_id}"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    work_item = _work_item(tmp_path, 1)

    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == f"/board/source/{source_id}"
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
    assert "Delete" in sources.text


def test_fastapi_sources_reject_invalid_repo(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post("/sources", data={"repo": "not-a-repo"})

    assert response.status_code == 400
    assert "owner/name format" in response.text


def test_fastapi_source_delete_requires_display_name_confirmation(tmp_path: Path) -> None:
    client = _client(tmp_path, source_sync_client=LinkedPullRequestSyncClient())
    source_id = _add_source(client, "dbcli/litecli")
    _sync_source(client, source_id)
    source_item_id = _source_item_id_for(client, source_id, "Linked issue")
    _activate_source_item(client, source_item_id, task_type="code")
    update = client.post(
        f"/sources/{source_id}",
        data={"display_name": "LiteCLI Source", "enabled": "true"},
        follow_redirects=False,
    )
    delete_page = client.get(f"/sources/{source_id}/delete")
    rejected = client.post(
        f"/sources/{source_id}/delete",
        data={"confirmation": "dbcli/litecli"},
    )
    sources = client.get("/sources")

    assert update.status_code == 303
    assert delete_page.status_code == 200
    assert f'action="/sources/{source_id}/delete"' in delete_page.text
    assert "Type LiteCLI Source to confirm" in delete_page.text
    assert rejected.status_code == 400
    assert "Type LiteCLI Source to confirm deletion." in rejected.text
    assert "LiteCLI Source" in sources.text

    deleted = client.post(
        f"/sources/{source_id}/delete",
        data={"confirmation": "LiteCLI Source"},
        follow_redirects=False,
    )

    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/sources"
    assert _source_owned_record_counts(tmp_path) == {
        "sources": 0,
        "source_items": 0,
        "source_item_links": 0,
        "source_sync_runs": 0,
        "work_items": 0,
        "work_item_links": 0,
        "work_item_runs": 0,
        "work_item_state_events": 0,
        "source_item_search": 0,
    }


def test_fastapi_ask_renders_inline_board_answer(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _add_source(client, "dbcli/litecli")

    response = client.get("/ask?q=What%20is%20the%20board%20status%3F")

    assert response.status_code == 200
    assert "Board status across 1 source(s)" in response.text
    assert 'href="/board"' in response.text
    assert 'href="/work-items"' in response.text


def test_fastapi_attempt_and_issue_pages_cover_review_actions(tmp_path: Path) -> None:
    client = _client(tmp_path)
    store = _legacy_store(tmp_path)
    _seed_legacy_issue(store)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="research",
        workflow_version_id=None,
        status="review",
    )
    store.record_worker_result(
        attempt_id=attempt_id,
        repo="dbcli/litecli",
        issue_number=245,
        result_type="research_answer",
        title="Research Answer",
        body="Worker result body",
    )
    timeline_id = store.record_timeline_event(
        attempt_id,
        phase="codex",
        event_type="started",
        message="Started codex exec",
    )
    completed_timeline_id = store.record_timeline_event(
        attempt_id,
        phase="codex",
        event_type="completed",
        message="Finished codex exec",
    )
    store.record_timeline_event(
        attempt_id,
        phase="github",
        event_type="pull_request_created",
        message="https://github.com/dbcli/litecli/pull/258",
    )
    with store.connect() as conn:
        conn.execute(
            "UPDATE worker_timeline_events SET started_at = ? WHERE id = ?",
            ("2026-01-25T12:00:00Z", timeline_id),
        )
        conn.execute(
            "UPDATE worker_timeline_events SET started_at = ? WHERE id = ?",
            ("2026-01-25T12:01:00Z", completed_timeline_id),
        )
    store.record_comment(
        attempt_id,
        repo="dbcli/litecli",
        issue_number=245,
        url="",
        body="Suggested reply body",
        status="drafted",
    )
    store.record_error(
        attempt_id,
        phase="worker",
        error_type="RuntimeError",
        message="Worker failed.",
        log_excerpt="Traceback (most recent call last):\nRuntimeError: worker exploded",
    )
    store.record_codex_event(
        attempt_id,
        thread_id="thread-245",
        event_type="turn/start/request",
        payload={
            "threadId": "thread-245",
            "cwd": "/tmp/litecli",
            "model": "gpt-5.4-mini",
            "approvalPolicy": "never",
            "input": [{"type": "text", "text": "Draft a reply for issue 245."}],
        },
    )
    store.record_codex_event(
        attempt_id,
        thread_id="thread-245",
        event_type="thread/tokenUsage/updated",
        payload={
            "threadId": "thread-245",
            "tokenUsage": {
                "total": {
                    "inputTokens": 40_000,
                    "outputTokens": 2_500,
                    "totalTokens": 42_500,
                }
            },
        },
    )
    store.record_pr(
        attempt_id,
        "dbcli/litecli",
        257,
        "https://github.com/dbcli/litecli/pull/257",
        "Draft pull request",
        state="open",
    )
    gate_id = _open_attempt_gate(store, attempt_id, transition_name="post_answer", gate="review_answer")
    with store.connect() as conn:
        conn.execute(
            "UPDATE pull_requests SET created_at = ? WHERE attempt_id = ?",
            ("2026-01-25T12:02:00Z", attempt_id),
        )
        conn.execute(
            "UPDATE workflow_gates SET created_at = ? WHERE id = ?",
            ("2026-01-25T12:03:00Z", gate_id),
        )

    attempt = client.get(f"/attempts/{attempt_id}")
    issue = client.get("/issues/dbcli/litecli/245")

    assert attempt.status_code == 200
    assert 'aria-label="Attempt conversation"' in attempt.text
    assert "Research Answer" in attempt.text
    assert "Live worker stream" in attempt.text
    assert f'data-live-events-url="/api/attempts/{attempt_id}/events"' in attempt.text
    assert 'data-live-output-mode="rendered" aria-pressed="true"' in attempt.text
    assert 'data-live-output-mode="raw" aria-pressed="false"' in attempt.text
    assert "data-live-codex-raw" in attempt.text
    assert "No streamed Codex output yet." in attempt.text
    assert attempt.text.index('aria-label="Attempt conversation"') < attempt.text.index(
        'class="chat-message is-assistant attempt-draft-reply"'
    )
    assert 'class="chat-message is-assistant attempt-draft-reply"' in attempt.text
    assert "Draft reply" in attempt.text
    assert f'action="/workflow-gates/{gate_id}/run"' in attempt.text
    assert "Post to GitHub" in attempt.text
    assert "42.5K tokens" in attempt.text
    assert "PR #257" in attempt.text
    assert 'class="metric-link" href="https://github.com/dbcli/litecli/pull/257"' in attempt.text
    assert (
        '<time datetime="2026-01-25T12:02:00Z" title="2026-01-25 4:02:00 AM PST">Jan 25, 4:02am</time>'
    ) in attempt.text
    assert "Worker result body" in attempt.text
    assert "Suggested reply body" in attempt.text
    assert '<section class="panel accordion-panel" data-collapsible="codex-prompts">' not in attempt.text
    assert '<div class="panel accordion-panel" data-collapsible="worker-result">' not in attempt.text
    assert "<summary>Initial prompt</summary>" in attempt.text
    assert "<summary>Run transcript</summary>" not in attempt.text
    assert "Run transcript" in attempt.text
    assert "Draft a reply for issue 245." in attempt.text
    assert "/tmp/litecli" in attempt.text
    assert "RuntimeError: worker exploded" in attempt.text
    assert issue.status_code == 200
    assert "Logging support question" in issue.text
    assert "Attempts" in issue.text
    assert f'href="/attempts/{attempt_id}"' in issue.text
    assert "Post to GitHub" in issue.text
    assert 'action="/comments/' in issue.text


def test_fastapi_attempt_page_creates_code_follow_up_and_renders_draft_pr_gate(tmp_path: Path) -> None:
    client = _client(tmp_path)
    store = _legacy_store(tmp_path)
    _seed_legacy_issue(store)
    research_attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="research",
        workflow_version_id=None,
        status="review",
    )
    store.record_worker_result(
        attempt_id=research_attempt_id,
        repo="dbcli/litecli",
        issue_number=245,
        result_type="research_answer",
        title="Research Answer",
        body="Expand log_file before checking its parent directory.",
    )
    code_attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        worktree_path="/tmp/litecli",
        branch="symphony/dbcli-litecli-245-attempt-1",
        status="review",
    )
    store.record_worker_result(
        attempt_id=code_attempt_id,
        repo="dbcli/litecli",
        issue_number=245,
        result_type="code_changes",
        title="Code Changes",
        body="Summary:\n- Expanded configured `log_file` paths.",
    )
    gate_id = _open_attempt_gate(
        store,
        code_attempt_id,
        transition_name="create_draft_pr",
        gate="review_diff",
    )

    follow_up = client.post(f"/attempts/{research_attempt_id}/follow-up-code", follow_redirects=False)
    follow_up_detail = client.get(follow_up.headers["location"])
    draft_pr = client.get(f"/attempts/{code_attempt_id}")

    assert follow_up.status_code == 303
    assert follow_up.headers["location"].startswith("/attempts/")
    assert "Source Research" in follow_up_detail.text
    assert "Expand log_file" in follow_up_detail.text
    assert draft_pr.status_code == 200
    assert "Create Draft PR" in draft_pr.text
    assert "Ask Codex to Create Draft PR" not in draft_pr.text
    assert f'action="/workflow-gates/{gate_id}/run"' in draft_pr.text
    assert 'name="title"' not in draft_pr.text
    assert 'name="body"' not in draft_pr.text
    assert "Draft pull request" in draft_pr.text
    assert "Ready to create a draft pull request from the worker result." in draft_pr.text
    assert "Fix #245: Logging support question" not in draft_pr.text
    assert "## Changes" not in draft_pr.text
    assert "Fixes https://github.com/dbcli/litecli/issues/245" not in draft_pr.text


def test_fastapi_attempt_page_renders_failed_action_retry_button(tmp_path: Path) -> None:
    client = _client(tmp_path)
    store = _legacy_store(tmp_path)
    _seed_legacy_issue(store)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="failed",
    )
    instance_id = store.create_workflow_instance(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        initial_state="worker_complete",
        attempt_id=attempt_id,
    )
    action_run_id = store.start_workflow_action_run(
        instance_id=instance_id,
        workflow_version_id=None,
        attempt_id=attempt_id,
        transition_name="auto_create_draft_pr",
        action_name="github.create_draft_pr",
        retry_count=1,
    )
    store.finish_workflow_action_run(action_run_id, status="failed", error="push failed")
    store.fail_workflow_instance(instance_id, workflow_version_id=None, message="push failed")

    response = client.get(f"/attempts/{attempt_id}")

    assert response.status_code == 200
    assert f'action="/workflow-actions/{action_run_id}/retry"' in response.text
    assert "Retry auto_create_draft_pr" in response.text
    assert "failed retry 1" in response.text


def test_fastapi_retry_failed_action_route_invokes_orchestrator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(tmp_path)
    store = _legacy_store(tmp_path)
    _seed_legacy_issue(store)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="failed",
    )
    instance_id = store.create_workflow_instance(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        initial_state="worker_complete",
        attempt_id=attempt_id,
    )
    action_run_id = store.start_workflow_action_run(
        instance_id=instance_id,
        workflow_version_id=None,
        attempt_id=attempt_id,
        transition_name="auto_create_draft_pr",
        action_name="github.create_draft_pr",
    )
    store.finish_workflow_action_run(action_run_id, status="failed", error="push failed")
    called: list[int] = []

    class FakeOrchestrator:
        def retry_failed_workflow_action(self, action_run_id: int) -> None:
            called.append(action_run_id)

    monkeypatch.setattr(attempts, "orchestrator_for_state", lambda _state: FakeOrchestrator())

    response = client.post(
        f"/workflow-actions/{action_run_id}/retry",
        data={"return_to": f"/attempts/{attempt_id}"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/attempts/{attempt_id}"
    assert called == [action_run_id]


def test_fastapi_attempt_page_renders_workspace_diff_before_draft_pr(tmp_path: Path) -> None:
    client = _client(tmp_path)
    store = _legacy_store(tmp_path)
    _seed_legacy_issue(store)
    worktree = tmp_path / "litecli"
    worktree.mkdir()
    _git(worktree, "init")
    (worktree / "README.md").write_text("start\n", encoding="utf-8")
    _git(worktree, "add", "README.md")
    _git(worktree, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    base_sha = _git(worktree, "rev-parse", "HEAD")
    (worktree / "README.md").write_text("fixed\n", encoding="utf-8")
    (worktree / "new_file.py").write_text("print('hello')\n", encoding="utf-8")
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="review",
    )
    store.update_attempt_workspace(
        attempt_id,
        base_repo_path=str(worktree),
        worktree_path=str(worktree),
        branch="symphony/dbcli-litecli-245-attempt-1",
        commit_sha=base_sha,
    )
    _open_attempt_gate(
        store,
        attempt_id,
        transition_name="create_draft_pr",
        gate="review_diff",
    )

    response = client.get(f"/attempts/{attempt_id}")

    assert response.status_code == 200
    assert "Workspace Diff" in response.text
    assert "README.md" in response.text
    assert "-start" in response.text
    assert "+fixed" in response.text
    assert "new_file.py" in response.text
    assert "+print(&#39;hello&#39;)" in response.text
    assert "Create Draft PR" in response.text
    assert "Ask Codex to Create Draft PR" not in response.text


def test_fastapi_create_draft_pr_gate_runs_in_background(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _codex_create_draft_pr_config(tmp_path)
    store = Store(config.database.path)
    store.init()
    client = TestClient(
        create_app(
            config,
            store,
            workflow_path="WORKFLOW.md",
        )
    )
    _seed_legacy_issue(store)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        worktree_path="/tmp/litecli",
        branch="symphony/dbcli-litecli-245-attempt-1",
        status="review",
    )
    gate_id = _open_attempt_gate(
        store,
        attempt_id,
        transition_name="create_draft_pr",
        gate="review_diff",
    )
    started_gates: list[tuple[int, dict[str, object]]] = []

    def fake_run_started_gate(
        state: object,
        started_gate_id: int,
        input_data: dict[str, object],
    ) -> None:
        started_gates.append((started_gate_id, input_data))

    monkeypatch.setattr(attempts, "_run_started_gate", fake_run_started_gate)

    response = client.post(
        f"/workflow-gates/{gate_id}/run",
        data={"return_to": f"/attempts/{attempt_id}"},
        follow_redirects=False,
    )
    gate = store.workflow_gate_by_id(gate_id)
    detail = client.get(f"/attempts/{attempt_id}")

    assert response.status_code == 303
    assert response.headers["location"] == f"/attempts/{attempt_id}"
    assert started_gates == [(gate_id, {})]
    assert gate is not None
    assert gate["status"] == "running"
    assert "Draft pull request creation is running." in detail.text
    assert "Ask Codex to Create Draft PR" not in detail.text


def test_fastapi_attempt_page_labels_pr_feedback_gate_explicitly(tmp_path: Path) -> None:
    client = _client(tmp_path)
    store = _legacy_store(tmp_path)
    _seed_legacy_issue(store)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="review",
    )
    gate_id = _open_attempt_gate(
        store,
        attempt_id,
        transition_name="push_pr_feedback_fix",
        gate="review_pr_feedback",
    )

    response = client.get(f"/attempts/{attempt_id}")

    assert response.status_code == 200
    assert "PR feedback fix" in response.text
    assert "Push Fix to Existing PR" in response.text
    assert "Run Gate" not in response.text
    assert f'action="/workflow-gates/{gate_id}/run"' in response.text


def test_fastapi_workflow_edit_preview_renders_proposal(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/workflow/edit",
        data={"request": "Prefer unit tests over integration tests.", "action": "preview"},
    )

    assert response.status_code == 200
    assert "Workflow Edit" in response.text
    assert "Workflow Preview" in response.text
    assert "Prefer unit tests over integration tests." in response.text
    assert "WORKFLOW.md proposal" in response.text
    assert "data-line-numbered-editor" in response.text
    assert "data-line-number-gutter" in response.text


def test_fastapi_github_app_callback_renders_conversion_instructions(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/github-app/callback?code=abc123&state=xyz")

    assert response.status_code == 200
    assert "GitHub App Created" in response.text
    assert "abc123" in response.text
    assert "State: xyz" in response.text
    assert "uv run symphony-dbcli github-app convert --code abc123" in response.text


def _codex_create_draft_pr_config(tmp_path: Path) -> WorkflowConfig:
    config = replace(default_config(), database=DatabaseConfig(path=str(tmp_path / "symphony.db")))
    transitions = dict(config.workflow.transitions)
    transitions["create_draft_pr"] = replace(
        transitions["create_draft_pr"],
        from_state="review",
        action="codex.create_draft_pr",
        trigger="human",
        gate="review_diff",
    )
    return replace(config, workflow=replace(config.workflow, transitions=transitions))


def _client(
    tmp_path: Path,
    source_sync_client: SourceSyncClient | None = None,
    runtime: WebRuntime | None = None,
) -> TestClient:
    config = replace(default_config(), database=DatabaseConfig(path=str(tmp_path / "symphony.db")))
    store = Store(config.database.path)
    store.init()
    return TestClient(
        create_app(
            config,
            store,
            workflow_path="WORKFLOW.md",
            source_sync_client=source_sync_client,
            runtime=runtime,
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


def _source_item_ids_for_source(tmp_path: Path, source_id: int) -> list[int]:
    session_factory = create_session_factory(create_db_engine(str(tmp_path / "symphony.db")))
    with session_factory() as session:
        return list(
            session.scalars(
                select(SourceItem.id)
                .where(SourceItem.source_id == source_id)
                .order_by(SourceItem.number.asc())
            )
        )


def _work_item_ids_for_source(tmp_path: Path, source_id: int) -> list[int]:
    session_factory = create_session_factory(create_db_engine(str(tmp_path / "symphony.db")))
    with session_factory() as session:
        return list(
            session.scalars(
                select(WorkItem.id).where(WorkItem.source_id == source_id).order_by(WorkItem.id.asc())
            )
        )


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


def _source_owned_record_counts(tmp_path: Path) -> dict[str, int]:
    session_factory = create_session_factory(create_db_engine(str(tmp_path / "symphony.db")))
    with session_factory() as session:
        search_count = session.execute(text("SELECT COUNT(*) FROM source_item_search")).scalar_one()
        return {
            "sources": len(list(session.scalars(select(Source)))),
            "source_items": len(list(session.scalars(select(SourceItem)))),
            "source_item_links": len(list(session.scalars(select(SourceItemLink)))),
            "source_sync_runs": len(list(session.scalars(select(SourceSyncRun)))),
            "work_items": len(list(session.scalars(select(WorkItem)))),
            "work_item_links": len(list(session.scalars(select(WorkItemLink)))),
            "work_item_runs": len(list(session.scalars(select(WorkItemRun)))),
            "work_item_state_events": len(list(session.scalars(select(WorkItemStateEvent)))),
            "source_item_search": int(search_count),
        }


def _chat_records(
    tmp_path: Path,
) -> tuple[
    list[ChatThread],
    list[ChatMessage],
    list[SourceItem],
    list[WorkItem],
    list[WorkItemLink],
    list[WorkItemRun],
]:
    session_factory = create_session_factory(create_db_engine(str(tmp_path / "symphony.db")))
    with session_factory() as session:
        return (
            list(session.scalars(select(ChatThread).order_by(ChatThread.id.asc()))),
            list(session.scalars(select(ChatMessage).order_by(ChatMessage.id.asc()))),
            list(session.scalars(select(SourceItem).order_by(SourceItem.id.asc()))),
            list(session.scalars(select(WorkItem).order_by(WorkItem.id.asc()))),
            list(session.scalars(select(WorkItemLink).order_by(WorkItemLink.id.asc()))),
            list(session.scalars(select(WorkItemRun).order_by(WorkItemRun.id.asc()))),
        )


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(path), *args], text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _work_item(tmp_path: Path, work_item_id: int) -> WorkItem:
    session_factory = create_session_factory(create_db_engine(str(tmp_path / "symphony.db")))
    with session_factory() as session:
        return session.scalars(select(WorkItem).where(WorkItem.id == work_item_id)).one()


class FakeRuntime:
    def __init__(self) -> None:
        self.triggers: list[str] = []
        self.started = False
        self.stopped = False
        self.cycle_result = RuntimeCycleResult(
            trigger="manual",
            status="succeeded",
            started_at="2026-05-25T12:00:00+00:00",
            completed_at="2026-05-25T12:00:03+00:00",
            synced=2,
            advanced=1,
            claimed=1,
            workers_started=1,
        )

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def run_cycle(self, *, trigger: str = "manual") -> RuntimeCycleResult:
        self.triggers.append(trigger)
        return self.cycle_result

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            enabled=True,
            running=True,
            polling_enabled=True,
            cycle_running=False,
            profile="local",
            poll_interval_seconds=5,
            next_cycle_at="2026-05-25T12:01:00+00:00",
            last_cycle=self.cycle_result if self.triggers else None,
            queued_attempts=3,
            running_attempts=1,
            workers=[
                RuntimeWorkerView(
                    worker_id="worker-42",
                    attempt_id=42,
                    repo="dbcli/litecli",
                    issue_number=245,
                    task_type="code",
                    pid=4242,
                    heartbeat_at="2026-05-25T12:00:02+00:00",
                    deadline_at="2026-05-25T13:00:00+00:00",
                    started_at="2026-05-25T12:00:00+00:00",
                    retry_count=1,
                )
            ],
        )


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


class ManyClosedIssueSyncClient:
    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]:
        return [
            GitHubIssue(
                repo=repo,
                number=number,
                title=f"Closed issue {number:03d}",
                body=f"Body for closed issue {number:03d}",
                url=f"https://github.com/{repo}/issues/{number}",
                state="closed",
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


class LocalTicketLinkedPullRequestSyncClient:
    def __init__(self) -> None:
        self.source_item_id: int | None = None

    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]:
        return []

    def list_pull_requests(self, repo: str, *, state: str = "open") -> list[PullRequest]:
        body = "regular body"
        if self.source_item_id is not None:
            body = source_item_link_marker(self.source_item_id)
        return [
            PullRequest(
                number=12,
                url=f"https://github.com/{repo}/pull/12",
                title="Remove Codex review workflow",
                state=state,
                author="alice",
                updated_at="2026-05-25T02:00:00Z",
                body=body,
            )
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
