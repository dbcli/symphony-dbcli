from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request, status
from starlette.background import BackgroundTask
from starlette.responses import RedirectResponse, Response

from symphony_dbcli.web.dependencies import (
    get_app_state,
    page_context,
    source_repository,
    templates,
    work_item_repository,
)
from symphony_dbcli.work_items import (
    KANBAN_STATES,
    REVIEW_RERUN_REASONS,
    STATE_LABELS,
    WorkItemActivation,
    WorkItemError,
    WorkItemMove,
)

router = APIRouter(tags=["work items"])


@router.get("/work-items")
def index(request: Request) -> Response:
    context = page_context(request, title="Work Items", active="work_items")
    context["work_items"] = work_item_repository(request).list_all()
    return templates.TemplateResponse(
        request=request,
        name="work_items/index.html",
        context=context,
    )


@router.get("/work-items/{work_item_id}")
def detail(request: Request, work_item_id: int, target_state: str | None = None) -> Response:
    context = _detail_context(
        request,
        work_item_id,
        error="",
        selected_target_state=target_state,
        return_to="",
        is_modal=False,
    )
    return templates.TemplateResponse(
        request=request,
        name="work_items/detail.html",
        context=context,
    )


@router.get("/work-items/{work_item_id}/move-form")
def move_form(
    request: Request,
    work_item_id: int,
    target_state: str | None = None,
    return_to: str = "",
) -> Response:
    context = _detail_context(
        request,
        work_item_id,
        error="",
        selected_target_state=target_state,
        return_to=_safe_return_to(return_to),
        is_modal=True,
    )
    return templates.TemplateResponse(
        request=request,
        name="work_items/_move_modal.html",
        context=context,
    )


