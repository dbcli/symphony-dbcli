from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from symphony_dbcli.config import PolicyConfig, ProfileConfig, default_config, render_workflow
from symphony_dbcli.dashboard import DashboardRuntime, DashboardState, render_attempt, render_index
from symphony_dbcli.store import IssueSnapshot, Store


def test_dashboard_uses_static_css(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()

    html = render_index(store)

    assert '<link rel="stylesheet" href="/static/dashboard.css"' in html
    assert "<style>" not in html
    assert "Recent Attempts" in html
    assert "Dry Run" in html
    assert "On" in html


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


def test_dashboard_state_updates_runtime_config() -> None:
    state = DashboardState(default_config())
    live_config = replace(default_config(), policy=PolicyConfig(dry_run=False))

    state.update_config(live_config)

    assert state.runtime().dry_run is False


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

    html = render_attempt(store, attempt_id)

    assert "Worker Result" in html
    assert "Worker result body" in html
    assert "Draft Replies" in html
    assert "Suggested reply body" in html
    assert "drafted" in html


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
