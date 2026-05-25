from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from starlette.responses import RedirectResponse, Response

from symphony_dbcli.web.dependencies import (
    page_context,
    source_repository,
    templates,
    work_item_repository,
)
from symphony_dbcli.work_items import WorkItemActivation, WorkItemError

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
def detail(request: Request, work_item_id: int) -> Response:
    work_item = work_item_repository(request).detail(work_item_id)
    if work_item is None:
        raise HTTPException(status_code=404, detail="Work item not found")
    context = page_context(request, title=f"Work Item #{work_item_id}", active="work_items")
    context["work_item"] = work_item
    return templates.TemplateResponse(
        request=request,
        name="work_items/detail.html",
        context=context,
    )


@router.get("/source-items/{source_item_id}/activate")
def activate_form(request: Request, source_item_id: int) -> Response:
    source_item = source_repository(request).get_source_item(source_item_id)
    if source_item is None:
        raise HTTPException(status_code=404, detail="Source item not found")
    context = page_context(request, title="Queue Work", active="board")
    context["source_item"] = source_item
    context["task_type"] = source_item.default_task_type
    context["user_hint"] = ""
    context["error"] = ""
    return templates.TemplateResponse(
        request=request,
        name="work_items/activate.html",
        context=context,
    )


@router.post("/source-items/{source_item_id}/activate")
def activate(
    request: Request,
    source_item_id: int,
    task_type: Annotated[str, Form()],
    user_hint: Annotated[str, Form()] = "",
) -> Response:
    try:
        work_item = work_item_repository(request).activate_source_item(
            WorkItemActivation(
                source_item_id=source_item_id,
                task_type=task_type,
                user_hint=user_hint,
            )
        )
    except WorkItemError as exc:
        source_item = source_repository(request).get_source_item(source_item_id)
        if source_item is None:
            raise HTTPException(status_code=404, detail="Source item not found") from exc
        context = page_context(request, title="Queue Work", active="board")
        context["source_item"] = source_item
        context["task_type"] = task_type
        context["user_hint"] = user_hint
        context["error"] = str(exc)
        return templates.TemplateResponse(
            request=request,
            name="work_items/activate.html",
            context=context,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(
        f"/board?source_id={work_item.source_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
