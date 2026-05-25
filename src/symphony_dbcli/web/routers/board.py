from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response

from symphony_dbcli.sources import SourceItemView, SourceRepository, SourceView
from symphony_dbcli.web.dependencies import (
    page_context,
    source_repository,
    templates,
    work_item_repository,
)
from symphony_dbcli.work_items import KANBAN_STATES, STATE_LABELS, WorkItemRepository, WorkItemView

router = APIRouter(tags=["board"])
BACKLOG_STATE = "backlog"
BOARD_STATE_LABELS = {"backlog": "Backlog", **STATE_LABELS}


@dataclass(frozen=True)
class BoardColumn:
    name: str
    label: str
    source_items: list[SourceItemView]
    work_items: list[WorkItemView]
    count: int = 0


@router.get("/")
@router.get("/board")
def index(request: Request, source_id: int | None = None) -> Response:
    repo = source_repository(request)
    work_items = work_item_repository(request)
    sources = repo.list_sources()
    selected_source = _selected_source(repo, sources, source_id)
    context = page_context(request, title=_board_title(selected_source), active="board")
    context["sources"] = sources
    context["selected_source"] = selected_source
    context["columns"] = _board_columns(repo, work_items, selected_source)
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


def _board_columns(
    repo: SourceRepository,
    work_items: WorkItemRepository,
    selected_source: SourceView | None,
) -> list[BoardColumn]:
    backlog_items = repo.backlog_source_items(selected_source.id) if selected_source else []
    return [
        BoardColumn(
            name=BACKLOG_STATE,
            label=BOARD_STATE_LABELS[BACKLOG_STATE],
            source_items=backlog_items,
            work_items=[],
            count=len(backlog_items),
        ),
        *[_work_item_column(state, work_items, selected_source) for state in KANBAN_STATES],
    ]


def _work_item_column(
    state: str,
    work_items: WorkItemRepository,
    selected_source: SourceView | None,
) -> BoardColumn:
    items = work_items.list_by_state(selected_source.id, state) if selected_source else []
    return BoardColumn(
        name=state,
        label=BOARD_STATE_LABELS[state],
        source_items=[],
        work_items=items,
        count=len(items),
    )
