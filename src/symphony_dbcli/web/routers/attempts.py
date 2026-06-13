from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request, status
from starlette.background import BackgroundTask
from starlette.datastructures import FormData
from starlette.responses import RedirectResponse, Response

from symphony_dbcli.actions import DEFAULT_ACTION_REGISTRY
from symphony_dbcli.orchestrator import Orchestrator, OrchestratorError
from symphony_dbcli.web.dependencies import (
    BreadcrumbItem,
    WebAppState,
    get_app_state,
    page_context,
    templates,
    work_item_repository,
)
from symphony_dbcli.work_items import WorkItemAdjustment, WorkItemError

router = APIRouter(tags=["attempts"])
MAX_WORKSPACE_DIFF_CHARS = 120_000
MAX_UNTRACKED_DIFF_FILES = 25


@dataclass(frozen=True)
class WorkspaceDiffView:
    worktree_path: str
    base_commit_sha: str
    head_commit_sha: str
    changed_files: list[str]
    text: str
    error: str = ""
    truncated: bool = False


@router.get("/attempts/{attempt_id}")
def attempt_detail(request: Request, attempt_id: int) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="attempts/detail.html",
        context=_attempt_context(request, attempt_id),
    )


@router.post("/attempts/{attempt_id}/follow-up-code")
def create_code_follow_up(request: Request, attempt_id: int) -> Response:
    state = get_app_state(request)
    try:
        workflow = state.store.latest_workflow_version()
        workflow_version_id = int(workflow["id"]) if workflow else None
        target_attempt_id = state.store.create_code_follow_up_attempt(attempt_id, workflow_version_id)
    except (ValueError, sqlite3.Error) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return RedirectResponse(
        f"/attempts/{target_attempt_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/workflow-actions/{action_run_id}/retry")
def retry_workflow_action(
    request: Request,
    action_run_id: int,
    return_to: Annotated[str, Form()] = "",
) -> Response:
    state = get_app_state(request)
    action_run = state.store.workflow_action_run_by_id(action_run_id)
    if not action_run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow action run not found")
    attempt_id = action_run["attempt_id"]
    if attempt_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workflow action run is not associated with an attempt.",
        )
    try:
        Orchestrator(state.config, state.store).retry_failed_workflow_action(action_run_id)
    except OrchestratorError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return RedirectResponse(
        _safe_path(return_to) or f"/attempts/{int(attempt_id)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/attempts/{attempt_id}/adjustment-form")
def adjustment_form(request: Request, attempt_id: int, return_to: str = "") -> Response:
    return templates.TemplateResponse(
        request=request,
        name="attempts/_adjustment_modal.html",
        context=_adjustment_context(
            request,
            attempt_id,
            error="",
            note="",
            return_to=_safe_path(return_to),
        ),
    )


@router.post("/attempts/{attempt_id}/adjustment")
def request_adjustment(
    request: Request,
    attempt_id: int,
    note: Annotated[str, Form()],
    return_to: Annotated[str, Form()] = "",
) -> Response:
    state = get_app_state(request)
    attempt = state.store.attempt_by_id(attempt_id)
    if not attempt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")
    work_item_id = _attempt_work_item_id(attempt)
    if work_item_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only work-item attempts can request adjustments.",
        )
    try:
        work_item_repository(request).request_adjustment(
            WorkItemAdjustment(
                work_item_id=work_item_id,
                source_attempt_id=attempt_id,
                note=note,
            )
        )
    except WorkItemError as exc:
        return templates.TemplateResponse(
            request=request,
            name="attempts/_adjustment_modal.html",
            context=_adjustment_context(
                request,
                attempt_id,
                error=str(exc),
                note=note,
                return_to=_safe_path(return_to),
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    safe_return_to = _safe_path(return_to) or f"/work-items/{work_item_id}"
    if _is_htmx(request):
        response: Response = Response(status_code=204, headers={"HX-Redirect": safe_return_to})
    else:
        response = RedirectResponse(safe_return_to, status_code=status.HTTP_303_SEE_OTHER)
    response.background = _schedule_runtime_cycle(request, trigger="attempt_adjustment")
    return response


@router.post("/attempts/{attempt_id}/draft-pr")
def create_draft_pr(request: Request, attempt_id: int) -> Response:
    state = get_app_state(request)
    gate = state.store.pending_workflow_gate_for_attempt(attempt_id, "create_draft_pr")
    if not gate:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No pending create_draft_pr workflow gate for this attempt.",
        )
    response = RedirectResponse(
        f"/attempts/{attempt_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.background = _run_or_schedule_gate(request, int(gate["id"]), {}, gate)
    return response


@router.post("/comments/{comment_id}/post")
def post_comment(
    request: Request,
    comment_id: int,
    body: Annotated[str, Form()] = "",
) -> Response:
    state = get_app_state(request)
    comment = state.store.comment_by_id(comment_id)
    if not comment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")
    attempt_id = comment["attempt_id"]
    if attempt_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Comment is not associated with a workflow attempt.",
        )
    gate = state.store.pending_workflow_gate_for_attempt(int(attempt_id), "post_answer")
    if not gate:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No pending post_answer workflow gate for this attempt.",
        )
    _run_gate(request, int(gate["id"]), {"comment_id": comment_id, "body": body})
    return RedirectResponse(
        f"/attempts/{int(attempt_id)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/workflow-gates/{gate_id}/run")
