from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request, status
from starlette.datastructures import FormData
from starlette.responses import RedirectResponse, Response

from symphony_dbcli.orchestrator import Orchestrator, OrchestratorError
from symphony_dbcli.review_actions import DraftPullRequestContent, build_draft_pr_content
from symphony_dbcli.store import Store
from symphony_dbcli.web.dependencies import get_app_state, page_context, templates

router = APIRouter(tags=["attempts"])


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


@router.post("/attempts/{attempt_id}/draft-pr")
def create_draft_pr(
    request: Request,
    attempt_id: int,
    title: Annotated[str, Form()] = "",
    body: Annotated[str, Form()] = "",
) -> Response:
    state = get_app_state(request)
    gate = state.store.pending_workflow_gate_for_attempt(attempt_id, "create_draft_pr")
    if not gate:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No pending create_draft_pr workflow gate for this attempt.",
        )
    _run_gate(request, int(gate["id"]), {"title": title, "body": body})
    return RedirectResponse(
        f"/attempts/{attempt_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


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
    _run_gate(request, gate_id, _gate_input_data(form))
    return RedirectResponse(
        _safe_return_to(form, gate),
        status_code=status.HTTP_303_SEE_OTHER,
    )


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
    context["post_answer_gate"] = gate_transitions.get("post_answer")
    context["return_to"] = f"/attempts/{attempt_id}"
    context["draft_pr_content"] = _draft_pr_content(detail, state.store)
    return context


def _draft_pr_content(detail: dict[str, Any] | None, store: Store) -> DraftPullRequestContent | None:
    if not detail or detail["attempt"]["task_type"] != "code" or detail["pull_requests"]:
        return None
    result = detail["result"]
    if not result:
        return None
    issue = store.issue_detail(str(detail["attempt"]["repo"]), int(detail["attempt"]["issue_number"]))
    issue_title = str(issue["issue"]["title"]) if issue else ""
    return build_draft_pr_content(
        str(detail["attempt"]["repo"]),
        int(detail["attempt"]["issue_number"]),
        str(result["body"]),
        issue_title=issue_title,
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
