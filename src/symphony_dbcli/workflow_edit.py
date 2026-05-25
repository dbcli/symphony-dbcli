from __future__ import annotations

import difflib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .actions import DEFAULT_ACTION_REGISTRY
from .config import WorkflowConfig, WorkflowError, parse_workflow


class WorkflowEditError(RuntimeError):
    """Raised when a conversational workflow edit cannot be generated."""


class WorkflowEditModel(Protocol):
    def propose(self, current_content: str, request: str) -> str: ...


@dataclass(frozen=True)
class WorkflowEditProposal:
    request: str
    current_content: str
    proposed_content: str
    diff: str
    error: str

    @property
    def valid(self) -> bool:
        return not self.error


def propose_workflow_edit(current_content: str, request: str) -> WorkflowEditProposal:
    return propose_workflow_edit_with_model(current_content, request, model=None)


def propose_workflow_edit_with_model(
    current_content: str,
    request: str,
    *,
    model: WorkflowEditModel | None,
) -> WorkflowEditProposal:
    cleaned_request = request.strip()
    if not cleaned_request:
        proposed = current_content
        error = _validation_error(proposed)
        return WorkflowEditProposal(
            request=cleaned_request,
            current_content=current_content,
            proposed_content=proposed,
            diff=_diff(current_content, proposed),
            error=error,
        )
    if model is None:
        proposed = _append_instruction_note(current_content, cleaned_request)
    else:
        try:
            proposed = model.propose(current_content, cleaned_request)
        except WorkflowEditError as exc:
            return WorkflowEditProposal(
                request=cleaned_request,
                current_content=current_content,
                proposed_content=current_content,
                diff="",
                error=str(exc),
            )
    error = _validation_error(proposed)
    return WorkflowEditProposal(
        request=cleaned_request,
        current_content=current_content,
        proposed_content=proposed,
        diff=_diff(current_content, proposed),
        error=error,
    )


def validate_workflow_edit(current_content: str, proposed_content: str, request: str) -> WorkflowEditProposal:
    return WorkflowEditProposal(
        request=request.strip(),
        current_content=current_content,
        proposed_content=proposed_content,
        diff=_diff(current_content, proposed_content),
        error=_validation_error(proposed_content),
    )


def parsed_config(content: str) -> WorkflowConfig:
    return parse_workflow(content)


@dataclass(frozen=True)
class CodexWorkflowEditModel:
    config: WorkflowConfig
    cwd: Path

    def propose(self, current_content: str, request: str) -> str:
        command = [
            self.config.codex.command,
            "exec",
            "--cd",
            str(self.cwd),
            "--ask-for-approval",
            self.config.codex.approval_policy,
        ]
        if self.config.codex.model:
            command.extend(["--model", self.config.codex.model])
        command.append(_workflow_edit_prompt(current_content, request))
        try:
            result = subprocess.run(command, text=True, capture_output=True, check=False)
        except OSError as exc:
            raise WorkflowEditError(str(exc)) from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or "codex workflow edit failed"
            raise WorkflowEditError(detail)
        proposed = _extract_workflow_markdown(result.stdout)
        if not proposed.strip():
            raise WorkflowEditError("Codex did not return a WORKFLOW.md proposal.")
        return proposed


def _append_instruction_note(content: str, request: str) -> str:
    if not request:
        return content
    note = f"\n\nConversation workflow edit:\n- {request}\n"
    if "## Worker Instructions" in content:
        return content.rstrip() + note
    return content.rstrip() + "\n\n## Worker Instructions" + note


def _diff(before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="WORKFLOW.md:current",
            tofile="WORKFLOW.md:proposed",
            lineterm="",
        )
    )


def _validation_error(content: str) -> str:
    try:
        parse_workflow(content)
    except WorkflowError as exc:
        return str(exc)
    return ""


def _workflow_edit_prompt(current_content: str, request: str) -> str:
    primitives = "\n".join(f"- {name}" for name in sorted(DEFAULT_ACTION_REGISTRY.names()))
    return f"""\
You are editing WORKFLOW.md for symphony-dbcli.

Return only the complete updated WORKFLOW.md markdown. Do not include commentary.
Keep unrelated configuration and prose unchanged. Preserve one fenced toml block.
The result must validate with the symphony-dbcli workflow parser.

Supported workflow conditions:
- task.type == "code"
- task.type == "research"
- pull_request.exists
- pull_request.is_merged
- pull_request.has_conflicts
- pull_request.needs_follow_up
- ci.has_failures
- review_comments.present
- not <any supported condition>
- <condition> and <condition>
- <condition> or <condition>

Known primitives:
{primitives}

Change request:
{request}

Current WORKFLOW.md:
{current_content}
"""


def _extract_workflow_markdown(output: str) -> str:
    stripped = output.strip()
    markdown_match = re.fullmatch(r"```(?:markdown|md)?\s*\n(?P<body>.*?)\n```", stripped, re.DOTALL)
    if markdown_match:
        return markdown_match.group("body").strip() + "\n"
    return stripped + "\n"
