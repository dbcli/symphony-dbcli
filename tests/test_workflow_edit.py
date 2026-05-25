from __future__ import annotations

from symphony_dbcli.config import default_config, render_workflow
from symphony_dbcli.workflow_edit import (
    WorkflowEditError,
    propose_workflow_edit,
    propose_workflow_edit_with_model,
    validate_workflow_edit,
)


def test_propose_workflow_edit_appends_conversation_note() -> None:
    current = render_workflow(default_config())

    proposal = propose_workflow_edit(current, "Prefer unit tests before integration tests.")

    assert proposal.valid
    assert "Conversation workflow edit:" in proposal.proposed_content
    assert "Prefer unit tests before integration tests." in proposal.proposed_content
    assert "WORKFLOW.md:proposed" in proposal.diff


def test_validate_workflow_edit_reports_invalid_toml() -> None:
    current = render_workflow(default_config())

    proposal = validate_workflow_edit(current, "not a workflow", "break it")

    assert not proposal.valid
    assert "WORKFLOW.md must contain one fenced toml config block." in proposal.error


def test_propose_workflow_edit_can_use_model() -> None:
    current = render_workflow(default_config())
    edited = current.replace(
        'additional_instructions = ""',
        'additional_instructions = "Keep support replies under two sentences."',
    )

    proposal = propose_workflow_edit_with_model(
        current,
        "Keep support replies under two sentences.",
        model=FakeWorkflowEditModel(edited),
    )

    assert proposal.valid
    assert "Keep support replies under two sentences." in proposal.proposed_content
    assert "additional_instructions" in proposal.diff


def test_propose_workflow_edit_reports_model_failure() -> None:
    current = render_workflow(default_config())

    proposal = propose_workflow_edit_with_model(
        current,
        "Change the workflow.",
        model=FailingWorkflowEditModel(),
    )

    assert not proposal.valid
    assert proposal.proposed_content == current
    assert "model unavailable" in proposal.error


class FakeWorkflowEditModel:
    def __init__(self, proposed: str) -> None:
        self.proposed = proposed

    def propose(self, current_content: str, request: str) -> str:
        return self.proposed


class FailingWorkflowEditModel:
    def propose(self, current_content: str, request: str) -> str:
        raise WorkflowEditError("model unavailable")
