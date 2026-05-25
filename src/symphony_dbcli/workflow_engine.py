from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from .workflow_definition import WorkflowDefinitionConfig, WorkflowTransitionConfig

type WorkflowTrigger = Literal["automatic", "human"]


class WorkflowEngineError(RuntimeError):
    """Raised when a workflow definition cannot be evaluated."""


@dataclass(frozen=True)
class WorkflowExecutionContext:
    task_type: str
    pull_request_is_merged: bool = False
    artifacts: Mapping[str, object] = field(default_factory=dict)
    transition_failure_counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowTransitionMatch:
    name: str
    transition: WorkflowTransitionConfig


@dataclass(frozen=True)
class WorkflowTransitionBatch:
    name: str
    transitions: list[WorkflowTransitionMatch]

    @property
    def from_state(self) -> str:
        return self.transitions[0].transition.from_state

    @property
    def to_state(self) -> str:
        return self.transitions[0].transition.to_state

    @property
    def is_parallel(self) -> bool:
        return len(self.transitions) > 1


class WorkflowEngine:
    def __init__(self, workflow: WorkflowDefinitionConfig):
        self.workflow = workflow

    def matching_transitions(
        self,
        *,
        from_state: str,
        trigger: WorkflowTrigger,
        context: WorkflowExecutionContext,
        actions: set[str] | None = None,
    ) -> list[WorkflowTransitionMatch]:
        return [
            WorkflowTransitionMatch(name, transition)
            for name, transition in self.workflow.transitions.items()
            if transition.from_state == from_state
            and transition.trigger == trigger
            and (actions is None or transition.action in actions)
            and condition_matches(transition.condition, context)
            and transition_retry_available(name, transition, context)
        ]

    def single_transition(
        self,
        *,
        from_state: str,
        trigger: WorkflowTrigger,
        context: WorkflowExecutionContext,
        actions: set[str] | None = None,
    ) -> WorkflowTransitionMatch | None:
        matches = self.matching_transitions(
            from_state=from_state,
            trigger=trigger,
            context=context,
            actions=actions,
        )
        if len(matches) > 1:
            names = ", ".join(match.name for match in matches)
            raise WorkflowEngineError(f"Multiple workflow transitions match {from_state}: {names}.")
        if not matches:
            exhausted = self.exhausted_transitions(
                from_state=from_state,
                trigger=trigger,
                context=context,
                actions=actions,
            )
            if exhausted:
                names = ", ".join(match.name for match in exhausted)
                raise WorkflowEngineError(f"Workflow transition retry limit exceeded: {names}.")
        return matches[0] if matches else None

    def automatic_batch(
        self,
        *,
        from_state: str,
        context: WorkflowExecutionContext,
        actions: set[str] | None = None,
    ) -> WorkflowTransitionBatch | None:
        exhausted = self.exhausted_transitions(
            from_state=from_state,
            trigger="automatic",
            context=context,
            actions=actions,
        )
        if exhausted:
            names = ", ".join(match.name for match in exhausted)
            raise WorkflowEngineError(f"Workflow transition retry limit exceeded: {names}.")
        matches = self.matching_transitions(
            from_state=from_state,
            trigger="automatic",
            context=context,
            actions=actions,
        )
        if not matches:
            return None
        if len(matches) == 1:
            return WorkflowTransitionBatch(matches[0].name, matches)
        group_names = {match.transition.parallel_group for match in matches}
        if len(group_names) == 1 and "" not in group_names:
            group_name = next(iter(group_names))
            from_states = {match.transition.from_state for match in matches}
            to_states = {match.transition.to_state for match in matches}
            if len(from_states) == 1 and len(to_states) == 1:
                return WorkflowTransitionBatch(group_name, matches)
        names = ", ".join(match.name for match in matches)
        raise WorkflowEngineError(f"Multiple workflow transitions match {from_state}: {names}.")

    def exhausted_transitions(
        self,
        *,
        from_state: str,
        trigger: WorkflowTrigger,
        context: WorkflowExecutionContext,
        actions: set[str] | None = None,
    ) -> list[WorkflowTransitionMatch]:
        return [
            WorkflowTransitionMatch(name, transition)
            for name, transition in self.workflow.transitions.items()
            if transition.from_state == from_state
            and transition.trigger == trigger
            and (actions is None or transition.action in actions)
            and condition_matches(transition.condition, context)
            and not transition_retry_available(name, transition, context)
        ]


def condition_matches(condition: str, context: WorkflowExecutionContext) -> bool:
    normalized = condition.strip()
    if not normalized:
        return True
    if " or " in normalized:
        return any(condition_matches(part, context) for part in normalized.split(" or "))
    if " and " in normalized:
        return all(condition_matches(part, context) for part in normalized.split(" and "))
    if normalized.startswith("not "):
        return not condition_matches(normalized.removeprefix("not "), context)
    if normalized == 'task.type == "code"':
        return context.task_type == "code"
    if normalized == 'task.type == "research"':
        return context.task_type == "research"
    if normalized == "pull_request.is_merged":
        return context.pull_request_is_merged or _truthy_artifact(
            context,
            "pull_request.is_merged",
            "fetch_pull_request.is_merged",
        )
    if normalized == "pull_request.exists":
        return _truthy_artifact(
            context,
            "pull_request.number",
            "find_issue_pull_requests.has_pull_request",
        )
    if normalized == "pull_request.has_conflicts":
        return _truthy_artifact(
            context,
            "pull_request.has_conflicts",
            "detect_merge_conflicts.has_conflicts",
        )
    if normalized == "ci.has_failures":
        return _truthy_artifact(context, "ci.failed_checks", "fetch_ci_status.failed_checks")
    if normalized == "review_comments.present":
        return _truthy_artifact(
            context,
            "review_comments.comments",
            "fetch_pr_review_comments.comments",
        )
    if normalized == "pull_request.needs_follow_up":
        return (
            condition_matches("pull_request.has_conflicts", context)
            or condition_matches("ci.has_failures", context)
            or condition_matches("review_comments.present", context)
        )
    raise WorkflowEngineError(f"Unsupported workflow condition: {condition}")


def transition_retry_available(
    transition_name: str,
    transition: WorkflowTransitionConfig,
    context: WorkflowExecutionContext,
) -> bool:
    failure_count = context.transition_failure_counts.get(transition_name, 0)
    return failure_count <= transition.retry_limit


def validate_condition(condition: str) -> bool:
    normalized = condition.strip()
    if " or " in normalized:
        return all(validate_condition(part) for part in normalized.split(" or "))
    if " and " in normalized:
        return all(validate_condition(part) for part in normalized.split(" and "))
    if normalized.startswith("not "):
        normalized = normalized.removeprefix("not ").strip()
    return normalized in {
        "",
        'task.type == "code"',
        'task.type == "research"',
        "pull_request.exists",
        "pull_request.is_merged",
        "pull_request.has_conflicts",
        "pull_request.needs_follow_up",
        "ci.has_failures",
        "review_comments.present",
    }


def _truthy_artifact(context: WorkflowExecutionContext, *names: str) -> bool:
    for name in names:
        if name in context.artifacts:
            return bool(context.artifacts[name])
    return False
