from __future__ import annotations

import difflib
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from symphony_dbcli.config import CodexConfig, default_config, render_workflow
from symphony_dbcli.workflow_edit import (
    CodexWorkflowEditModel,
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


def test_propose_workflow_edit_normalizes_unified_diff_from_model() -> None:
    current = render_workflow(default_config())
    edited = current.replace("dry_run = true", "dry_run = false")
    model_output = "\n".join(
        difflib.unified_diff(
            current.splitlines(),
            edited.splitlines(),
            fromfile="WORKFLOW.md:current",
            tofile="WORKFLOW.md:proposed",
            lineterm="",
        )
    )

    proposal = propose_workflow_edit_with_model(
        current,
        "Disable dry run.",
        model=FakeWorkflowEditModel(model_output),
    )

    assert proposal.valid
    assert proposal.proposed_content == edited
    assert "dry_run = false" in proposal.proposed_content


def test_validate_workflow_edit_normalizes_toml_only_output() -> None:
    current = render_workflow(default_config())
    edited = current.replace("dry_run = true", "dry_run = false")
    toml_body = edited.split("```toml\n", 1)[1].split("\n```", 1)[0]

    proposal = validate_workflow_edit(current, f"```toml\n{toml_body}\n```", "Disable dry run.")

    assert proposal.valid
    assert proposal.proposed_content.startswith("# Symphony DBCLI Workflow")
    assert "dry_run = false" in proposal.proposed_content
    assert "## Worker Instructions" in proposal.proposed_content


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


def test_codex_workflow_edit_model_uses_current_exec_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = render_workflow(default_config())
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        *,
        text: bool,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=current, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = replace(
        default_config(),
        codex=CodexConfig(command="codex", approval_policy="never", sandbox="workspace-write"),
    )

    proposal = CodexWorkflowEditModel(config, tmp_path).propose(current, "Keep replies short.")

    assert proposal == current
    assert commands
    command = commands[0]
    assert "--ask-for-approval" not in command
    assert command[:6] == ["codex", "exec", "--cd", str(tmp_path), "--sandbox", "workspace-write"]
    assert "-c" in command
    assert 'approval_policy="never"' in command
    assert 'model_reasoning_effort="low"' in command
    assert command[command.index("--model") + 1] == "gpt-5.4-mini"


def test_codex_workflow_edit_model_falls_back_to_worker_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = render_workflow(default_config())
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        *,
        text: bool,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=current, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = replace(
        default_config(),
        codex=CodexConfig(model="gpt-worker", workflow_edit_model="", workflow_edit_reasoning_effort=""),
    )

    proposal = CodexWorkflowEditModel(config, tmp_path).propose(current, "Keep replies short.")

    assert proposal == current
    command = commands[0]
    assert command[command.index("--model") + 1] == "gpt-worker"
    assert 'model_reasoning_effort="low"' not in command


class FakeWorkflowEditModel:
    def __init__(self, proposed: str) -> None:
        self.proposed = proposed

    def propose(self, current_content: str, request: str) -> str:
        return self.proposed


class FailingWorkflowEditModel:
    def propose(self, current_content: str, request: str) -> str:
        raise WorkflowEditError("model unavailable")
