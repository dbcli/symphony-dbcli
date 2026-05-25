from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response

from symphony_dbcli.sources import SourceRepository, SourceView
from symphony_dbcli.web.dependencies import page_context, source_repository, templates

router = APIRouter(tags=["board"])
COLUMN_NAMES = ("backlog", "todo", "in progress", "in review", "done")


@dataclass(frozen=True)
class BoardColumn:
    name: str
    count: int = 0


@router.get("/")
@router.get("/board")
def index(request: Request, source_id: int | None = None) -> Response:
    repo = source_repository(request)
    sources = repo.list_sources()
    selected_source = _selected_source(repo, sources, source_id)
    context = page_context(request, title=_board_title(selected_source), active="board")
    context["sources"] = sources
    context["selected_source"] = selected_source
    context["columns"] = [BoardColumn(name=name) for name in COLUMN_NAMES]
    return templates.TemplateResponse(
        request=request,
        name="board/index.html",
        context=context,
    )


def _selected_source(
    repo: SourceRepository,
    sources: list[SourceView],
    source_id: int | None,
) -> SourceView | None:
    if source_id is None:
        return sources[0] if sources else None
    source = repo.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


def _board_title(source: SourceView | None) -> str:
    return f"Board · {source.repo}" if source else "Board"
