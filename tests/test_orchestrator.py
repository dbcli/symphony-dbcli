from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

from symphony_dbcli.config import WorkflowConfig, WorkspaceConfig, default_config
from symphony_dbcli.db import create_db_engine, create_session_factory
from symphony_dbcli.github import (
    GitHubCheckRun,
    GitHubCiFailureContext,
    GitHubCiStatus,
    GitHubComment,
    GitHubIssue,
    GitHubPullRequestReviewComment,
    PullRequest,
    PullRequestMergeStatus,
)
from symphony_dbcli.models import WorkItem, WorkItemRun, create_model_tables
from symphony_dbcli.orchestrator import Orchestrator, build_worker_prompt
from symphony_dbcli.primitive_executor import PrimitiveContext, PrimitiveExecutionError, PrimitiveOutcome
from symphony_dbcli.sources import SourceCreate, SourceItemUpsert, SourceRepository
from symphony_dbcli.store import IssueSnapshot, Store
from symphony_dbcli.work_items import WorkItemActivation, WorkItemRepository


def test_code_follow_up_prompt_includes_research_context() -> None:
    prompt = build_worker_prompt(
        default_config(),
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        title="Logging support question",
        follow_up_context="Research result:\nExpand log_file before checking its parent directory.",
    )

    assert "Task type: code" in prompt
    assert "Follow-up context:" in prompt
    assert "Expand log_file" in prompt


def test_worker_prompt_includes_primitive_guidance() -> None:
    prompt = build_worker_prompt(
        default_config(),
        repo="dbcli/litecli",
        issue_number=245,
        task_type="research",
        title="Logging support question",
        primitive_guidance=["Keep the reply under two sentences.", "Stay succinct."],
    )

    assert "Primitive guidance:" in prompt
    assert "Keep the reply under two sentences." in prompt
    assert "Stay succinct." in prompt


def test_orchestrator_claim_records_workflow_runtime_state(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)

    attempt_id = Orchestrator(default_config(), store, github=FakeCleanupGitHub()).claim_next()

    assert attempt_id is not None
    instance = store.workflow_instance_for_attempt(attempt_id)
    assert instance is not None
    assert instance["current_state"] == "claimed"
    with store.connect() as conn:
        action_run = conn.execute(
            "SELECT * FROM workflow_action_runs WHERE workflow_instance_id = ?", (instance["id"],)
        ).fetchone()
        transition = conn.execute(
            "SELECT * FROM workflow_transition_events WHERE workflow_instance_id = ?", (instance["id"],)
        ).fetchone()
    assert action_run is not None
    assert action_run["transition_name"] == "claim_issue"
    assert action_run["status"] == "succeeded"
    assert transition is not None
    assert transition["from_state"] == "todo"
    assert transition["to_state"] == "claimed"


def test_orchestrator_runs_attempt_from_workflow_transitions(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="queued",
    )
    primitives = FakeWorkflowPrimitives()

    Orchestrator(
        default_config(),
        store,
        github=FakeCleanupGitHub(),
        primitives=primitives,
    ).run_attempt(attempt_id)

    instance = store.workflow_instance_for_attempt(attempt_id)
    detail = store.attempt_detail(attempt_id)
    gates = store.pending_workflow_gates()
    with store.connect() as conn:
        action_runs = list(
            conn.execute(
                "SELECT transition_name, action_name, status FROM workflow_action_runs ORDER BY id ASC"
            )
        )
    assert primitives.transitions == [
        "find_issue_pull_requests",
        "allocate_workspace",
        "run_setup",
        "fix_issue",
        "request_review",
    ]
    assert instance is not None
    assert instance["current_state"] == "review"
    assert detail is not None
    assert detail["attempt"]["status"] == "review"
    assert [row["transition_name"] for row in action_runs] == primitives.transitions
    assert {row["transition_name"] for row in gates} == {"create_draft_pr", "mark_blocked"}


