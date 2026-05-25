from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from symphony_dbcli.web.dependencies import page_context, templates, work_item_repository

router = APIRouter(tags=["operations"])


@router.get("/operations")
def index(request: Request) -> Response:
    context = page_context(request, title="Operations", active="operations")
    context["runs"] = work_item_repository(request).list_operations()
    return templates.TemplateResponse(
        request=request,
        name="operations/index.html",
        context=context,
    )
