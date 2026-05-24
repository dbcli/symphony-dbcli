from __future__ import annotations

from pathlib import Path

from symphony_dbcli.config import default_config, render_workflow
from symphony_dbcli.store import IssueSnapshot, Store


def test_store_records_workflow_versions_and_attempt_metrics(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    config = default_config()
    workflow = render_workflow(config)

    version_id = store.record_workflow_version("WORKFLOW.md", workflow, config)
    same_version_id = store.record_workflow_version("WORKFLOW.md", workflow, config)

    assert same_version_id == version_id
    latest = store.latest_workflow_version()
    assert latest is not None
    assert latest["id"] == version_id

    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/pgcli",
            number=42,
            title="Investigate completion bug",
            url="https://github.com/dbcli/pgcli/issues/42",
            state="open",
            labels=["symphony:todo", "symphony:type:code"],
            task_type="code",
        )
    )
    attempt_id = store.create_attempt(
        repo="dbcli/pgcli",
        issue_number=42,
        task_type="code",
        workflow_version_id=version_id,
    )
    store.start_attempt(attempt_id, "worker-1")
    running_workers = store.running_workers()
    assert len(running_workers) == 1
    assert running_workers[0]["worker_id"] == "worker-1"
    store.record_timeline_event(
        attempt_id,
        phase="codex",
        event_type="completed",
        started_monotonic_ns=1_000_000,
        ended_monotonic_ns=6_000_000,
    )
    store.record_codex_turn(
        attempt_id,
        thread_id="thread-1",
        turn_index=1,
        status="completed",
        started_monotonic_ns=1_000_000,
        ended_monotonic_ns=6_000_000,
    )
    store.record_error(
        attempt_id,
        phase="test",
        error_type="pytest_failed",
        message="one test failed",
        recoverable=True,
    )
    store.record_worker_result(
        attempt_id=attempt_id,
        repo="dbcli/pgcli",
        issue_number=42,
        result_type="research_answer",
        title="Research Answer",
        body="Worker result body",
    )
    store.record_comment(
        attempt_id,
        repo="dbcli/pgcli",
        issue_number=42,
        url="",
        body="Draft response",
        status="drafted",
    )
    store.finish_attempt(attempt_id, "review", "needs_review")

    detail = store.attempt_detail(attempt_id)

    assert detail is not None
    assert detail["attempt"]["turn_count"] == 1
    assert detail["attempt"]["error_count"] == 1
    assert detail["attempt"]["codex_duration_ms"] == 5
    assert detail["attempt"]["duration_ms"] == 5
    assert detail["result"]["body"] == "Worker result body"
    assert detail["comments"][0]["body"] == "Draft response"


def test_eligible_issues_use_labels(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/litecli",
            number=7,
            title="Support question",
            url="https://github.com/dbcli/litecli/issues/7",
            state="open",
            labels=["symphony:todo"],
            task_type="research",
        )
    )

    eligible = store.eligible_issues("symphony:todo", "symphony:blocked")

    assert len(eligible) == 1
    assert eligible[0]["repo"] == "dbcli/litecli"

    store.create_attempt(
        repo="dbcli/litecli",
        issue_number=7,
        task_type="research",
        workflow_version_id=None,
    )

    assert store.eligible_issues("symphony:todo", "symphony:blocked") == []


def test_store_creates_code_follow_up_from_research_result(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/litecli",
            number=245,
            title="Support question",
            url="https://github.com/dbcli/litecli/issues/245",
            state="open",
            labels=["symphony:todo"],
            task_type="research",
        )
    )
    research_attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="research",
        workflow_version_id=None,
    )
    store.record_worker_result(
        attempt_id=research_attempt_id,
        repo="dbcli/litecli",
        issue_number=245,
        result_type="research_answer",
        title="Research Answer",
        body="Expand log_file before checking its parent directory.",
    )

    code_attempt_id = store.create_code_follow_up_attempt(research_attempt_id, workflow_version_id=None)
    duplicate_attempt_id = store.create_code_follow_up_attempt(research_attempt_id, workflow_version_id=None)

    code_attempt = store.attempt_by_id(code_attempt_id)
    source_result = store.follow_up_source_result(code_attempt_id)
    research_detail = store.attempt_detail(research_attempt_id)
    code_detail = store.attempt_detail(code_attempt_id)
    assert duplicate_attempt_id == code_attempt_id
    assert code_attempt is not None
    assert code_attempt["task_type"] == "code"
    assert code_attempt["status"] == "queued"
    assert source_result is not None
    assert source_result["source_attempt_id"] == research_attempt_id
    assert "Expand log_file" in source_result["body"]
    assert research_detail is not None
    assert research_detail["code_follow_up"]["id"] == code_attempt_id
    assert len(research_detail["follow_up_targets"]) == 1
    assert code_detail is not None
    assert code_detail["source_result"]["source_attempt_id"] == research_attempt_id
