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
    transition_failure_counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowTransitionMatch:
    name: str
    transition: WorkflowTransitionConfig


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
    if normalized == 'task.type == "code"':
        return context.task_type == "code"
    if normalized == 'task.type == "research"':
        return context.task_type == "research"
    if normalized == "pull_request.is_merged":
        return context.pull_request_is_merged
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
    return normalized in {
        "",
        'task.type == "code"',
        'task.type == "research"',
        "pull_request.is_merged",
    }
