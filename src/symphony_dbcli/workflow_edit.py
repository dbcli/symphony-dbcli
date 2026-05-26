from __future__ import annotations

import difflib
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .actions import DEFAULT_ACTION_REGISTRY
from .config import FENCE_RE, WorkflowConfig, WorkflowError, parse_workflow


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
            proposed = normalize_workflow_edit_output(
                current_content,
                model.propose(current_content, cleaned_request),
            )
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
    normalized_content = normalize_workflow_edit_output(current_content, proposed_content)
    return WorkflowEditProposal(
        request=request.strip(),
        current_content=current_content,
        proposed_content=normalized_content,
        diff=_diff(current_content, normalized_content),
        error=_validation_error(normalized_content),
    )


def parsed_config(content: str) -> WorkflowConfig:
    return parse_workflow(content)


def normalize_workflow_edit_output(current_content: str, output: str) -> str:
    candidate = _extract_workflow_markdown(output)
    applied_diff = _apply_unified_diff(current_content, candidate)
    if applied_diff is not None:
        return applied_diff
    replaced_toml = _replace_toml_block(current_content, candidate)
    if (
        replaced_toml is not None
        and not _looks_like_complete_workflow_markdown(candidate)
        and not _validation_error(replaced_toml)
    ):
        return replaced_toml
    if not _validation_error(candidate):
        return _ensure_trailing_newline(candidate)
    if replaced_toml is not None and not _validation_error(replaced_toml):
        return replaced_toml
    return _ensure_trailing_newline(candidate)


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
            "--sandbox",
            self.config.codex.sandbox,
            "-c",
            f'approval_policy="{self.config.codex.approval_policy}"',
        ]
        if self.config.codex.workflow_edit_reasoning_effort:
            command.extend(
                [
                    "-c",
                    f'model_reasoning_effort="{self.config.codex.workflow_edit_reasoning_effort}"',
                ]
            )
        workflow_edit_model = self.config.codex.workflow_edit_model or self.config.codex.model
        if workflow_edit_model:
            command.extend(["--model", workflow_edit_model])
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


def _ensure_trailing_newline(content: str) -> str:
    return content.rstrip() + "\n" if content.strip() else ""


def _replace_toml_block(current_content: str, candidate: str) -> str | None:
    current_match = FENCE_RE.search(current_content)
    candidate_match = FENCE_RE.search(candidate)
    if current_match is None or candidate_match is None:
        return None
    toml_body = candidate_match.group("body").strip()
    if not toml_body:
        return None
    return (
        current_content[: current_match.start("body")]
        + toml_body
        + current_content[current_match.end("body") :]
    )


def _looks_like_complete_workflow_markdown(content: str) -> bool:
    return "# Symphony DBCLI Workflow" in content or "## Worker Instructions" in content


def _apply_unified_diff(current_content: str, candidate: str) -> str | None:
    lines = candidate.splitlines()
    if not any(line.startswith("@@ ") for line in lines):
        return None
    try:
        patched = _apply_unified_diff_lines(current_content.splitlines(), lines)
    except ValueError:
        return None
    return "\n".join(patched) + "\n"


def _apply_unified_diff_lines(original_lines: Sequence[str], diff_lines: Sequence[str]) -> list[str]:
    output: list[str] = []
    original_index = 0
    diff_index = 0
    while diff_index < len(diff_lines):
        line = diff_lines[diff_index]
        if not line.startswith("@@ "):
            diff_index += 1
            continue
        old_start = _parse_hunk_old_start(line)
        hunk_start = old_start - 1
        if hunk_start < original_index or hunk_start > len(original_lines):
            raise ValueError("hunk start does not match current content")
        output.extend(original_lines[original_index:hunk_start])
        original_index = hunk_start
        diff_index += 1
        while diff_index < len(diff_lines) and not diff_lines[diff_index].startswith("@@ "):
            diff_line = diff_lines[diff_index]
            if diff_line.startswith("\\ No newline at end of file"):
                diff_index += 1
                continue
            if not diff_line:
                raise ValueError("invalid unified diff line")
            marker = diff_line[0]
            value = diff_line[1:]
            if marker == " ":
                if not _original_line_matches(original_lines, original_index, value):
                    raise ValueError("context line does not match current content")
                output.append(original_lines[original_index])
                original_index += 1
            elif marker == "-":
                if not _original_line_matches(original_lines, original_index, value):
                    raise ValueError("removed line does not match current content")
                original_index += 1
            elif marker == "+":
                output.append(value)
            elif diff_line.startswith(("--- ", "+++ ", "diff --git ", "index ")):
                pass
            else:
                raise ValueError("invalid unified diff line")
            diff_index += 1
    output.extend(original_lines[original_index:])
    return output


def _parse_hunk_old_start(header: str) -> int:
    match = re.match(r"@@ -(?P<start>\d+)(?:,\d+)? \+(?:\d+)(?:,\d+)? @@", header)
    if match is None:
        raise ValueError("invalid unified diff hunk header")
    return int(match.group("start"))


def _original_line_matches(lines: Sequence[str], index: int, expected: str) -> bool:
    return index < len(lines) and lines[index] == expected


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
Do not return a unified diff, patch, or partial snippet.
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