@router.post("/work-items/{work_item_id}/move")
def move(
    request: Request,
    work_item_id: int,
    target_state: Annotated[str, Form()],
    reasons: Annotated[list[str] | None, Form()] = None,
    note: Annotated[str, Form()] = "",
    return_to: Annotated[str, Form()] = "",
) -> Response:
    safe_return_to = _safe_return_to(return_to)
    try:
        work_item_repository(request).move_work_item(
            WorkItemMove(
                work_item_id=work_item_id,
                target_state=target_state,
                reasons=reasons or [],
                note=note,
            )
        )
    except WorkItemError as exc:
        context = _detail_context(
            request,
            work_item_id,
            error=str(exc),
            selected_target_state=target_state,
            return_to=safe_return_to,
            is_modal=_is_htmx(request),
        )
        template_name = "work_items/_move_modal.html" if _is_htmx(request) else "work_items/detail.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=context,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if _is_htmx(request) and safe_return_to:
        response: Response = Response(status_code=204, headers={"HX-Redirect": safe_return_to})
    else:
        response = RedirectResponse(
            safe_return_to or f"/work-items/{work_item_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return _schedule_cycle_after_in_progress_move(request, target_state, response)


@router.post("/work-items/{work_item_id}/active-pr")
def select_active_pr(
    request: Request,
    work_item_id: int,
    source_item_id: Annotated[int, Form()],
) -> Response:
    try:
        work_item_repository(request).select_active_pr(work_item_id, source_item_id)
    except WorkItemError as exc:
        context = _detail_context(request, work_item_id, error=str(exc))
        return templates.TemplateResponse(
            request=request,
            name="work_items/detail.html",
            context=context,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(
        f"/work-items/{work_item_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/work-items/{work_item_id}/archive")
def archive_work_item(
    request: Request,
    work_item_id: int,
    note: Annotated[str, Form()] = "",
) -> Response:
    try:
        work_item = work_item_repository(request).archive_work_item(work_item_id, note)
    except WorkItemError as exc:
        context = _detail_context(request, work_item_id, error=str(exc))
        return templates.TemplateResponse(
            request=request,
            name="work_items/detail.html",
            context=context,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(
        f"/board/source/{work_item.source_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/source-items/{source_item_id}/activate")
def activate_form(request: Request, source_item_id: int) -> Response:
    context = _activation_context(
        request,
        source_item_id,
        task_type=None,
        user_hint="",
        error="",
        return_to="",
        is_modal=False,
    )
    return templates.TemplateResponse(
        request=request,
        name="work_items/activate.html",
        context=context,
    )


@router.get("/source-items/{source_item_id}/activate-form")
def activate_modal_form(request: Request, source_item_id: int, return_to: str = "") -> Response:
    context = _activation_context(
        request,
        source_item_id,
        task_type=None,
        user_hint="",
        error="",
        return_to=_safe_return_to(return_to),
        is_modal=True,
    )
    return templates.TemplateResponse(
        request=request,
        name="work_items/_activate_modal.html",
        context=context,
    )


@router.post("/source-items/{source_item_id}/activate")
def activate(
    request: Request,
    source_item_id: int,
    task_type: Annotated[str, Form()],
    user_hint: Annotated[str, Form()] = "",
    return_to: Annotated[str, Form()] = "",
) -> Response:
    safe_return_to = _safe_return_to(return_to)
    try:
        work_item = work_item_repository(request).activate_source_item(
            WorkItemActivation(
                source_item_id=source_item_id,
                task_type=task_type,
                user_hint=user_hint,
            )
        )
    except WorkItemError as exc:
        context = _activation_context(
            request,
            source_item_id,
            task_type=task_type,
            user_hint=user_hint,
            error=str(exc),
            return_to=safe_return_to,
            is_modal=_is_htmx(request),
        )
        template_name = "work_items/_activate_modal.html" if _is_htmx(request) else "work_items/activate.html"
        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context=context,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if _is_htmx(request) and safe_return_to:
        return Response(status_code=204, headers={"HX-Redirect": safe_return_to})
    return RedirectResponse(
        safe_return_to or f"/board/source/{work_item.source_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/source-items/{source_item_id}/ignore")
def ignore_source_item(
    request: Request,
    source_item_id: int,
    note: Annotated[str, Form()] = "",
) -> Response:
    source_item = source_repository(request).ignore_source_item(source_item_id, note)
    return RedirectResponse(
        f"/board/source/{source_item.source_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _detail_context(
    request: Request,
    work_item_id: int,
    error: str,
    selected_target_state: str | None = None,
    return_to: str = "",
    is_modal: bool = False,
) -> dict[str, object]:
    work_item = work_item_repository(request).detail(work_item_id)
    if work_item is None:
        raise HTTPException(status_code=404, detail="Work item not found")
    context = page_context(request, title=f"Work Item #{work_item_id}", active="work_items")
    context["work_item"] = work_item
    context["runs"] = work_item_repository(request).list_runs(work_item_id)
    linked_source_items = work_item_repository(request).linked_source_items(work_item_id)
    context["linked_source_items"] = linked_source_items
    context["linked_pull_requests"] = [item for item in linked_source_items if item.kind == "pull_request"]
    context["states"] = [(state, STATE_LABELS[state]) for state in KANBAN_STATES]
    context["selected_target_state"] = (
        selected_target_state if selected_target_state in KANBAN_STATES else work_item.state
    )
    context["show_rerun_prompt"] = work_item.state == "in_review" and selected_target_state == "in_progress"
    context["review_reasons"] = REVIEW_RERUN_REASONS.items()
    context["return_to"] = return_to
    context["is_modal"] = is_modal
    context["error"] = error
    return context


def _activation_context(
    request: Request,
    source_item_id: int,
    *,
    task_type: str | None,
    user_hint: str,
    error: str,
    return_to: str,
    is_modal: bool,
) -> dict[str, object]:
    source_item = source_repository(request).get_source_item(source_item_id)
    if source_item is None:
        raise HTTPException(status_code=404, detail="Source item not found")
    context = page_context(request, title="Queue Work", active="board")
    context["source_item"] = source_item
    context["task_type"] = task_type or source_item.default_task_type
    context["user_hint"] = user_hint
    context["return_to"] = return_to
    context["is_modal"] = is_modal
    context["error"] = error
    return context


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _safe_return_to(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return ""
    return value


def _schedule_cycle_after_in_progress_move(
    request: Request, target_state: str, response: Response
) -> Response:
    if target_state != "in_progress":
        return response
    runtime = get_app_state(request).runtime
    if runtime is None:
        return response
    response.background = BackgroundTask(runtime.run_cycle, trigger="board_move")
    return response
