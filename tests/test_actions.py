from __future__ import annotations

from symphony_dbcli.actions import DEFAULT_ACTION_REGISTRY
from symphony_dbcli.config import default_config


def test_default_action_registry_covers_default_workflow() -> None:
    config = default_config()

    missing = [
        transition.action
        for transition in config.workflow.transitions.values()
        if not DEFAULT_ACTION_REGISTRY.contains(transition.action)
    ]

    assert missing == []


def test_action_registry_records_execution_boundaries() -> None:
    draft_pr = DEFAULT_ACTION_REGISTRY.get("github.create_draft_pr")
    codex_draft_pr = DEFAULT_ACTION_REGISTRY.get("codex.create_draft_pr")
    fetch_issues = DEFAULT_ACTION_REGISTRY.get("github.fetch_issues")
    find_issue_prs = DEFAULT_ACTION_REGISTRY.get("github.find_issue_pull_requests")
    ci_failure_context = DEFAULT_ACTION_REGISTRY.get("github.fetch_ci_failure_context")
    pr_comments = DEFAULT_ACTION_REGISTRY.get("github.fetch_pr_review_comments")
    merge_conflicts = DEFAULT_ACTION_REGISTRY.get("github.detect_merge_conflicts")
    address_feedback = DEFAULT_ACTION_REGISTRY.get("codex.address_pr_feedback")
    operations_task = DEFAULT_ACTION_REGISTRY.get("codex.operations_task")
    push_update = DEFAULT_ACTION_REGISTRY.get("github.push_pr_update")
    source_sync = DEFAULT_ACTION_REGISTRY.get("source.sync")
    work_item_move = DEFAULT_ACTION_REGISTRY.get("work_item.move")
    noop = DEFAULT_ACTION_REGISTRY.get("workflow.noop")

    assert draft_pr is not None
    assert draft_pr.side_effect == "github_write"
    assert draft_pr.input_type == "DraftPullRequestRequest"
    assert draft_pr.output_type == "PullRequestSnapshot"
    assert draft_pr.idempotency_strategy == "pull_request"
    assert draft_pr.automatic_allowed is True
    assert draft_pr.human_gate_allowed is True
    assert "attempt_id" in draft_pr.input_fields
    assert "pull_request_url" in draft_pr.output_fields
    assert "issue_marker_present" in draft_pr.output_fields
    assert codex_draft_pr is not None
    assert codex_draft_pr.side_effect == "codex_worker"
    assert codex_draft_pr.automatic_allowed is False

    assert fetch_issues is not None
    assert fetch_issues.side_effect == "github_read"
    assert fetch_issues.automatic_allowed is True
    assert fetch_issues.human_gate_allowed is False
    assert "repos" in fetch_issues.input_fields
    assert "issues" in fetch_issues.output_fields

    assert find_issue_prs is not None
    assert find_issue_prs.side_effect == "github_read"
    assert find_issue_prs.output_type == "AssociatedPullRequestList"
    assert "pull_request_source_ref" in find_issue_prs.output_fields

    assert pr_comments is not None
    assert pr_comments.side_effect == "github_read"
    assert pr_comments.automatic_allowed is True
    assert pr_comments.human_gate_allowed is False
    assert "pull_request_number" in pr_comments.input_fields
    assert "comments" in pr_comments.output_fields

    assert merge_conflicts is not None
    assert merge_conflicts.side_effect == "github_read"
    assert merge_conflicts.output_type == "PullRequestMergeStatus"
    assert "has_conflicts" in merge_conflicts.output_fields

    assert ci_failure_context is not None
    assert ci_failure_context.side_effect == "github_read"
    assert ci_failure_context.output_type == "CiFailureContext"
    assert "failed_checks" in ci_failure_context.input_fields
    assert "failure_context" in ci_failure_context.output_fields

    assert address_feedback is not None
    assert address_feedback.side_effect == "codex_worker"
    assert "failed_checks" in address_feedback.input_fields
    assert "failure_context" in address_feedback.input_fields
    assert "comments" in address_feedback.input_fields
    assert "has_conflicts" in address_feedback.input_fields

    assert operations_task is not None
    assert operations_task.side_effect == "codex_worker"
    assert operations_task.input_type == "CodexOperationsTask"
    assert operations_task.output_type == "WorkerResult"
    assert operations_task.human_gate_allowed is True
    assert "worktree_path" in operations_task.input_fields
    allocate_workspace = DEFAULT_ACTION_REGISTRY.get("workspace.allocate")
    assert allocate_workspace is not None
    assert "reused_existing" in allocate_workspace.output_fields

    assert push_update is not None
    assert push_update.side_effect == "github_write"
    assert push_update.human_gate_allowed is True
    assert "commit_sha" in push_update.output_fields

    assert source_sync is not None
    assert source_sync.idempotency_strategy == "source_sync"
    assert source_sync.side_effect == "github_read"
    assert "source_id" in source_sync.input_fields

    assert work_item_move is not None
    assert work_item_move.idempotency_strategy == "work_item_transition"
    assert work_item_move.side_effect == "none"
    assert work_item_move.human_gate_allowed is True
    assert "target_state" in work_item_move.input_fields

    assert noop is not None
    assert noop.side_effect == "none"
    assert noop.automatic_allowed is True
