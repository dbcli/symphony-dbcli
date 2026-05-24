from __future__ import annotations

import difflib
from dataclasses import dataclass

from .config import WorkflowConfig, WorkflowError, parse_workflow


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
    cleaned_request = request.strip()
    proposed = (
        _append_instruction_note(current_content, cleaned_request) if cleaned_request else current_content
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