async def run_workflow_gate(request: Request, gate_id: int) -> Response:
    state = get_app_state(request)
    gate = state.store.workflow_gate_by_id(gate_id)
    if not gate or str(gate["status"]) != "pending":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow gate not found")
    form = await request.form()
    input_data = _gate_input_data(form)
    response = RedirectResponse(
        _safe_return_to(form, gate),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.background = _run_or_schedule_gate(request, gate_id, input_data, gate)
    return response


@router.get("/issues/{owner}/{name}/{number}")
def issue_detail(request: Request, owner: str, name: str, number: int) -> Response:
    state = get_app_state(request)
    repo = f"{owner}/{name}"
    context = page_context(request, title=f"{repo} #{number}", active="work_items")
    context["detail"] = state.store.issue_detail(repo, number)
    return templates.TemplateResponse(
        request=request,
        name="issues/detail.html",
        context=context,
    )


def _attempt_context(request: Request, attempt_id: int) -> dict[str, object]:
    state = get_app_state(request)
    detail = state.store.attempt_detail(attempt_id)
    pending_gates = state.store.pending_workflow_gates_for_attempt(attempt_id) if detail else []
    gate_transitions = {str(row["transition_name"]): row for row in pending_gates}
    context = page_context(request, title=f"Attempt {attempt_id}", active="work_items")
    context["detail"] = detail
    context["pending_gates"] = pending_gates
    context["create_draft_pr_gate"] = gate_transitions.get("create_draft_pr")
    context["running_create_draft_pr_gate"] = (
        state.store.running_workflow_gate_for_attempt(attempt_id, "create_draft_pr") if detail else None
    )
    context["workspace_diff"] = _attempt_workspace_diff(detail)
    context["post_answer_gate"] = gate_transitions.get("post_answer")
    context["return_to"] = f"/attempts/{attempt_id}"
    context["can_request_adjustment"] = _can_request_adjustment(detail)
    context["retryable_failed_workflow_actions"] = _retryable_failed_workflow_actions(state, detail)
    context["breadcrumbs"] = _attempt_breadcrumbs(request, detail, attempt_id)
    return context


def _retryable_failed_workflow_actions(
    state: WebAppState,
    detail: dict[str, Any] | None,
) -> list[sqlite3.Row]:
    if not detail:
        return []
    if str(detail["attempt"]["status"]) != "failed":
        return []
    seen: set[str] = set()
    retryable: list[sqlite3.Row] = []
    for row in reversed(detail["workflow_action_runs"]):
        if str(row["status"]) != "failed":
            continue
        transition_name = str(row["transition_name"])
        if transition_name in seen:
            continue
        seen.add(transition_name)
        transition = state.config.workflow.transitions.get(transition_name)
        if transition is None or transition.trigger != "automatic":
            continue
        primitive = DEFAULT_ACTION_REGISTRY.get(transition.action)
        if primitive is None or primitive.side_effect == "codex_worker":
            continue
        retryable.append(row)
    return retryable


def _can_request_adjustment(detail: dict[str, Any] | None) -> bool:
    if not detail:
        return False
    attempt = detail["attempt"]
    return _attempt_work_item_id(attempt) is not None and str(attempt["status"]) not in {"queued", "running"}


def _attempt_breadcrumbs(
    request: Request,
    detail: dict[str, Any] | None,
    attempt_id: int,
) -> list[BreadcrumbItem]:
    if not detail:
        return [BreadcrumbItem("Work Items", "/work-items"), BreadcrumbItem(f"Attempt {attempt_id}")]
    work_item_id = _attempt_work_item_id(detail["attempt"])
    if work_item_id is None:
        return [BreadcrumbItem("Work Items", "/work-items"), BreadcrumbItem(f"Attempt {attempt_id}")]
    work_item = work_item_repository(request).detail(work_item_id)
    if work_item is None:
        return [
            BreadcrumbItem("Work Items", "/work-items"),
            BreadcrumbItem(f"Work Item #{work_item_id}", f"/work-items/{work_item_id}"),
            BreadcrumbItem(f"Attempt {attempt_id}"),
        ]
    return [
        BreadcrumbItem("Board", f"/board/source/{work_item.source_id}"),
        BreadcrumbItem(f"Work Item #{work_item.id}", f"/work-items/{work_item.id}"),
        BreadcrumbItem(f"Attempt {attempt_id}"),
    ]


def _adjustment_context(
    request: Request,
    attempt_id: int,
    *,
    error: str,
    note: str,
    return_to: str,
) -> dict[str, object]:
    detail = get_app_state(request).store.attempt_detail(attempt_id)
    if not detail:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")
    if not _can_request_adjustment(detail):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This attempt cannot request a Codex adjustment.",
        )
    context = page_context(request, title=f"Adjust Attempt {attempt_id}", active="work_items")
    context["detail"] = detail
    context["error"] = error
    context["note"] = note
    context["return_to"] = return_to or f"/attempts/{attempt_id}"
    return context


