from __future__ import annotations

import pytest

from symphony_dbcli.config import default_config
from symphony_dbcli.workflow_engine import (
    WorkflowEngine,
    WorkflowEngineError,
    WorkflowExecutionContext,
    condition_matches,
    transition_retry_available,
)


def test_workflow_engine_selects_task_type_transition() -> None:
    engine = WorkflowEngine(default_config().workflow)

    code_match = engine.single_transition(
        from_state="setup_complete",
        trigger="automatic",
        context=WorkflowExecutionContext(task_type="code"),
        actions={"codex.fix_issue", "codex.research_issue"},
    )
    research_match = engine.single_transition(
        from_state="setup_complete",
        trigger="automatic",
        context=WorkflowExecutionContext(task_type="research"),
        actions={"codex.fix_issue", "codex.research_issue"},
    )

    assert code_match is not None
    assert code_match.name == "fix_issue"
    assert research_match is not None
    assert research_match.name == "research_issue"


def test_workflow_engine_lists_matching_human_gates() -> None:
    engine = WorkflowEngine(default_config().workflow)

    matches = engine.matching_transitions(
        from_state="review",
        trigger="human",
        context=WorkflowExecutionContext(task_type="code"),
    )

    assert [match.name for match in matches] == ["create_draft_pr", "mark_blocked"]


def test_workflow_engine_returns_parallel_batch_for_pr_checks() -> None:
    engine = WorkflowEngine(default_config().workflow)

    batch = engine.automatic_batch(
        from_state="setup_complete",
        context=WorkflowExecutionContext(
            task_type="code",
            artifacts={"pull_request.exists": True, "pull_request.number": 12},
        ),
    )

    assert batch is not None
    assert batch.name == "initial_pr_checks"
    assert batch.is_parallel is True
    assert {match.name for match in batch.transitions} == {
        "check_pr_ci",
        "check_pr_comments",
        "check_pr_mergeability",
    }


def test_condition_matching_rejects_unknown_conditions() -> None:
    assert condition_matches('task.type == "code"', WorkflowExecutionContext(task_type="code")) is True

    with pytest.raises(WorkflowEngineError, match="Unsupported workflow condition"):
        condition_matches("issue.priority == high", WorkflowExecutionContext(task_type="code"))


def test_condition_matching_reads_workflow_artifacts() -> None:
    context = WorkflowExecutionContext(
        task_type="code",
        artifacts={
            "ci.failed_checks": [{"name": "tests"}],
            "pull_request.has_conflicts": False,
            "review_comments.comments": [],
        },
    )

    assert condition_matches("ci.has_failures", context) is True
    assert condition_matches("not pull_request.has_conflicts", context) is True
    assert condition_matches("review_comments.present", context) is False
    assert (
        condition_matches(
            'task.type == "code" and ci.has_failures and not review_comments.present',
            context,
        )
        is True
    )
    assert condition_matches("ci.has_failures or review_comments.present", context) is True


def test_workflow_engine_blocks_transition_after_retry_limit() -> None:
    workflow = default_config().workflow
    engine = WorkflowEngine(workflow)
    transition = workflow.transitions["fix_issue"]

    assert transition_retry_available(
        "fix_issue",
        transition,
        WorkflowExecutionContext(
            task_type="code",
            transition_failure_counts={"fix_issue": transition.retry_limit},
        ),
    )
    assert not transition_retry_available(
        "fix_issue",
        transition,
        WorkflowExecutionContext(
            task_type="code",
            transition_failure_counts={"fix_issue": transition.retry_limit + 1},
        ),
    )

    with pytest.raises(WorkflowEngineError, match="Workflow transition retry limit exceeded: fix_issue"):
        engine.single_transition(
            from_state="setup_complete",
            trigger="automatic",
            context=WorkflowExecutionContext(
                task_type="code",
                transition_failure_counts={"fix_issue": transition.retry_limit + 1},
            ),
        )