def test_orchestrator_claims_and_runs_work_item_from_kanban_queue(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    source_repo, work_item_repo = _source_work_item_repositories(tmp_path)
    source = source_repo.create_source(SourceCreate(repo="dbcli/litecli"))
    source_repo.upsert_source_items(
        source_id=source.id,
        items=[
            SourceItemUpsert(
                kind="issue",
                number=245,
                title="Logging support question",
                url="https://github.com/dbcli/litecli/issues/245",
                state="open",
                author="amjith",
                labels=[],
                body="The log_file option does not expand ~.",
                github_updated_at="2026-05-24T11:00:00Z",
            )
        ],
    )
    source_item = source_repo.backlog_source_items(source.id)[0]
    work_item = work_item_repo.activate_source_item(
        WorkItemActivation(
            source_item_id=source_item.id,
            task_type="code",
            user_hint="Prefer a unit test.",
        )
    )
    primitives = FakeWorkflowPrimitives()
    orchestrator = Orchestrator(
        default_config(),
        store,
        github=FakeCleanupGitHub(),
        primitives=primitives,
    )

    attempt_id = orchestrator.claim_next()
    assert attempt_id is not None
    orchestrator.run_attempt(attempt_id)

    attempt = store.attempt_by_id(attempt_id)
    instance = store.workflow_instance_for_work_item(work_item.id)
    artifacts = store.workflow_artifacts(0 if instance is None else int(instance["id"]))
    saved_work_item, run = _work_item_and_run(tmp_path, work_item.id)
    assert attempt is not None
    assert int(attempt["work_item_id"]) == work_item.id
    assert int(attempt["work_item_run_id"]) == run.id
    assert instance is not None
    assert int(instance["work_item_id"]) == work_item.id
    assert int(instance["work_item_run_id"]) == run.id
    assert artifacts["work_item.id"] == work_item.id
    assert artifacts["work_item.user_hint"] == "Prefer a unit test."
    assert artifacts["source_item.number"] == 245
    assert artifacts["linked_issue.number"] == 245
    assert primitives.contexts[0].work_item_id == work_item.id
    assert primitives.contexts[0].user_hint == "Prefer a unit test."
    assert saved_work_item.state == "in_review"
    assert run.status == "needs_review"
    assert run.attempt_id == attempt_id


def test_orchestrator_hands_off_artifacts_between_transitions(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="queued",
    )
    config = _config_with_artifact_handoff()
    primitives = FakeWorkflowPrimitives()

    Orchestrator(
        config,
        store,
        github=FakeCleanupGitHub(),
        primitives=primitives,
    ).run_attempt(attempt_id)

    instance = store.workflow_instance_for_attempt(attempt_id)
    assert instance is not None
    artifacts = store.workflow_artifacts(int(instance["id"]))
    assert artifacts["workspace"] == "/tmp/worktree"
    assert artifacts["allocate_workspace.worktree_path"] == "/tmp/worktree"
    assert primitives.inputs[2]["worktree_path"] == "/tmp/worktree"
    assert primitives.inputs[3]["worktree_path"] == "/tmp/worktree"


def test_orchestrator_fans_out_pr_checks_and_feeds_combined_follow_up(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="queued",
        worktree_path="/tmp/worktree",
        base_repo_path="/tmp/repo.git",
        branch="symphony/existing-pr",
    )
    instance_id = store.create_workflow_instance(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        initial_state="setup_complete",
        attempt_id=attempt_id,
    )
    store.record_workflow_artifacts(
        instance_id,
        {
            "pull_request.exists": True,
            "pull_request.number": 12,
        },
        workflow_version_id=None,
    )
    primitives = FakeWorkflowPrimitives(pr_feedback=True)

    Orchestrator(
        default_config(),
        store,
        github=FakeCleanupGitHub(),
        primitives=primitives,
    ).run_attempt(attempt_id)

    instance = store.workflow_instance_by_id(instance_id)
    artifacts = store.workflow_artifacts(instance_id)
    gates = store.pending_workflow_gates_for_attempt(attempt_id)
    with store.connect() as conn:
        transitions = list(
            conn.execute(
                "SELECT transition_name, action_name FROM workflow_transition_events ORDER BY id ASC"
            )
        )
    check_transitions = {
        "check_pr_ci",
        "check_pr_comments",
        "check_pr_mergeability",
    }
    assert check_transitions.issubset(set(primitives.transitions))
    assert "fetch_ci_failure_context" in primitives.transitions
    assert "address_pr_feedback" in primitives.transitions
    assert instance is not None
    assert instance["current_state"] == "pr_follow_up_complete"
    assert artifacts["ci.failed_checks"] == [{"name": "tests", "conclusion": "failure"}]
    assert artifacts["ci.failure_context"] == [{"name": "tests", "log_excerpt": "failing test"}]
    assert artifacts["review_comments.comments"] == [{"body": "Please add a regression test."}]
    assert [row["transition_name"] for row in transitions] == [
        "initial_pr_checks",
        "fetch_ci_failure_context",
        "address_pr_feedback",
    ]
    assert transitions[0]["action_name"] == "workflow.parallel"
    assert {row["transition_name"] for row in gates} == {"push_pr_feedback_fix"}


def test_orchestrator_does_not_reuse_consumed_parallel_checkpoint(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="queued",
        worktree_path="/tmp/worktree",
        base_repo_path="/tmp/repo.git",
        branch="symphony/existing-pr",
    )
    instance_id = store.create_workflow_instance(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        initial_state="pr_refreshed",
        attempt_id=attempt_id,
    )
    store.record_workflow_artifacts(
        instance_id,
        {
            "pull_request.number": 12,
            "pull_request.is_merged": False,
        },
        workflow_version_id=None,
    )
    action_run_id = store.start_workflow_action_run(
        instance_id=instance_id,
        workflow_version_id=None,
        attempt_id=attempt_id,
        transition_name="refresh_pr_ci",
        action_name="github.fetch_ci_status",
    )
    store.finish_workflow_action_run(
        action_run_id,
        status="succeeded",
        output_data={"failed_checks": [{"name": "stale"}]},
    )
    store.transition_workflow_instance(
        instance_id,
        workflow_version_id=None,
        transition_name="refreshed_pr_checks",
        action_name="workflow.parallel",
        trigger="automatic",
        from_state="pr_refreshed",
        to_state="pr_checks_complete",
    )
    with store.connect() as conn:
        conn.execute(
            "UPDATE workflow_instances SET current_state = 'pr_refreshed' WHERE id = ?",
            (instance_id,),
        )
    primitives = FakeWorkflowPrimitives(pr_feedback=True)

    Orchestrator(
        default_config(),
        store,
        github=FakeCleanupGitHub(),
        primitives=primitives,
    ).run_attempt(attempt_id)

    artifacts = store.workflow_artifacts(instance_id)
    assert "refresh_pr_ci" in primitives.transitions
    assert artifacts["ci.failed_checks"] == [{"name": "tests", "conclusion": "failure"}]


def test_orchestrator_retries_failed_transition_within_retry_limit(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="queued",
    )
    config = _config_with_transition_retry("fix_issue", retry_limit=1)
    primitives = FakeWorkflowPrimitives(fail_once={"fix_issue"})

    Orchestrator(
        config,
        store,
        github=FakeCleanupGitHub(),
        primitives=primitives,
    ).run_attempt(attempt_id)

    with store.connect() as conn:
        fix_runs = list(
            conn.execute(
                """
                SELECT status, retry_count
                FROM workflow_action_runs
                WHERE transition_name = 'fix_issue'
                ORDER BY id ASC
                """
            )
        )
    assert primitives.transitions == [
        "find_issue_pull_requests",
        "allocate_workspace",
        "run_setup",
        "fix_issue",
        "fix_issue",
        "request_review",
    ]
    assert [(row["status"], row["retry_count"]) for row in fix_runs] == [("failed", 0), ("succeeded", 1)]


def test_orchestrator_resumes_succeeded_action_checkpoint(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="queued",
        worktree_path="/tmp/worktree",
        base_repo_path="/tmp/repo.git",
        branch="symphony/test",
    )
    instance_id = store.create_workflow_instance(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        initial_state="setup_complete",
        attempt_id=attempt_id,
    )
    action_run_id = store.start_workflow_action_run(
        instance_id=instance_id,
        workflow_version_id=None,
        attempt_id=attempt_id,
        transition_name="fix_issue",
        action_name="codex.fix_issue",
    )
    store.finish_workflow_action_run(
        action_run_id,
        status="succeeded",
        output_data={"transition": "fix_issue", "worktree_path": "/tmp/worktree"},
    )
    primitives = FakeWorkflowPrimitives()

    Orchestrator(
        default_config(),
        store,
        github=FakeCleanupGitHub(),
        primitives=primitives,
    ).run_attempt(attempt_id)

    instance = store.workflow_instance_by_id(instance_id)
    assert primitives.transitions == ["request_review"]
    assert instance is not None
    assert instance["current_state"] == "review"


def test_orchestrator_runs_human_gate_from_workflow_transition(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="review",
    )
    instance_id = store.create_workflow_instance(
        repo="dbcli/litecli",
        issue_number=245,
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
        prompt="Review the generated diff.",
    )
    primitives = FakeWorkflowPrimitives()

    result = Orchestrator(
        default_config(),
        store,
        github=FakeCleanupGitHub(),
        primitives=primitives,
    ).run_human_gate(gate_id)

    instance = store.workflow_instance_by_id(instance_id)
    gate = store.workflow_gate_by_id(gate_id)
    attempt = store.attempt_by_id(attempt_id)
    assert primitives.transitions == [
        "create_draft_pr",
        "wait_created_pr",
    ]
    assert result.current_state == "pr_waiting"
    assert result.stop_reason == "human_gate"
    assert instance is not None
    assert instance["current_state"] == "pr_waiting"
    assert gate is not None
    assert gate["status"] == "resolved"
    assert gate["decision"] == "approved"
    assert attempt is not None
    assert attempt["outcome"] == "draft_pr_created"
    assert {row["transition_name"] for row in store.pending_workflow_gates_for_attempt(attempt_id)} == {
        "check_pr_again"
    }


def test_orchestrator_runs_started_human_gate_from_background_path(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="review",
    )
    instance_id = store.create_workflow_instance(
        repo="dbcli/litecli",
        issue_number=245,
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
        prompt="Review the generated diff.",
    )
    primitives = FakeWorkflowPrimitives()
    orchestrator = Orchestrator(
        default_config(),
        store,
        github=FakeCleanupGitHub(),
        primitives=primitives,
    )

    orchestrator.start_human_gate(gate_id)
    running_gate = store.workflow_gate_by_id(gate_id)
    result = orchestrator.run_started_human_gate(gate_id)

    gate = store.workflow_gate_by_id(gate_id)
    instance = store.workflow_instance_by_id(instance_id)
    assert running_gate is not None
    assert running_gate["status"] == "running"
    assert primitives.transitions == [
        "create_draft_pr",
        "wait_created_pr",
    ]
    assert result.current_state == "pr_waiting"
    assert gate is not None
    assert gate["status"] == "resolved"
    assert instance is not None
    assert instance["current_state"] == "pr_waiting"


def test_orchestrator_runs_mark_blocked_human_gate(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="review",
    )
    instance_id = store.create_workflow_instance(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        initial_state="review",
        attempt_id=attempt_id,
    )
    gate_id = store.open_workflow_gate(
        instance_id=instance_id,
        workflow_version_id=None,
        gate="mark_blocked",
        transition_name="mark_blocked",
        state="review",
        prompt="Stop this attempt.",
    )
    primitives = FakeWorkflowPrimitives()

    result = Orchestrator(
        default_config(),
        store,
        github=FakeCleanupGitHub(),
        primitives=primitives,
    ).run_human_gate(gate_id)

    instance = store.workflow_instance_by_id(instance_id)
    attempt = store.attempt_by_id(attempt_id)
    assert primitives.transitions == ["mark_blocked"]
    assert result.current_state == "blocked"
    assert instance is not None
    assert instance["current_state"] == "blocked"
    assert attempt is not None
    assert attempt["status"] == "blocked"
    assert attempt["outcome"] == "blocked"


def test_orchestrator_advances_ready_workflow_instances(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="review",
    )
    store.record_pr(
        attempt_id,
        repo="dbcli/litecli",
        number=254,
        url="https://github.com/dbcli/litecli/pull/254",
        title="Fix #245",
        state="closed",
        merged_at="2026-05-24T14:00:00Z",
    )
    store.create_workflow_instance(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        initial_state="pr_refreshed",
        attempt_id=attempt_id,
    )
    primitives = FakeWorkflowPrimitives()

    advanced = Orchestrator(
        default_config(),
        store,
        github=FakeCleanupGitHub(),
        primitives=primitives,
    ).advance_ready_workflow_instances(allowed_side_effects={"github_read", "workspace_write"})

    attempt = store.attempt_by_id(attempt_id)
    assert advanced == 1
    assert primitives.transitions == ["cleanup_after_merge"]
    assert attempt is not None
    assert attempt["status"] == "done"
    assert attempt["outcome"] == "done"


def test_orchestrator_cleans_worktree_after_pr_merge(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    source = tmp_path / "source"
    bare = tmp_path / "repos" / "litecli.git"
    worktree = tmp_path / "worktrees" / "litecli"
    source.mkdir()
    bare.parent.mkdir()
    worktree.parent.mkdir()
    _git(source, "init", "--initial-branch=main")
    (source / "README.md").write_text("start\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    subprocess.run(["git", "clone", "--bare", str(source), str(bare)], check=True, capture_output=True)
    subprocess.run(
        ["git", "--git-dir", str(bare), "worktree", "add", str(worktree), "main"],
        check=True,
        capture_output=True,
    )
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="review",
    )
    store.update_attempt_workspace(
        attempt_id,
        base_repo_path=str(bare),
        worktree_path=str(worktree),
        branch="symphony/dbcli-litecli-245-attempt-1",
    )
    store.record_pr(
        attempt_id,
        repo="dbcli/litecli",
        number=254,
        url="https://github.com/dbcli/litecli/pull/254",
        title="Fix #245",
    )
    config = replace(
        default_config(),
        workspace=WorkspaceConfig(root=str(worktree.parent), bare_repos_root=str(bare.parent)),
    )

    summary = Orchestrator(config, store, github=FakeCleanupGitHub()).cleanup_merged_pull_request_worktrees()

    detail = store.attempt_detail(attempt_id)
    assert summary.scanned == 1
    assert summary.merged == 1
    assert summary.cleaned == 1
    assert not worktree.exists()
    assert detail is not None
    pull_request = detail["pull_requests"][0]
    assert pull_request["state"] == "closed"
    assert pull_request["merged_at"] == "2026-05-24T14:00:00Z"
    assert pull_request["worktree_cleaned_at"]
    assert detail["timeline"][-1]["event_type"] == "cleaned_after_pr_merge"


def test_orchestrator_skips_open_pr_worktree_cleanup(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        worktree_path=str(tmp_path / "worktrees" / "litecli"),
        base_repo_path=str(tmp_path / "repos" / "litecli.git"),
        status="review",
    )
    store.record_pr(
        attempt_id,
        repo="dbcli/litecli",
        number=254,
        url="https://github.com/dbcli/litecli/pull/254",
        title="Fix #245",
    )
    summary = Orchestrator(
        default_config(), store, github=FakeCleanupGitHub(merged=False)
    ).cleanup_merged_pull_request_worktrees()

    detail = store.attempt_detail(attempt_id)
    assert summary.scanned == 1
    assert summary.skipped == 1
    assert detail is not None
    assert detail["pull_requests"][0]["state"] == "open"
    assert detail["pull_requests"][0]["worktree_cleaned_at"] is None


class FakeCleanupGitHub:
    def __init__(self, *, merged: bool = True) -> None:
        self.merged = merged

    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]:
        return []

    def issue(self, repo: str, issue_number: int) -> GitHubIssue:
        return GitHubIssue(
            repo=repo,
            number=issue_number,
            title="Issue",
            body="",
            url=f"https://github.com/{repo}/issues/{issue_number}",
            state="open",
            labels=[],
            author="",
            updated_at="",
        )

    def list_comments(self, repo: str, issue_number: int) -> list[GitHubComment]:
        return []

    def list_pull_request_review_comments(
        self,
        repo: str,
        pull_request_number: int,
    ) -> list[GitHubPullRequestReviewComment]:
        return []

    def list_pull_requests(self, repo: str, *, state: str = "open") -> list[PullRequest]:
        return []

    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> None:
        return

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        return

    def pull_request(self, repo: str, number: int) -> PullRequest:
        if not self.merged:
            return PullRequest(
                number=number,
                url=f"https://github.com/{repo}/pull/{number}",
                title="Fix #245",
                state="open",
            )
        return PullRequest(
            number=number,
            url=f"https://github.com/{repo}/pull/{number}",
            title="Fix #245",
            state="closed",
            merged_at="2026-05-24T14:00:00Z",
        )

    def merge_status(self, repo: str, pull_request_number: int) -> PullRequestMergeStatus:
        pull_request = self.pull_request(repo, pull_request_number)
        return PullRequestMergeStatus(
            number=pull_request.number,
            url=pull_request.url,
            title=pull_request.title,
            state=pull_request.state,
            merged_at=pull_request.merged_at,
            head_sha=pull_request.head_sha,
            mergeable=True,
            mergeable_state="clean",
            has_conflicts=False,
        )

    def ci_status(self, repo: str, pull_request_number: int) -> GitHubCiStatus:
        return GitHubCiStatus(
            sha="",
            state="success",
            conclusion="success",
            failed_checks=[],
            checks=[GitHubCheckRun(name="tests", status="completed", conclusion="success", url="")],
        )

    def ci_failure_context(
        self,
        repo: str,
        pull_request_number: int,
        failed_checks: list[GitHubCheckRun],
    ) -> GitHubCiFailureContext:
        return GitHubCiFailureContext(sha="", failed_checks=[])


class FakeWorkflowPrimitives:
    def __init__(self, *, fail_once: set[str] | None = None, pr_feedback: bool = False) -> None:
        self.transitions: list[str] = []
        self.inputs: list[dict[str, object]] = []
        self.contexts: list[PrimitiveContext] = []
        self.fail_once = fail_once or set()
        self.failed: set[str] = set()
        self.pr_feedback = pr_feedback

    def fetch_issues(self) -> PrimitiveOutcome:
        return PrimitiveOutcome({"synced": 0})

    def execute(self, context: PrimitiveContext) -> PrimitiveOutcome:
        self.transitions.append(context.transition_name)
        self.inputs.append(dict(context.input_data))
        self.contexts.append(context)
        if context.transition_name in self.fail_once and context.transition_name not in self.failed:
            self.failed.add(context.transition_name)
            raise PrimitiveExecutionError(f"{context.transition_name} failed once")
        if context.transition_name == "find_issue_pull_requests":
            return PrimitiveOutcome(
                {
                    "has_pull_request": False,
                    "pull_request_count": 0,
                    "pull_requests": [],
                    "pull_request_number": 0,
                    "pull_request_url": "",
                    "pull_request_title": "",
                    "pull_request_head_ref": "",
                    "pull_request_head_sha": "",
                    "pull_request_source_ref": "",
                }
            )
        if context.transition_name == "allocate_workspace":
            return PrimitiveOutcome(
                {
                    "worktree_path": "/tmp/worktree",
                    "branch": "symphony/test",
                    "base_repo_path": "/tmp/repo.git",
                    "commit_sha": "abc123",
                }
            )
        if context.transition_name == "create_draft_pr":
            return PrimitiveOutcome(
                {
                    "pull_request_number": 12,
                    "pull_request_url": "https://github.com/dbcli/litecli/pull/12",
                    "pull_request_title": "Fix logging path support",
                    "head_ref": "symphony/existing-pr",
                    "head_sha": "abc123",
                }
            )
        if self.pr_feedback and context.transition_name in {"check_pr_ci", "refresh_pr_ci"}:
            return PrimitiveOutcome(
                {
                    "failed_checks": [{"name": "tests", "conclusion": "failure"}],
                    "checks": [{"name": "tests", "conclusion": "failure"}],
                    "state": "failure",
                    "conclusion": "failure",
                }
            )
        if self.pr_feedback and context.transition_name == "fetch_ci_failure_context":
            return PrimitiveOutcome(
                {
                    "failure_context": [{"name": "tests", "log_excerpt": "failing test"}],
                    "sha": "abc123",
                    "unavailable_reason": "",
                }
            )
        if self.pr_feedback and context.transition_name in {"check_pr_comments", "refresh_pr_comments"}:
            return PrimitiveOutcome({"comments": [{"body": "Please add a regression test."}]})
        if self.pr_feedback and context.transition_name in {
            "check_pr_mergeability",
            "refresh_pr_mergeability",
        }:
            return PrimitiveOutcome(
                {
                    "has_conflicts": False,
                    "mergeable": True,
                    "mergeable_state": "clean",
                    "head_sha": "abc123",
                }
            )
        return PrimitiveOutcome({"transition": context.transition_name})


def _config_with_artifact_handoff() -> WorkflowConfig:
    config = default_config()
    transitions = dict(config.workflow.transitions)
    transitions["allocate_workspace"] = replace(
        transitions["allocate_workspace"],
        outputs={"worktree_path": "artifact.workspace"},
    )
    transitions["run_setup"] = replace(
        transitions["run_setup"],
        inputs={"worktree_path": "artifact.workspace"},
    )
    transitions["fix_issue"] = replace(
        transitions["fix_issue"],
        inputs={"worktree_path": "outputs.allocate_workspace.worktree_path"},
    )
    workflow = replace(config.workflow, transitions=transitions)
    return replace(config, workflow=workflow)


def _config_with_transition_retry(transition_name: str, *, retry_limit: int) -> WorkflowConfig:
    config = default_config()
    transitions = dict(config.workflow.transitions)
    transitions[transition_name] = replace(transitions[transition_name], retry_limit=retry_limit)
    return replace(config, workflow=replace(config.workflow, transitions=transitions))


def _seed_store(tmp_path: Path) -> Store:
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
            task_type="code",
        )
    )
    return store


def _source_work_item_repositories(tmp_path: Path) -> tuple[SourceRepository, WorkItemRepository]:
    engine = create_db_engine(str(tmp_path / "symphony.db"))
    create_model_tables(engine)
    session_factory = create_session_factory(engine)
    return SourceRepository(session_factory), WorkItemRepository(session_factory)


def _work_item_and_run(tmp_path: Path, work_item_id: int) -> tuple[WorkItem, WorkItemRun]:
    session_factory = create_session_factory(create_db_engine(str(tmp_path / "symphony.db")))
    with session_factory() as session:
        work_item = session.get_one(WorkItem, work_item_id)
        run = session.get_one(WorkItemRun, 1)
        return work_item, run


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)
