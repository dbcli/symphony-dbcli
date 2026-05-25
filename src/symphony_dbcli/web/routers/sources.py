from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from symphony_dbcli.web.dependencies import page_context, templates

router = APIRouter(tags=["sources"])


@router.get("/sources")
def index(request: Request) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="sources/index.html",
        context=page_context(request, title="Sources", active="sources"),
    )
