from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from .actions import DEFAULT_ACTION_REGISTRY

type TriggerKind = Literal["automatic", "human"]
type SetupRunPolicy = Literal["per_attempt", "per_repo", "manual"]
type SetupWorkingDirectory = Literal["workspace", "repo"]
type ConfigTable = dict[str, Any]


TRIGGER_KINDS = frozenset({"automatic", "human"})
SETUP_RUN_POLICIES = frozenset({"per_attempt", "per_repo", "manual"})
SETUP_WORKING_DIRECTORIES = frozenset({"workspace", "repo"})
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class WorkflowStateConfig:
    description: str = ""
    terminal: bool = False
    gate: str = ""


@dataclass(frozen=True)
class WorkflowTransitionConfig:
    from_state: str
    to_state: str
    action: str
    trigger: TriggerKind = "automatic"
    parallel_group: str = ""
    description: str = ""
    condition: str = ""
    gate: str = ""
    on_failure: str = "failed"
    retry_limit: int = 0
    timeout_seconds: int = 0
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    guidance: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WorkflowDefinitionConfig:
    initial_state: str = "todo"
    terminal_states: list[str] = field(default_factory=lambda: ["done", "failed", "blocked"])
    states: dict[str, WorkflowStateConfig] = field(default_factory=dict)
    transitions: dict[str, WorkflowTransitionConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerPreferencesConfig:
    run_review_after_code_change: bool = True
    preferred_test_strategy: str = "unit"
    require_tests_for_code_changes: bool = True
    coding_style: list[str] = field(
        default_factory=lambda: [
            "Keep changes focused on the issue.",
            "Prefer narrow unit tests before broader integration tests.",
            "Avoid unrelated refactors.",
        ]
    )
    additional_instructions: str = ""


@dataclass(frozen=True)
class SetupStepConfig:
    command: list[str]
    description: str = ""
    run: SetupRunPolicy = "per_attempt"
    cwd: SetupWorkingDirectory = "workspace"
    timeout_seconds: int = 600
    blocks_worker: bool = True


@dataclass(frozen=True)
class SetupConfig:
    enabled: bool = True
    steps: dict[str, SetupStepConfig] = field(default_factory=dict)


def default_workflow_definition() -> WorkflowDefinitionConfig:
    return WorkflowDefinitionConfig(
        initial_state="todo",
        terminal_states=["done", "failed", "blocked"],
        states={
            "todo": WorkflowStateConfig("Issue is eligible for Symphony dispatch."),
            "claimed": WorkflowStateConfig("Issue has been claimed and labeled as working."),
            "associated_pr_checked": WorkflowStateConfig("Durable issue-to-PR bookkeeping has been checked."),
            "workspace_ready": WorkflowStateConfig("An isolated workspace has been prepared."),
            "setup_complete": WorkflowStateConfig("Configured setup steps have completed."),
            "worker_complete": WorkflowStateConfig("Codex has produced a durable worker result."),
            "review": WorkflowStateConfig(
                "Human review is required before GitHub side effects.", gate="review"
            ),
            "pr_ready": WorkflowStateConfig("A draft pull request exists and is waiting for merge."),
            "pr_refreshed": WorkflowStateConfig("Latest pull request metadata has been fetched."),
            "pr_checks_complete": WorkflowStateConfig("CI, PR comments, and mergeability have been checked."),
            "pr_feedback_context_ready": WorkflowStateConfig(
                "Pull request follow-up context has been prepared for Codex."
            ),
            "pr_follow_up_complete": WorkflowStateConfig(
                "Codex has addressed pull request feedback and needs human review.",
                gate="review_pr_feedback",
            ),
            "pr_waiting": WorkflowStateConfig(
                "Pull request has no immediate Symphony follow-up and is waiting for external activity.",
                gate="check_pr_again",
            ),
            "done": WorkflowStateConfig("Workflow completed successfully.", terminal=True),
            "failed": WorkflowStateConfig("Workflow failed and requires inspection.", terminal=True),
            "blocked": WorkflowStateConfig("Workflow is blocked by a human decision.", terminal=True),
        },
        transitions={
            "claim_issue": WorkflowTransitionConfig(
                from_state="todo",
                to_state="claimed",
                action="github.apply_labels",
                description="Move a dispatchable issue into the working state.",
                retry_limit=1,
                guidance=[
                    "Keep label changes minimal and explainable.",
                    "Do not alter labels unrelated to Symphony state.",
                ],
            ),
            "find_issue_pull_requests": WorkflowTransitionConfig(
                from_state="claimed",
                to_state="associated_pr_checked",
                action="github.find_issue_pull_requests",
                description="Find pull requests durably associated with this issue.",
                retry_limit=1,
                outputs={
                    "has_pull_request": "artifact.pull_request.exists",
                    "pull_request_count": "artifact.pull_request.count",
                    "pull_requests": "artifact.pull_request.associated",
                    "pull_request_number": "artifact.pull_request.number",
                    "pull_request_url": "artifact.pull_request.url",
                    "pull_request_title": "artifact.pull_request.title",
                    "pull_request_head_ref": "artifact.pull_request.head_ref",
                    "pull_request_head_sha": "artifact.pull_request.head_sha",
                    "pull_request_source_ref": "artifact.pull_request.source_ref",
                },
                guidance=[
                    "Trust only DB bookkeeping or the exact Symphony issue-link marker in the PR body.",
                    "Ignore incidental issue mentions to avoid false positive associations.",
                ],
            ),
            "allocate_workspace": WorkflowTransitionConfig(
                from_state="associated_pr_checked",
                to_state="workspace_ready",
                action="workspace.allocate",
                description="Create the per-attempt workspace.",
                retry_limit=1,
                inputs={
                    "branch": "artifact.pull_request.head_ref",
                    "source_ref": "artifact.pull_request.source_ref",
                },
                guidance=[
                    "Prefer isolated worktrees so concurrent workers do not share a checkout.",
                    "When an associated PR exists, resume from that PR branch.",
                    "Use deterministic paths and branch names that are easy to inspect.",
                ],
            ),
            "run_setup": WorkflowTransitionConfig(
                from_state="workspace_ready",
                to_state="setup_complete",
                action="workspace.run_setup",
                description="Run configured setup commands before Codex starts.",
                retry_limit=1,
                guidance=[
                    "Run only setup commands declared in this workflow.",
                    "Treat blocking setup failures as worker-blocking failures.",
                ],
            ),
            "research_issue": WorkflowTransitionConfig(
                from_state="setup_complete",
                to_state="worker_complete",
                action="codex.research_issue",
                description="Use Codex to draft a research or support answer.",
                condition='task.type == "research" and not pull_request.exists',
                retry_limit=1,
                guidance=[
                    "Draft a concise support answer in the user's voice.",
                    "Keep the reply under two sentences unless the issue requires concrete steps.",
                    "Cite specific files, commands, or issue facts when they matter.",
                    "Include the draft reply text in the final agent response; do not save it only to a filesystem path.",
                ],
            ),
            "fix_issue": WorkflowTransitionConfig(
                from_state="setup_complete",
                to_state="worker_complete",
                action="codex.fix_issue",
                description="Use Codex to implement a code change.",
                condition='task.type == "code" and not pull_request.exists',
                retry_limit=1,
                guidance=[
                    "Keep the code change focused on the issue.",
                    "Prefer narrow unit tests before broader integration tests.",
                    "Run a review pass after implementation when the workflow asks for it.",
                ],
            ),
            "check_pr_ci": WorkflowTransitionConfig(
                from_state="setup_complete",
                to_state="pr_checks_complete",
                action="github.fetch_ci_status",
                parallel_group="initial_pr_checks",
                description="Fetch current CI status for the associated pull request.",
                condition="pull_request.exists",
                retry_limit=1,
                inputs={"pull_request_number": "artifact.pull_request.number"},
                outputs={
                    "failed_checks": "artifact.ci.failed_checks",
                    "checks": "artifact.ci.checks",
                    "state": "artifact.ci.state",
                    "conclusion": "artifact.ci.conclusion",
                },
                guidance=[
                    "Capture both failed checks and the full check list for follow-up workers.",
                ],
            ),
            "check_pr_comments": WorkflowTransitionConfig(
                from_state="setup_complete",
                to_state="pr_checks_complete",
                action="github.fetch_pr_review_comments",
                parallel_group="initial_pr_checks",
                description="Fetch review-body and inline comments for the associated pull request.",
                condition="pull_request.exists",
                retry_limit=1,
                inputs={"pull_request_number": "artifact.pull_request.number"},
                outputs={"comments": "artifact.review_comments.comments"},
                guidance=[
                    "Keep review-body comments and inline comments together for the worker.",
                ],
            ),
            "check_pr_mergeability": WorkflowTransitionConfig(
                from_state="setup_complete",
                to_state="pr_checks_complete",
                action="github.detect_merge_conflicts",
                parallel_group="initial_pr_checks",
                description="Detect merge conflicts for the associated pull request.",
                condition="pull_request.exists",
                retry_limit=1,
                inputs={"pull_request_number": "artifact.pull_request.number"},
                outputs={
                    "has_conflicts": "artifact.pull_request.has_conflicts",
                    "mergeable": "artifact.pull_request.mergeable",
                    "mergeable_state": "artifact.pull_request.mergeable_state",
                    "head_sha": "artifact.pull_request.head_sha",
                },
                guidance=[
                    "Treat GitHub mergeability as a snapshot that can change after new commits.",
                ],
            ),
            "address_pr_feedback": WorkflowTransitionConfig(
                from_state="pr_feedback_context_ready",
                to_state="pr_follow_up_complete",
                action="codex.address_pr_feedback",
                description="Ask Codex to address PR feedback from CI, comments, and mergeability.",
                condition="pull_request.needs_follow_up",
                retry_limit=1,
                inputs={
                    "pull_request_number": "artifact.pull_request.number",
                    "failed_checks": "artifact.ci.failed_checks",
                    "failure_context": "artifact.ci.failure_context",
                    "checks": "artifact.ci.checks",
                    "comments": "artifact.review_comments.comments",
                    "has_conflicts": "artifact.pull_request.has_conflicts",
                    "mergeable_state": "artifact.pull_request.mergeable_state",
                },
                guidance=[
                    "Address only the PR feedback captured in workflow artifacts.",
                    "Keep the update focused and preserve the original issue intent.",
                    "Prefer the narrowest local test that validates the change.",
                ],
            ),
            "fetch_ci_failure_context": WorkflowTransitionConfig(
                from_state="pr_checks_complete",
                to_state="pr_feedback_context_ready",
                action="github.fetch_ci_failure_context",
                description="Fetch bounded logs and annotations for failed CI checks.",
                condition="ci.has_failures",
                retry_limit=1,
                inputs={
                    "pull_request_number": "artifact.pull_request.number",
                    "failed_checks": "artifact.ci.failed_checks",
                },
                outputs={
                    "failure_context": "artifact.ci.failure_context",
                    "unavailable_reason": "artifact.ci.failure_context_unavailable_reason",
                },
                guidance=[
                    "Prefer failure excerpts, check annotations, and concise summaries over full CI logs.",
                ],
            ),
            "skip_ci_failure_context": WorkflowTransitionConfig(
                from_state="pr_checks_complete",
                to_state="pr_feedback_context_ready",
                action="workflow.noop",
                description="Continue without CI failure logs when CI has no failures.",
                condition="not ci.has_failures",
                retry_limit=1,
                guidance=[
                    "Only fetch CI failure logs when failed checks are present.",
                ],
            ),
            "push_pr_feedback_fix": WorkflowTransitionConfig(
                from_state="pr_follow_up_complete",
                to_state="pr_waiting",
                action="github.push_pr_update",
                trigger="human",
                gate="review_pr_feedback",
                description="Review and push the PR feedback fix to the existing pull request branch.",
                retry_limit=1,
                guidance=[
                    "Let the human inspect the follow-up fix before pushing it to GitHub.",
                ],
            ),
            "wait_existing_pr": WorkflowTransitionConfig(
                from_state="pr_feedback_context_ready",
                to_state="pr_waiting",
                action="workflow.noop",
                description="Wait when the associated PR has no current Symphony follow-up.",
                condition="not pull_request.needs_follow_up",
                retry_limit=1,
                guidance=[
                    "Stop automatic work until a human asks Symphony to check the pull request again.",
                ],
            ),
            "request_review": WorkflowTransitionConfig(
                from_state="worker_complete",
                to_state="review",
                action="github.apply_labels",
                description="Move completed worker output into human review.",
                condition='task.type == "research"',
                retry_limit=1,
                guidance=[
                    "Preserve worker output for human review before external side effects.",
                    "Avoid posting comments or opening PRs in this step.",
                ],
            ),
            "post_answer": WorkflowTransitionConfig(
                from_state="review",
                to_state="done",
                action="github.post_issue_comment",
                trigger="human",
                gate="review_answer",
                description="Post an edited research answer to GitHub.",
                condition='task.type == "research"',
                retry_limit=1,
                guidance=[
                    "Let the human edit the final reply before posting.",
                    "Keep the posted response succinct and avoid unnecessary caveats.",
                ],
            ),
            "auto_create_draft_pr": WorkflowTransitionConfig(
                from_state="worker_complete",
                to_state="pr_ready",
                action="github.create_draft_pr",
                description="Create a draft pull request automatically after the code worker finishes.",
                condition='task.type == "code"',
                retry_limit=1,
                outputs={
                    "pull_request_number": "artifact.pull_request.number",
                    "pull_request_url": "artifact.pull_request.url",
                    "pull_request_title": "artifact.pull_request.title",
                    "head_ref": "artifact.pull_request.head_ref",
                    "head_sha": "artifact.pull_request.head_sha",
                },
                guidance=[
                    "Use the worker's PR title and body when they describe the actual code change.",
                    "Generate a reviewable body from the worker result if the worker body is only a link or marker.",
                    "Create only a draft pull request.",
                    "Require the PR description to include the correct hidden Symphony source marker.",
                    "Use a GitHub issue URL only when the source item is a real GitHub issue.",
                ],
            ),
            "create_draft_pr": WorkflowTransitionConfig(
                from_state="review",
                to_state="pr_ready",
                action="github.create_draft_pr",
                trigger="human",
                gate="review_diff",
                description="Create a draft pull request from a reviewed code attempt.",
                condition='task.type == "code"',
                retry_limit=2,
                outputs={
                    "pull_request_number": "artifact.pull_request.number",
                    "pull_request_url": "artifact.pull_request.url",
                    "pull_request_title": "artifact.pull_request.title",
                    "head_ref": "artifact.pull_request.head_ref",
                    "head_sha": "artifact.pull_request.head_sha",
                },
                guidance=[
                    "Use the worker's PR title and body when they describe the actual code change.",
                    "Generate a reviewable body from the worker result if the worker body is only a link or marker.",
                    "Create only a draft pull request.",
                    "Require the PR description to include the correct hidden Symphony source marker.",
                    "Use a GitHub issue URL only when the source item is a real GitHub issue.",
                ],
            ),
            "wait_created_pr": WorkflowTransitionConfig(
                from_state="pr_ready",
                to_state="pr_waiting",
                action="workflow.noop",
                description="Wait for external PR activity after creating a draft pull request.",
                retry_limit=1,
                guidance=[
                    "Do not immediately reprocess a freshly created draft PR.",
                    "Wait for CI, review, or an explicit human check.",
                ],
            ),
            "check_pr_again": WorkflowTransitionConfig(
                from_state="pr_waiting",
                to_state="pr_refreshed",
                action="github.fetch_pull_request",
                trigger="human",
                gate="check_pr_again",
                description="Manually check the pull request again for merge, CI, or review changes.",
                retry_limit=1,
                inputs={"pull_request_number": "artifact.pull_request.number"},
                outputs={
                    "is_merged": "artifact.pull_request.is_merged",
                    "head_ref": "artifact.pull_request.head_ref",
                    "head_sha": "artifact.pull_request.head_sha",
                    "state": "artifact.pull_request.state",
                },
                guidance=[
                    "Use this when GitHub activity has changed and Symphony should reassess the PR.",
                ],
            ),
            "cleanup_after_merge": WorkflowTransitionConfig(
                from_state="pr_refreshed",
                to_state="done",
                action="workspace.cleanup_after_merge",
                description="Clean up the workspace after the pull request is merged.",
                condition="pull_request.is_merged",
                retry_limit=1,
                guidance=[
                    "Clean only workspaces owned by Symphony.",
                    "Do not remove worktrees with uncommitted changes.",
                ],
            ),
            "refresh_pr_ci": WorkflowTransitionConfig(
                from_state="pr_refreshed",
                to_state="pr_checks_complete",
                action="github.fetch_ci_status",
                parallel_group="refreshed_pr_checks",
                description="Refresh CI status for an unmerged pull request.",
                condition="not pull_request.is_merged",
                retry_limit=1,
                inputs={"pull_request_number": "artifact.pull_request.number"},
                outputs={
                    "failed_checks": "artifact.ci.failed_checks",
                    "checks": "artifact.ci.checks",
                    "state": "artifact.ci.state",
                    "conclusion": "artifact.ci.conclusion",
                },
                guidance=[
                    "Capture both failed checks and the full check list for follow-up workers.",
                ],
            ),
            "refresh_pr_comments": WorkflowTransitionConfig(
                from_state="pr_refreshed",
                to_state="pr_checks_complete",
                action="github.fetch_pr_review_comments",
                parallel_group="refreshed_pr_checks",
                description="Refresh review-body and inline comments for an unmerged pull request.",
                condition="not pull_request.is_merged",
                retry_limit=1,
                inputs={"pull_request_number": "artifact.pull_request.number"},
                outputs={"comments": "artifact.review_comments.comments"},
                guidance=[
                    "Keep review-body comments and inline comments together for the worker.",
                ],
            ),
            "refresh_pr_mergeability": WorkflowTransitionConfig(
                from_state="pr_refreshed",
                to_state="pr_checks_complete",
                action="github.detect_merge_conflicts",
                parallel_group="refreshed_pr_checks",
                description="Refresh mergeability for an unmerged pull request.",
                condition="not pull_request.is_merged",
                retry_limit=1,
                inputs={"pull_request_number": "artifact.pull_request.number"},
                outputs={
                    "has_conflicts": "artifact.pull_request.has_conflicts",
                    "mergeable": "artifact.pull_request.mergeable",
                    "mergeable_state": "artifact.pull_request.mergeable_state",
                    "head_sha": "artifact.pull_request.head_sha",
                },
                guidance=[
                    "Treat GitHub mergeability as a snapshot that can change after new commits.",
                ],
            ),
            "mark_blocked": WorkflowTransitionConfig(
                from_state="review",
                to_state="blocked",
                action="github.apply_labels",
                trigger="human",
                gate="mark_blocked",
                description="Let a human stop progress when review cannot continue.",
                retry_limit=1,
                guidance=[
                    "Use this only when human review cannot continue safely.",
                    "Leave enough context in the dashboard for a future retry.",
                ],
            ),
        },
    )


def default_worker_preferences() -> WorkerPreferencesConfig:
    return WorkerPreferencesConfig()


def default_setup_config() -> SetupConfig:
    return SetupConfig()


def workflow_definition_from_dict(data: ConfigTable) -> WorkflowDefinitionConfig:
    if not data:
        return default_workflow_definition()
    states = {
        name: _state_from_dict(name, state_data) for name, state_data in _nested_table(data, "states").items()
    }
    transitions = {
        name: _transition_from_dict(name, transition_data)
        for name, transition_data in _nested_table(data, "transitions").items()
    }
    return WorkflowDefinitionConfig(
        initial_state=_str_value(data, "initial_state", default_workflow_definition().initial_state),
        terminal_states=_str_list(data, "terminal_states", default_workflow_definition().terminal_states),
        states=states or default_workflow_definition().states,
        transitions=transitions or default_workflow_definition().transitions,
    )


def worker_preferences_from_dict(data: ConfigTable) -> WorkerPreferencesConfig:
    if not data:
        return default_worker_preferences()
    return WorkerPreferencesConfig(
        run_review_after_code_change=_bool_value(data, "run_review_after_code_change", True),
        preferred_test_strategy=_str_value(data, "preferred_test_strategy", "unit"),
        require_tests_for_code_changes=_bool_value(data, "require_tests_for_code_changes", True),
        coding_style=_str_list(data, "coding_style", default_worker_preferences().coding_style),
        additional_instructions=_str_value(data, "additional_instructions", ""),
    )


def setup_config_from_dict(data: ConfigTable) -> SetupConfig:
    if not data:
        return default_setup_config()
    steps = {
        name: _setup_step_from_dict(name, step_data)
        for name, step_data in _nested_table(data, "steps").items()
    }
    return SetupConfig(
        enabled=_bool_value(data, "enabled", True),
        steps=steps,
    )


def validate_workflow_definition(workflow: WorkflowDefinitionConfig) -> list[str]:
    errors: list[str] = []
    if not workflow.states:
        errors.append("workflow.states must include at least one state.")
        return errors
    if workflow.initial_state not in workflow.states:
        errors.append(f"workflow.initial_state '{workflow.initial_state}' is not defined in workflow.states.")
    for state_name in workflow.states:
        if not NAME_RE.match(state_name):
            errors.append(f"workflow state '{state_name}' must use letters, numbers, hyphen, or underscore.")
    terminal_states = set(workflow.terminal_states)
    for state_name in terminal_states:
        state = workflow.states.get(state_name)
        if state is None:
            errors.append(f"workflow.terminal_states references undefined state '{state_name}'.")
        elif not state.terminal:
            errors.append(f"workflow state '{state_name}' must set terminal = true.")
    for transition_name, transition in workflow.transitions.items():
        errors.extend(_transition_errors(transition_name, transition, workflow.states, terminal_states))
    errors.extend(_parallel_group_errors(workflow.transitions))
    errors.extend(_unreachable_state_errors(workflow))
    return errors


def validate_worker_preferences(preferences: WorkerPreferencesConfig) -> list[str]:
    errors: list[str] = []
    if preferences.preferred_test_strategy not in {"unit", "integration", "balanced", "project_default"}:
        errors.append(
            "preferences.preferred_test_strategy must be 'unit', 'integration', 'balanced', or 'project_default'."
        )
    if any(not item.strip() for item in preferences.coding_style):
        errors.append("preferences.coding_style entries must not be empty.")
    return errors


def validate_setup_config(setup: SetupConfig) -> list[str]:
    errors: list[str] = []
    for step_name, step in setup.steps.items():
        if not NAME_RE.match(step_name):
            errors.append(f"setup step '{step_name}' must use letters, numbers, hyphen, or underscore.")
        if not step.command:
            errors.append(f"setup.steps.{step_name}.command must include at least one argument.")
        if any(not argument.strip() for argument in step.command):
            errors.append(f"setup.steps.{step_name}.command entries must not be empty.")
        if step.run not in SETUP_RUN_POLICIES:
            errors.append(f"setup.steps.{step_name}.run must be 'per_attempt', 'per_repo', or 'manual'.")
        if step.cwd not in SETUP_WORKING_DIRECTORIES:
            errors.append(f"setup.steps.{step_name}.cwd must be 'workspace' or 'repo'.")
        if step.timeout_seconds < 1:
            errors.append(f"setup.steps.{step_name}.timeout_seconds must be at least 1.")
    return errors


def _state_from_dict(name: str, data: Any) -> WorkflowStateConfig:
    table = _table_value(data, f"workflow.states.{name}")
    return WorkflowStateConfig(
        description=_str_value(table, "description", ""),
        terminal=_bool_value(table, "terminal", False),
        gate=_str_value(table, "gate", ""),
    )


def _transition_from_dict(name: str, data: Any) -> WorkflowTransitionConfig:
    table = _table_value(data, f"workflow.transitions.{name}")
    return WorkflowTransitionConfig(
        from_state=_required_str(table, "from_state", f"workflow.transitions.{name}"),
        to_state=_required_str(table, "to_state", f"workflow.transitions.{name}"),
        action=_required_str(table, "action", f"workflow.transitions.{name}"),
        trigger=_trigger_kind(_str_value(table, "trigger", "automatic")),
        parallel_group=_str_value(table, "parallel_group", ""),
        description=_str_value(table, "description", ""),
        condition=_str_value(table, "condition", ""),
        gate=_str_value(table, "gate", ""),
        on_failure=_str_value(table, "on_failure", "failed"),
        retry_limit=_int_value(table, "retry_limit", 0),
        timeout_seconds=_int_value(table, "timeout_seconds", 0),
        inputs=_str_map(table, "inputs", f"workflow.transitions.{name}"),
        outputs=_str_map(table, "outputs", f"workflow.transitions.{name}"),
        guidance=_str_list(table, "guidance", []),
    )


def _setup_step_from_dict(name: str, data: Any) -> SetupStepConfig:
    table = _table_value(data, f"setup.steps.{name}")
    return SetupStepConfig(
        command=_required_str_list(table, "command", f"setup.steps.{name}"),
        description=_str_value(table, "description", ""),
        run=_setup_run_policy(_str_value(table, "run", "per_attempt")),
        cwd=_setup_working_directory(_str_value(table, "cwd", "workspace")),
        timeout_seconds=_int_value(table, "timeout_seconds", 600),
        blocks_worker=_bool_value(table, "blocks_worker", True),
    )


def _transition_errors(
    name: str,
    transition: WorkflowTransitionConfig,
    states: dict[str, WorkflowStateConfig],
    terminal_states: set[str],
) -> list[str]:
    errors: list[str] = []
    if not NAME_RE.match(name):
        errors.append(f"workflow transition '{name}' must use letters, numbers, hyphen, or underscore.")
    if transition.from_state not in states:
        errors.append(
            f"workflow.transitions.{name}.from_state references undefined state '{transition.from_state}'."
        )
    if transition.to_state not in states:
        errors.append(
            f"workflow.transitions.{name}.to_state references undefined state '{transition.to_state}'."
        )
    if transition.on_failure and transition.on_failure not in states:
        errors.append(
            f"workflow.transitions.{name}.on_failure references undefined state '{transition.on_failure}'."
        )
    primitive = DEFAULT_ACTION_REGISTRY.get(transition.action)
    if primitive is None:
        errors.append(f"workflow.transitions.{name}.action '{transition.action}' is not a known primitive.")
    elif transition.trigger == "automatic" and not primitive.automatic_allowed:
        errors.append(f"workflow.transitions.{name}.action '{transition.action}' cannot run automatically.")
    elif transition.trigger == "human" and not primitive.human_gate_allowed:
        errors.append(
            f"workflow.transitions.{name}.action '{transition.action}' cannot run behind a human gate."
        )
    if primitive is not None:
        errors.extend(_mapping_errors(name, "inputs", transition.inputs, primitive.input_fields))
        errors.extend(_mapping_errors(name, "outputs", transition.outputs, primitive.output_fields))
    if transition.trigger not in TRIGGER_KINDS:
        errors.append(f"workflow.transitions.{name}.trigger must be 'automatic' or 'human'.")
    if transition.trigger == "human" and not transition.gate:
        errors.append(f"workflow.transitions.{name}.gate is required for human transitions.")
    if transition.parallel_group:
        if not NAME_RE.match(transition.parallel_group):
            errors.append(f"workflow.transitions.{name}.parallel_group must use a valid workflow name.")
        if transition.trigger != "automatic":
            errors.append(
                f"workflow.transitions.{name}.parallel_group is only supported for automatic transitions."
            )
    if transition.from_state in terminal_states:
        errors.append(f"workflow.transitions.{name}.from_state must not be terminal.")
    if transition.retry_limit < 0:
        errors.append(f"workflow.transitions.{name}.retry_limit must be at least 0.")
    if transition.timeout_seconds < 0:
        errors.append(f"workflow.transitions.{name}.timeout_seconds must be at least 0.")
    if any(not item.strip() for item in transition.guidance):
        errors.append(f"workflow.transitions.{name}.guidance entries must not be empty.")
    return errors


def _mapping_errors(
    transition_name: str,
    mapping_name: str,
    mapping: dict[str, str],
    allowed_fields: frozenset[str],
) -> list[str]:
    unknown = sorted(set(mapping) - allowed_fields)
    return [
        f"workflow.transitions.{transition_name}.{mapping_name}.{field} is not valid for this primitive."
        for field in unknown
    ]


def _parallel_group_errors(transitions: dict[str, WorkflowTransitionConfig]) -> list[str]:
    errors: list[str] = []
    grouped: dict[str, list[tuple[str, WorkflowTransitionConfig]]] = {}
    for name, transition in transitions.items():
        if transition.parallel_group:
            grouped.setdefault(transition.parallel_group, []).append((name, transition))
    for group, members in grouped.items():
        if len(members) < 2:
            errors.append(f"workflow parallel_group '{group}' must include at least two transitions.")
            continue
        from_states = {transition.from_state for _, transition in members}
        to_states = {transition.to_state for _, transition in members}
        if len(from_states) != 1 or len(to_states) != 1:
            errors.append(
                f"workflow parallel_group '{group}' transitions must share the same from_state and to_state."
            )
    return errors


def _unreachable_state_errors(workflow: WorkflowDefinitionConfig) -> list[str]:
    reachable = {workflow.initial_state}
    transitions = list(workflow.transitions.values())
    changed = True
    while changed:
        changed = False
        for transition in transitions:
            if transition.from_state in reachable:
                for target in (transition.to_state, transition.on_failure):
                    if target and target not in reachable:
                        reachable.add(target)
                        changed = True
    unreachable = sorted(set(workflow.states) - reachable)
    return [
        f"workflow state '{state}' is not reachable from '{workflow.initial_state}'." for state in unreachable
    ]


def _nested_table(data: ConfigTable, key: str) -> ConfigTable:
    value = data.get(key, {})
    return _table_value(value, key)


def _table_value(value: Any, path: str) -> ConfigTable:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a TOML table.")
    return value


def _required_str(data: ConfigTable, key: str, path: str) -> str:
    if key not in data:
        raise ValueError(f"{path}.{key} is required.")
    return _str_value(data, key, "")


def _str_value(data: ConfigTable, key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string.")
    return value


def _bool_value(data: ConfigTable, key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false.")
    return value


def _int_value(data: ConfigTable, key: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer.")
    return value


def _str_list(data: ConfigTable, key: str, default: list[str]) -> list[str]:
    value = data.get(key, default)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of strings.")
    return list(value)


def _str_map(data: ConfigTable, key: str, path: str) -> dict[str, str]:
    value = data.get(key, {})
    if not isinstance(value, dict) or any(
        not isinstance(field, str) or not isinstance(target, str) for field, target in value.items()
    ):
        raise ValueError(f"{path}.{key} must be a table of string keys and values.")
    return dict(value)


def _required_str_list(data: ConfigTable, key: str, path: str) -> list[str]:
    if key not in data:
        raise ValueError(f"{path}.{key} is required.")
    return _str_list(data, key, [])


def _trigger_kind(value: str) -> TriggerKind:
    if value not in TRIGGER_KINDS:
        raise ValueError("trigger must be 'automatic' or 'human'.")
    return cast(TriggerKind, value)


def _setup_run_policy(value: str) -> SetupRunPolicy:
    if value not in SETUP_RUN_POLICIES:
        raise ValueError("run must be 'per_attempt', 'per_repo', or 'manual'.")
    return cast(SetupRunPolicy, value)


def _setup_working_directory(value: str) -> SetupWorkingDirectory:
    if value not in SETUP_WORKING_DIRECTORIES:
        raise ValueError("cwd must be 'workspace' or 'repo'.")
    return cast(SetupWorkingDirectory, value)
