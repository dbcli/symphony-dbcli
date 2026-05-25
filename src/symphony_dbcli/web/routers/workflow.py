from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from symphony_dbcli.web.dependencies import page_context, templates

router = APIRouter(prefix="/workflow", tags=["workflow"])


@router.get("")
def index(request: Request) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="workflow/index.html",
        context=page_context(request, title="Workflow", active="workflow"),
    )


@router.get("/edit")
def edit(request: Request) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="workflow/edit.html",
        context=page_context(request, title="Workflow Editor", active="workflow"),
    )
