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
    store.record_codex_event(
        attempt_id,
        thread_id="thread-1",
        event_type="turn/start/request",
        payload={
            "threadId": "thread-1",
            "cwd": "/tmp/worktree",
            "model": "gpt-5.4-mini",
            "approvalPolicy": "never",
            "input": [{"type": "text", "text": "Fix the failing tests."}],
        },
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
    assert detail["prompts"][0]["prompt"] == "Fix the failing tests."
    assert detail["prompts"][0]["model"] == "gpt-5.4-mini"


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


def test_failed_attempt_does_not_block_eligible_issue(tmp_path: Path) -> None:
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
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="research",
        workflow_version_id=None,
    )
    store.finish_attempt(attempt_id, "failed", "failed")

    eligible = store.eligible_issues("symphony:todo", "symphony:blocked")

    assert [row["number"] for row in eligible] == [245]


def test_start_queued_work_automatically_is_always_on(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()

    assert store.start_queued_work_automatically() is True

    store.set_start_queued_work_automatically(False)
    assert store.start_queued_work_automatically() is True

    store.set_start_queued_work_automatically(True)
    assert store.start_queued_work_automatically() is True


def test_runtime_lock_allows_one_owner_until_expiration(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()

    assert store.acquire_runtime_lock("orchestration", "owner-a", ttl_seconds=60) is True
    assert store.acquire_runtime_lock("orchestration", "owner-b", ttl_seconds=60) is False
    assert store.refresh_runtime_lock("orchestration", "owner-b", ttl_seconds=60) is False

    lock = store.runtime_lock("orchestration")
    assert lock is not None
    assert lock["owner"] == "owner-a"

    assert store.acquire_runtime_lock("orchestration", "owner-a", ttl_seconds=-1) is True
    assert store.acquire_runtime_lock("orchestration", "owner-b", ttl_seconds=60) is True
    store.release_runtime_lock("orchestration", "owner-b")
    assert store.runtime_lock("orchestration") is None


def test_store_records_workflow_runtime_state(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    config = default_config()
    version_id = store.record_workflow_version("WORKFLOW.md", render_workflow(config), config)
    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/litecli",
            number=9,
            title="Fix completions",
            url="https://github.com/dbcli/litecli/issues/9",
            state="open",
            labels=["symphony:todo", "symphony:type:code"],
            task_type="code",
        )
    )
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=9,
        task_type="code",
        workflow_version_id=version_id,
    )

    instance_id = store.create_workflow_instance(
        repo="dbcli/litecli",
        issue_number=9,
        task_type="code",
        workflow_version_id=version_id,
        initial_state="todo",
        attempt_id=attempt_id,
    )
    action_run_id = store.start_workflow_action_run(
        instance_id=instance_id,
        workflow_version_id=version_id,
        attempt_id=attempt_id,
        transition_name="claim_issue",
        action_name="github.apply_labels",
        input_data={"labels": ["symphony:working"]},
        idempotency_key="dbcli/litecli#9:claim_issue",
    )
    store.finish_workflow_action_run(
        action_run_id,
        status="succeeded",
        output_data={"applied": ["symphony:working"]},
    )
    event_id = store.transition_workflow_instance(
        instance_id,
        workflow_version_id=version_id,
        transition_name="claim_issue",
        action_name="github.apply_labels",
        trigger="automatic",
        from_state="todo",
        to_state="claimed",
        data={"action_run_id": action_run_id},
    )
    gate_id = store.open_workflow_gate(
        instance_id=instance_id,
        workflow_version_id=version_id,
        gate="review_diff",
        transition_name="create_draft_pr",
        state="review",
        prompt="Review the generated diff.",
    )

    instance = store.workflow_instance_by_id(instance_id)
    active = store.active_workflow_instance_for_issue("dbcli/litecli", 9)
    pending_gates = store.pending_workflow_gates()
    assert instance is not None
    assert active is not None
    assert instance["current_state"] == "claimed"
    assert active["id"] == instance_id
    assert pending_gates[0]["id"] == gate_id
    assert pending_gates[0]["repo"] == "dbcli/litecli"

    store.resolve_workflow_gate(gate_id, decision="approved", decided_by="amjith")

    with store.connect() as conn:
        action_run = conn.execute(
            "SELECT * FROM workflow_action_runs WHERE id = ?", (action_run_id,)
        ).fetchone()
        event = conn.execute("SELECT * FROM workflow_transition_events WHERE id = ?", (event_id,)).fetchone()
        gate = conn.execute("SELECT * FROM workflow_gates WHERE id = ?", (gate_id,)).fetchone()
    assert action_run is not None
    assert action_run["status"] == "succeeded"
    assert action_run["duration_ms"] is not None
    assert event is not None
    assert event["to_state"] == "claimed"
    assert gate is not None
    assert gate["status"] == "resolved"
    assert gate["decision"] == "approved"


def test_store_running_workflow_gate_reserves_transition(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/litecli",
            number=9,
            title="Fix prompt logging",
            url="https://github.com/dbcli/litecli/issues/9",
            state="open",
            labels=[],
            task_type="code",
        )
    )
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=9,
        task_type="code",
        workflow_version_id=None,
        status="review",
    )
    instance_id = store.create_workflow_instance(
        repo="dbcli/litecli",
        issue_number=9,
        task_type="code",
        workflow_version_id=None,
        initial_state="review",
        attempt_id=attempt_id,
    )
    gate_id = store.open_workflow_gate(
        instance_id=instance_id,
        workflow_version_id=None,
        gate="review_diff",
        transition_name="create_draft_pr",
        state="review",
    )

    assert store.start_workflow_gate(gate_id, decided_by="dashboard") is True
    assert store.start_workflow_gate(gate_id, decided_by="dashboard") is False
    assert store.pending_workflow_gates() == []
    running = store.running_workflow_gate_for_attempt(attempt_id, "create_draft_pr")
    duplicate_id = store.open_workflow_gate(
        instance_id=instance_id,
        workflow_version_id=None,
        gate="review_diff",
        transition_name="create_draft_pr",
        state="review",
    )

    assert running is not None
    assert running["id"] == gate_id
    assert duplicate_id == gate_id
    store.reopen_workflow_gate(gate_id)
    assert store.pending_workflow_gates()[0]["id"] == gate_id


def test_store_records_workflow_artifacts(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/litecli",
            number=9,
            title="Fix completions",
            url="https://github.com/dbcli/litecli/issues/9",
            state="open",
            labels=["symphony:todo"],
            task_type="code",
        )
    )
    instance_id = store.create_workflow_instance(
        repo="dbcli/litecli",
        issue_number=9,
        task_type="code",
        workflow_version_id=None,
        initial_state="todo",
    )
    action_run_id = store.start_workflow_action_run(
        instance_id=instance_id,
        workflow_version_id=None,
        attempt_id=None,
        transition_name="allocate_workspace",
        action_name="workspace.allocate",
    )
    store.finish_workflow_action_run(
        action_run_id,
        status="succeeded",
        output_data={"worktree_path": "/tmp/litecli"},
    )

    store.record_workflow_artifacts(
        instance_id,
        {"workspace": "/tmp/litecli"},
        workflow_version_id=None,
        action_run_id=action_run_id,
    )

    assert store.workflow_artifact(instance_id, "workspace") == "/tmp/litecli"
    assert store.workflow_artifacts(instance_id) == {"workspace": "/tmp/litecli"}
    assert store.latest_workflow_action_output(instance_id, "allocate_workspace") == {
        "worktree_path": "/tmp/litecli"
    }


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