def _attempt_workspace_diff(detail: dict[str, Any] | None) -> WorkspaceDiffView | None:
    if not detail:
        return None
    attempt = detail["attempt"]
    if str(attempt["task_type"]) != "code":
        return None
    worktree_path = str(attempt["worktree_path"] or "")
    if not worktree_path:
        return None
    return _workspace_diff(Path(worktree_path), base_commit_sha=str(attempt["commit_sha"] or ""))


def _workspace_diff(worktree: Path, *, base_commit_sha: str) -> WorkspaceDiffView:
    if not worktree.exists():
        return WorkspaceDiffView(
            worktree_path=str(worktree),
            base_commit_sha=base_commit_sha,
            head_commit_sha="",
            changed_files=[],
            text="",
            error=f"Workspace does not exist: {worktree}",
        )
    try:
        head_commit_sha = _git_stdout(worktree, ["rev-parse", "HEAD"])
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return WorkspaceDiffView(
            worktree_path=str(worktree),
            base_commit_sha=base_commit_sha,
            head_commit_sha="",
            changed_files=[],
            text="",
            error=str(exc),
        )

    sections: list[str] = []
    changed_files: set[str] = set()
    errors: list[str] = []

    if base_commit_sha:
        committed_diff, committed_error = _committed_diff(worktree, base_commit_sha)
        if committed_diff:
            sections.append(_diff_section(f"Committed changes since {base_commit_sha[:12]}", committed_diff))
        if committed_error:
            errors.append(committed_error)
        changed_files.update(_git_lines(worktree, ["diff", "--name-only", f"{base_commit_sha}..HEAD"]))

    uncommitted_diff = _git_diff(worktree, ["diff", "--find-renames", "--no-color", "HEAD"])
    if uncommitted_diff:
        sections.append(_diff_section("Uncommitted changes", uncommitted_diff))
    changed_files.update(_git_lines(worktree, ["diff", "--name-only", "HEAD"]))

    untracked_files = _git_lines(worktree, ["ls-files", "--others", "--exclude-standard"])
    changed_files.update(untracked_files)
    if untracked_files:
        untracked_sections = [
            _git_diff(
                worktree,
                ["diff", "--no-index", "--no-color", "--", "/dev/null", path],
                diff_exit_codes={0, 1},
            )
            for path in untracked_files[:MAX_UNTRACKED_DIFF_FILES]
        ]
        untracked_diff = "\n\n".join(section for section in untracked_sections if section)
        if untracked_diff:
            sections.append(_diff_section("Untracked files", untracked_diff))
        if len(untracked_files) > MAX_UNTRACKED_DIFF_FILES:
            sections.append(
                _diff_section(
                    "Untracked files omitted",
                    "\n".join(untracked_files[MAX_UNTRACKED_DIFF_FILES:]),
                )
            )

    diff_text = "\n\n".join(sections).strip()
    truncated = len(diff_text) > MAX_WORKSPACE_DIFF_CHARS
    if truncated:
        diff_text = (
            diff_text[:MAX_WORKSPACE_DIFF_CHARS].rstrip()
            + f"\n\n[Diff truncated at {MAX_WORKSPACE_DIFF_CHARS} characters.]"
        )
    return WorkspaceDiffView(
        worktree_path=str(worktree),
        base_commit_sha=base_commit_sha,
        head_commit_sha=head_commit_sha,
        changed_files=sorted(changed_files),
        text=diff_text,
        error=" ".join(errors),
        truncated=truncated,
    )


