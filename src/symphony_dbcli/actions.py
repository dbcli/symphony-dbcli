from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

type PrimitiveSideEffect = Literal["none", "github_read", "github_write", "workspace_write", "codex_worker"]
type IdempotencyStrategy = Literal[
    "repo_poll",
    "issue_snapshot",
    "issue_transition",
    "attempt_transition",
    "pull_request",
    "issue_comment",
]


@dataclass(frozen=True)
class PrimitiveSpec:
    name: str
    input_type: str
    output_type: str
    side_effect: PrimitiveSideEffect
    idempotency_strategy: IdempotencyStrategy
    automatic_allowed: bool
    human_gate_allowed: bool
    description: str
    input_fields: frozenset[str] = field(default_factory=frozenset)
    output_fields: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class ActionRegistry:
    primitives: dict[str, PrimitiveSpec]

    def names(self) -> set[str]:
        return set(self.primitives)

    def get(self, name: str) -> PrimitiveSpec | None:
        return self.primitives.get(name)

    def contains(self, name: str) -> bool:
        return name in self.primitives


def default_action_registry() -> ActionRegistry:
    specs = [
        PrimitiveSpec(
            name="github.fetch_issues",
            input_type="GitHubIssueQuery",
            output_type="IssueSnapshotList",
            side_effect="github_read",
            idempotency_strategy="repo_poll",
            automatic_allowed=True,
            human_gate_allowed=False,
            description="Fetch dispatchable GitHub issues for configured repositories.",
            input_fields=frozenset({"repos", "labels"}),
            output_fields=frozenset({"synced", "issues"}),
        ),
        PrimitiveSpec(
            name="github.fetch_issue",
            input_type="GitHubIssueReference",
            output_type="IssueSnapshot",
            side_effect="github_read",
            idempotency_strategy="issue_snapshot",
            automatic_allowed=True,
            human_gate_allowed=False,
            description="Fetch one GitHub issue and persist its latest snapshot.",
            input_fields=frozenset({"repo", "issue_number"}),
            output_fields=frozenset({"issue"}),
        ),
        PrimitiveSpec(
            name="github.fetch_comments",
            input_type="GitHubIssueReference",
            output_type="GitHubCommentList",
            side_effect="github_read",
            idempotency_strategy="issue_snapshot",
            automatic_allowed=True,
            human_gate_allowed=False,
            description="Fetch issue or pull request comments for worker context.",
            input_fields=frozenset({"repo", "issue_number"}),
            output_fields=frozenset({"comments"}),
        ),
        PrimitiveSpec(
            name="github.apply_labels",
            input_type="GitHubLabelChange",
            output_type="GitHubLabelChangeResult",
            side_effect="github_write",
            idempotency_strategy="issue_transition",
            automatic_allowed=True,
            human_gate_allowed=True,
            description="Apply or remove labels for a workflow state transition.",
            input_fields=frozenset({"repo", "issue_number", "labels_added", "labels_removed"}),
            output_fields=frozenset({"dry_run", "labels_added", "labels_removed"}),
        ),
        PrimitiveSpec(
            name="github.create_draft_pr",
            input_type="DraftPullRequestRequest",
            output_type="PullRequestSnapshot",
            side_effect="github_write",
            idempotency_strategy="pull_request",
            automatic_allowed=True,
            human_gate_allowed=True,
            description="Push an attempt branch and create a draft pull request.",
            input_fields=frozenset(
                {"attempt_id", "repo", "issue_number", "worktree_path", "branch", "title", "body"}
            ),
            output_fields=frozenset(
                {
                    "pull_request_number",
                    "pull_request_url",
                    "pull_request_title",
                    "state",
                    "merged_at",
                }
            ),
        ),
        PrimitiveSpec(
            name="github.post_issue_comment",
            input_type="IssueCommentRequest",
            output_type="IssueCommentSnapshot",
            side_effect="github_write",
            idempotency_strategy="issue_comment",
            automatic_allowed=True,
            human_gate_allowed=True,
            description="Post an approved issue comment to GitHub.",
            input_fields=frozenset({"comment_id", "body", "repo", "issue_number"}),
            output_fields=frozenset({"comment_id", "comment_url", "attempt_id", "repo", "issue_number"}),
        ),
        PrimitiveSpec(
            name="github.fetch_pull_request",
            input_type="PullRequestReference",
            output_type="PullRequestSnapshot",
            side_effect="github_read",
            idempotency_strategy="pull_request",
            automatic_allowed=True,
            human_gate_allowed=False,
            description="Fetch pull request metadata for review, merge, and cleanup decisions.",
            input_fields=frozenset({"repo", "pull_request_number"}),
            output_fields=frozenset({"pull_request_number", "state", "merged_at", "is_merged"}),
        ),
        PrimitiveSpec(
            name="github.fetch_ci_status",
            input_type="PullRequestReference",
            output_type="CiStatusSnapshot",
            side_effect="github_read",
            idempotency_strategy="pull_request",
            automatic_allowed=True,
            human_gate_allowed=False,
            description="Fetch CI/check-run status for a pull request.",
            input_fields=frozenset({"repo", "pull_request_number"}),
            output_fields=frozenset({"state", "conclusion", "failed_checks"}),
        ),
        PrimitiveSpec(
            name="codex.research_issue",
            input_type="CodexIssueTask",
            output_type="WorkerResult",
            side_effect="codex_worker",
            idempotency_strategy="attempt_transition",
            automatic_allowed=True,
            human_gate_allowed=True,
            description="Ask Codex to research an issue and draft an answer.",
            input_fields=frozenset({"attempt_id", "repo", "issue_number", "task_type", "worktree_path"}),
            output_fields=frozenset({"thread_id", "turn_count", "duration_ms", "message_chars"}),
        ),
        PrimitiveSpec(
            name="codex.fix_issue",
            input_type="CodexIssueTask",
            output_type="WorkerResult",
            side_effect="codex_worker",
            idempotency_strategy="attempt_transition",
            automatic_allowed=True,
            human_gate_allowed=True,
            description="Ask Codex to implement a fix for a coding issue.",
            input_fields=frozenset({"attempt_id", "repo", "issue_number", "task_type", "worktree_path"}),
            output_fields=frozenset({"thread_id", "turn_count", "duration_ms", "message_chars"}),
        ),
        PrimitiveSpec(
            name="codex.address_pr_comments",
            input_type="CodexPullRequestTask",
            output_type="WorkerResult",
            side_effect="codex_worker",
            idempotency_strategy="attempt_transition",
            automatic_allowed=True,
            human_gate_allowed=True,
            description="Ask Codex to address pull request review comments.",
            input_fields=frozenset({"attempt_id", "repo", "issue_number", "pull_request_number"}),
            output_fields=frozenset({"thread_id", "turn_count", "duration_ms", "message_chars"}),
        ),
        PrimitiveSpec(
            name="codex.fix_ci_failures",
            input_type="CodexPullRequestTask",
            output_type="WorkerResult",
            side_effect="codex_worker",
            idempotency_strategy="attempt_transition",
            automatic_allowed=True,
            human_gate_allowed=True,
            description="Ask Codex to inspect failing CI and apply a fix.",
            input_fields=frozenset({"attempt_id", "repo", "issue_number", "pull_request_number"}),
            output_fields=frozenset({"thread_id", "turn_count", "duration_ms", "message_chars"}),
        ),
        PrimitiveSpec(
            name="workspace.allocate",
            input_type="WorkspaceAllocationRequest",
            output_type="WorkspaceAllocationResult",
            side_effect="workspace_write",
            idempotency_strategy="attempt_transition",
            automatic_allowed=True,
            human_gate_allowed=False,
            description="Allocate an isolated worktree or clone for an attempt.",
            input_fields=frozenset({"repo", "issue_number", "attempt_id"}),
            output_fields=frozenset({"base_repo_path", "worktree_path", "branch", "commit_sha"}),
        ),
        PrimitiveSpec(
            name="workspace.run_setup",
            input_type="WorkspaceSetupRequest",
            output_type="WorkspaceSetupResult",
            side_effect="workspace_write",
            idempotency_strategy="attempt_transition",
            automatic_allowed=True,
            human_gate_allowed=True,
            description="Run configured setup commands inside the allocated workspace.",
            input_fields=frozenset({"attempt_id", "worktree_path"}),
            output_fields=frozenset({"steps"}),
        ),
        PrimitiveSpec(
            name="workspace.record_changes",
            input_type="WorkspaceChangeRequest",
            output_type="WorkspaceChangeResult",
            side_effect="workspace_write",
            idempotency_strategy="attempt_transition",
            automatic_allowed=True,
            human_gate_allowed=False,
            description="Record changed files, commits, and branch metadata for an attempt.",
            input_fields=frozenset({"attempt_id", "worktree_path", "branch", "commit_sha"}),
            output_fields=frozenset({"changed_files", "commit_sha", "has_changes"}),
        ),
        PrimitiveSpec(
            name="workspace.cleanup_after_merge",
            input_type="WorkspaceCleanupRequest",
            output_type="WorkspaceCleanupResult",
            side_effect="workspace_write",
            idempotency_strategy="pull_request",
            automatic_allowed=True,
            human_gate_allowed=True,
            description="Remove an attempt workspace after its pull request has merged.",
            input_fields=frozenset({"pull_request_id", "base_repo_path", "worktree_path"}),
            output_fields=frozenset({"removed", "reason", "worktree_path"}),
        ),
    ]
    return ActionRegistry({spec.name: spec for spec in specs})


DEFAULT_ACTION_REGISTRY = default_action_registry()
