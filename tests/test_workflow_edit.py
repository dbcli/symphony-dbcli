from __future__ import annotations

from symphony_dbcli.config import default_config, render_workflow
from symphony_dbcli.dashboard import render_workflow_edit
from symphony_dbcli.workflow_edit import propose_workflow_edit, validate_workflow_edit


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


def test_render_workflow_edit_shows_diff_and_apply_control() -> None:
    current = render_workflow(default_config())
    proposal = propose_workflow_edit(current, "Keep support replies under two sentences.")

    html = render_workflow_edit(proposal=proposal)

    assert "Workflow Edit" in html
    assert "Proposed diff" in html
    assert "Keep support replies under two sentences." in html
    assert 'name="action" value="apply"' in html
