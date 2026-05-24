from __future__ import annotations

from dataclasses import dataclass
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
        ),
    ]
    return ActionRegistry({spec.name: spec for spec in specs})


DEFAULT_ACTION_REGISTRY = default_action_registry()