def _committed_diff(worktree: Path, base_commit_sha: str) -> tuple[str, str]:
    result = _git(
        worktree,
        ["diff", "--find-renames", "--no-color", f"{base_commit_sha}...HEAD"],
    )
    if result.returncode == 0:
        return result.stdout.strip(), ""
    fallback = _git(
        worktree,
        ["diff", "--find-renames", "--no-color", f"{base_commit_sha}..HEAD"],
    )
    if fallback.returncode == 0:
        return fallback.stdout.strip(), ""
    return "", _git_error(fallback)


def _diff_section(title: str, body: str) -> str:
    return f"## {title}\n{body.strip()}"


def _git_stdout(worktree: Path, args: list[str]) -> str:
    result = _git(worktree, args)
    if result.returncode != 0:
        raise RuntimeError(_git_error(result))
    return result.stdout.strip()


def _git_lines(worktree: Path, args: list[str]) -> list[str]:
    result = _git(worktree, args)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _git_diff(
    worktree: Path,
    args: list[str],
    *,
    diff_exit_codes: set[int] | None = None,
) -> str:
    result = _git(worktree, args)
    allowed = diff_exit_codes or {0}
    if result.returncode not in allowed:
        return ""
    return result.stdout.strip()


def _git(worktree: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )


def _git_error(result: subprocess.CompletedProcess[str]) -> str:
    return result.stderr.strip() or result.stdout.strip() or "git command failed"


def _attempt_work_item_id(attempt: sqlite3.Row) -> int | None:
    value = attempt["work_item_id"]
    if value is None:
        return None
    return int(value)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _safe_path(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        return ""
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or not candidate.startswith("/"):
        return ""
    return candidate


def _schedule_runtime_cycle(request: Request, *, trigger: str) -> BackgroundTask | None:
    state = get_app_state(request)
    if state.runtime is None:
        return None
    return BackgroundTask(state.runtime.run_cycle, trigger=trigger)


def _run_or_schedule_gate(
    request: Request,
    gate_id: int,
    input_data: dict[str, Any],
    gate: sqlite3.Row,
) -> BackgroundTask | None:
    state = get_app_state(request)
    if not _gate_runs_in_background(state, gate):
        _run_gate(request, gate_id, input_data)
        return None
    try:
        Orchestrator(state.config, state.store).start_human_gate(gate_id, input_data=input_data)
    except OrchestratorError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return BackgroundTask(_run_started_gate, state, gate_id, input_data)


def _gate_runs_in_background(state: WebAppState, gate: sqlite3.Row) -> bool:
    transition = state.config.workflow.transitions.get(str(gate["transition_name"]))
    return transition is not None and transition.action == "codex.create_draft_pr"


def _run_started_gate(state: WebAppState, gate_id: int, input_data: dict[str, Any]) -> None:
    try:
        Orchestrator(state.config, state.store).run_started_human_gate(gate_id, input_data=input_data)
    except Exception as exc:
        _record_background_gate_error(state, gate_id, exc)


def _record_background_gate_error(state: WebAppState, gate_id: int, exc: Exception) -> None:
    state.store.reopen_workflow_gate(gate_id)
    gate = state.store.workflow_gate_by_id(gate_id)
    if gate is None or gate["attempt_id"] is None:
        return
    state.store.record_error(
        int(gate["attempt_id"]),
        phase="workflow",
        error_type=type(exc).__name__,
        message=str(exc),
        recoverable=True,
    )


def _run_gate(request: Request, gate_id: int, input_data: dict[str, Any]) -> None:
    state = get_app_state(request)
    try:
        Orchestrator(state.config, state.store).run_human_gate(gate_id, input_data=input_data)
    except OrchestratorError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


def _gate_input_data(form: FormData) -> dict[str, Any]:
    return {key: str(value) for key, value in form.items() if key != "return_to"}


def _safe_return_to(form: FormData, gate: sqlite3.Row) -> str:
    requested = str(form.get("return_to") or "")
    if requested.startswith("/") and not requested.startswith("//"):
        return requested
    attempt_id = gate["attempt_id"]
    if attempt_id is not None:
        return f"/attempts/{int(attempt_id)}"
    return f"/issues/{str(gate['repo'])}/{int(gate['issue_number'])}"
