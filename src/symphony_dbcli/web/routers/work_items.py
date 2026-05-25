from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from symphony_dbcli.web.dependencies import page_context, templates

router = APIRouter(prefix="/work-items", tags=["work items"])


@router.get("")
def index(request: Request) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="work_items/index.html",
        context=page_context(request, title="Work Items", active="work_items"),
    )


@router.get("/{work_item_id}")
def detail(request: Request, work_item_id: int) -> Response:
    context = page_context(request, title=f"Work Item #{work_item_id}", active="work_items")
    context["work_item_id"] = work_item_id
    return templates.TemplateResponse(
        request=request,
        name="work_items/detail.html",
        context=context,
    )
