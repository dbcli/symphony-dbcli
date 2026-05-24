from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from symphony_dbcli.config import PolicyConfig, ProfileConfig, default_config, render_workflow
from symphony_dbcli.dashboard import (
    DashboardRuntime,
    DashboardState,
    render_attempt,
    render_index,
    render_issue,
)
from symphony_dbcli.store import IssueSnapshot, Store


def test_dashboard_uses_static_css(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()

    html = render_index(store)

    assert '<link rel="stylesheet" href="/static/dashboard.css"' in html
    assert '<script src="/static/dashboard.js" defer></script>' in html
    assert "<style>" not in html
    assert "Recent Attempts" in html
    assert "Dry Run" in html
    assert "On" in html
    assert "Workspace Strategy" in html
    assert "branch prefix symphony" in html
    assert ".symphony/worktrees" in html
    assert "Start queued work automatically" in html
    assert 'role="switch"' in html
    assert 'aria-checked="true"' in html
    assert 'form action="/" method="get" data-ask-form' in html
    assert "data-ask-answer" in html


def test_dashboard_shows_worker_auto_start_off(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    store.set_start_queued_work_automatically(False)

    html = render_index(store)

    assert "Queued work will wait until this is turned on." in html
    assert 'aria-checked="false"' in html
    assert 'value="true"' in html


def test_dashboard_shows_workflow_reload_status(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    config = default_config()
    accepted_id = store.record_workflow_version("WORKFLOW.md", render_workflow(config), config)
    rejected_id = store.record_workflow_version(
        "WORKFLOW.md",
        "# broken workflow",
        None,
        status="rejected",
        error="WORKFLOW.md must contain one fenced toml config block.",
    )

    html = render_index(store)

    assert "Workflow" in html
    assert "Current error" in html
    assert f"#{accepted_id}" in html
    assert f"#{rejected_id}" in html
    assert "WORKFLOW.md must contain one fenced toml config block." in html


def test_dashboard_shows_workflow_state_machine_and_pending_gates(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    config = default_config()
    _seed_issue(store)
    instance_id = store.create_workflow_instance(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        initial_state="review",
    )
    store.open_workflow_gate(
        instance_id=instance_id,
        workflow_version_id=None,
        gate="review_diff",
        transition_name="create_draft_pr",
        state="review",
        prompt="Review the generated diff.",
    )

    html = render_index(store, config=config)

    assert "State Machine" in html
    assert "todo" in html
    assert "create_draft_pr" in html
    assert "review_diff" in html
    assert "1 active" in html
    assert "Pending Human Gates" in html
    assert "dbcli/litecli#245" in html


def test_dashboard_shows_live_mode_when_dry_run_is_disabled(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    config = replace(
        default_config(),
        profile=ProfileConfig(active="prod"),
        policy=PolicyConfig(dry_run=False),
    )

    html = render_index(store, DashboardRuntime.from_config(config))

    assert "Dry Run" in html
    assert "Off" in html
    assert "prod profile" in html


def test_dashboard_shows_inline_ask_answer_with_detail_links(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    _seed_issue(store)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="research",
        workflow_version_id=None,
    )

    html = render_index(store, ask_question="How long did issue #245 take?")

    assert 'value="How long did issue #245 take?"' in html
    assert "dbcli/litecli#245 is queued" in html
    assert 'href="/issues/dbcli/litecli/245"' in html
    assert f'href="/attempts/{attempt_id}"' in html


def test_dashboard_state_updates_runtime_config() -> None:
    state = DashboardState(default_config())
    live_config = replace(default_config(), policy=PolicyConfig(dry_run=False))

    state.update_config(live_config)

    assert state.runtime(start_queued_work_automatically=False).dry_run is False
    assert state.runtime(start_queued_work_automatically=False).start_queued_work_automatically is False


def test_attempt_page_shows_draft_reply(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
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
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="research",
        workflow_version_id=None,
    )
    store.record_worker_result(
        attempt_id=attempt_id,
        repo="dbcli/litecli",
        issue_number=245,
        result_type="research_answer",
        title="Research Answer",
        body="Worker result body",
    )
    store.record_comment(
        attempt_id,
        repo="dbcli/litecli",
        issue_number=245,
        url="",
        body="Suggested reply body",
        status="drafted",
    )
    gate_id = _open_attempt_gate(store, attempt_id, transition_name="post_answer", gate="review_answer")

    html = render_attempt(store, attempt_id)

    assert "Worker Result" in html
    assert "Pending Workflow Gates" in html
    assert "post_answer" in html
    assert "Worker result body" in html
    assert "Draft Replies" in html
    assert "Suggested reply body" in html
    assert "drafted" in html
    assert "<textarea" in html
    assert "Post to GitHub" in html
    assert f'action="/workflow-gates/{gate_id}/run"' in html


def test_attempt_page_shows_code_follow_up_action_and_source_research(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    _seed_issue(store)
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

    research_html = render_attempt(store, research_attempt_id)
    code_attempt_id = store.create_code_follow_up_attempt(research_attempt_id, workflow_version_id=None)
    linked_research_html = render_attempt(store, research_attempt_id)
    code_html = render_attempt(store, code_attempt_id)

    assert "Create Code Follow-up" in research_html
    assert f"Code follow-up attempt {code_attempt_id}" in linked_research_html
    assert "Source Research" in code_html
    assert "Expand log_file" in code_html


def test_code_attempt_page_can_create_and_show_draft_pr(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    _seed_issue(store)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        worktree_path="/tmp/litecli",
        branch="symphony/dbcli-litecli-245-attempt-1",
        status="review",
    )
    store.record_worker_result(
        attempt_id=attempt_id,
        repo="dbcli/litecli",
        issue_number=245,
        result_type="code_changes",
        title="Code Changes",
        body="Summary:\n- Expanded configured `log_file` paths.",
    )
    gate_id = _open_attempt_gate(store, attempt_id, transition_name="create_draft_pr", gate="review_diff")

    html = render_attempt(store, attempt_id)

    assert "Create Draft PR" in html
    assert f'action="/workflow-gates/{gate_id}/run"' in html
    assert 'name="title"' in html
    assert 'name="body"' in html
    assert "Fix #245: Expanded configured log_file paths" in html
    assert "Fixes https://github.com/dbcli/litecli/issues/245" in html

    store.record_pr(
        attempt_id,
        repo="dbcli/litecli",
        number=12,
        url="https://github.com/dbcli/litecli/pull/12",
        title="Fix #245",
    )
    pr_html = render_attempt(store, attempt_id)

    assert "Create Draft PR" not in pr_html
    assert "https://github.com/dbcli/litecli/pull/12" in pr_html


def test_attempt_page_hides_review_actions_without_pending_gates(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    _seed_issue(store)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        worktree_path="/tmp/litecli",
        branch="symphony/dbcli-litecli-245-attempt-1",
        status="review",
    )
    store.record_worker_result(
        attempt_id=attempt_id,
        repo="dbcli/litecli",
        issue_number=245,
        result_type="code_changes",
        title="Code Changes",
        body="Summary:\n- Expanded configured `log_file` paths.",
    )

    html = render_attempt(store, attempt_id)

    assert "Pending Workflow Gates" not in html
    assert "Create Draft PR" not in html


def test_issue_page_shows_editable_draft_replies(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    _seed_issue(store)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="research",
        workflow_version_id=None,
    )
    store.record_comment(
        attempt_id,
        repo="dbcli/litecli",
        issue_number=245,
        url="",
        body="Edited before posting",
        status="drafted",
    )

    html = render_issue(store, "dbcli/litecli", 245)

    assert "Draft Replies" in html
    assert "Edited before posting" in html
    assert "Post to GitHub" in html
    assert f"Attempt {attempt_id}" in html


def _seed_issue(store: Store) -> None:
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
    return store.open_workflow_gate(
        instance_id=instance_id,
        workflow_version_id=None,
        gate=gate,
        transition_name=transition_name,
        state="review",
        prompt="Review the generated output.",
    )
