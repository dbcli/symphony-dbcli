from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response

from symphony_dbcli.sources import SourceItemPage, SourceItemView, SourceRepository, SourceView
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
SEARCH_PARAM_BY_STATE = {
    BACKLOG_STATE: "backlog_q",
    "todo": "todo_q",
    "in_progress": "in_progress_q",
    "in_review": "in_review_q",
    "done": "done_q",
}


@dataclass(frozen=True)
class BoardFilters:
    source_id: int | None = None
    backlog_q: str = ""
    todo_q: str = ""
    in_progress_q: str = ""
    in_review_q: str = ""
    done_q: str = ""
    backlog_page: int = 1

    def query_for(self, state: str) -> str:
        return self.query_values.get(SEARCH_PARAM_BY_STATE[state], "").strip()

    @property
    def query_values(self) -> dict[str, str]:
        return {
            "backlog_q": self.backlog_q.strip(),
            "todo_q": self.todo_q.strip(),
            "in_progress_q": self.in_progress_q.strip(),
            "in_review_q": self.in_review_q.strip(),
            "done_q": self.done_q.strip(),
        }


@dataclass(frozen=True)
class BoardColumn:
    name: str
    label: str
    source_items: list[SourceItemView]
    work_items: list[WorkItemView]
    count: int
    form_action: str
    query_param: str
    query: str
    hidden_params: tuple[tuple[str, str], ...]
    clear_url: str
    page: int = 1
    page_start: int = 0
    page_end: int = 0
    previous_url: str = ""
    next_url: str = ""


@router.get("/")
@router.get("/board")
def index(
    request: Request,
    source_id: int | None = None,
    backlog_q: str = "",
    todo_q: str = "",
    in_progress_q: str = "",
    in_review_q: str = "",
    done_q: str = "",
    backlog_page: int = 1,
) -> Response:
    return _render_board(
        request,
        BoardFilters(
            source_id=source_id,
            backlog_q=backlog_q,
            todo_q=todo_q,
            in_progress_q=in_progress_q,
            in_review_q=in_review_q,
            done_q=done_q,
            backlog_page=backlog_page,
        ),
    )


@router.get("/board/source/{source_id}")
def source_index(
    request: Request,
    source_id: int,
    backlog_q: str = "",
    todo_q: str = "",
    in_progress_q: str = "",
    in_review_q: str = "",
    done_q: str = "",
    backlog_page: int = 1,
) -> Response:
    return _render_board(
        request,
        BoardFilters(
            source_id=source_id,
            backlog_q=backlog_q,
            todo_q=todo_q,
            in_progress_q=in_progress_q,
            in_review_q=in_review_q,
            done_q=done_q,
            backlog_page=backlog_page,
        ),
    )


def _render_board(request: Request, filters: BoardFilters) -> Response:
    repo = source_repository(request)
    work_items = work_item_repository(request)
    sources = repo.list_sources()
    selected_source = _selected_source(repo, sources, filters.source_id)
    context = page_context(request, title=_board_title(selected_source), active="board")
    context["sources"] = sources
    context["selected_source"] = selected_source
    context["columns"] = _board_columns(repo, work_items, selected_source, filters)
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
    filters: BoardFilters,
) -> list[BoardColumn]:
    backlog_page = (
        repo.backlog_source_item_page(
            selected_source.id,
            query=filters.query_for(BACKLOG_STATE),
            page=filters.backlog_page,
        )
        if selected_source
        else SourceItemPage(items=[], total=0, page=1, limit=20, query="")
    )
    return [
        _backlog_column(backlog_page, selected_source, filters),
        *[_work_item_column(state, work_items, selected_source, filters) for state in KANBAN_STATES],
    ]


def _backlog_column(
    page: SourceItemPage,
    selected_source: SourceView | None,
    filters: BoardFilters,
) -> BoardColumn:
    source_id = None if selected_source is None else selected_source.id
    return BoardColumn(
        name=BACKLOG_STATE,
        label=BOARD_STATE_LABELS[BACKLOG_STATE],
        source_items=page.items,
        work_items=[],
        count=page.total,
        form_action=_board_base_url(source_id),
        query_param=SEARCH_PARAM_BY_STATE[BACKLOG_STATE],
        query=page.query,
        hidden_params=_hidden_params(BACKLOG_STATE, filters),
        clear_url=_board_url(source_id, filters, clear_state=BACKLOG_STATE, backlog_page=1),
        page=page.page,
        page_start=page.start_index,
        page_end=page.end_index,
        previous_url=_board_url(source_id, filters, backlog_page=page.previous_page)
        if page.has_previous
        else "",
        next_url=_board_url(source_id, filters, backlog_page=page.next_page) if page.has_next else "",
    )


def _work_item_column(
    state: str,
    work_items: WorkItemRepository,
    selected_source: SourceView | None,
    filters: BoardFilters,
) -> BoardColumn:
    source_id = None if selected_source is None else selected_source.id
    query = filters.query_for(state)
    items = work_items.list_by_state(selected_source.id, state, query=query) if selected_source else []
    return BoardColumn(
        name=state,
        label=BOARD_STATE_LABELS[state],
        source_items=[],
        work_items=items,
        count=len(items),
        form_action=_board_base_url(source_id),
        query_param=SEARCH_PARAM_BY_STATE[state],
        query=query,
        hidden_params=_hidden_params(state, filters),
        clear_url=_board_url(source_id, filters, clear_state=state),
    )


def _hidden_params(
    state: str,
    filters: BoardFilters,
) -> tuple[tuple[str, str], ...]:
    query_param = SEARCH_PARAM_BY_STATE[state]
    params: list[tuple[str, str]] = []
    params.extend(
        (name, value) for name, value in filters.query_values.items() if name != query_param and value
    )
    if state != BACKLOG_STATE and filters.backlog_page > 1:
        params.append(("backlog_page", str(filters.backlog_page)))
    return tuple(params)


def _board_url(
    source_id: int | None,
    filters: BoardFilters,
    *,
    backlog_page: int | None = None,
    clear_state: str | None = None,
) -> str:
    params: dict[str, str] = {}
    clear_query_param = SEARCH_PARAM_BY_STATE[clear_state] if clear_state else ""
    params.update(
        {name: value for name, value in filters.query_values.items() if value and name != clear_query_param}
    )
    page = filters.backlog_page if backlog_page is None else backlog_page
    if page > 1:
        params["backlog_page"] = str(page)
    base_url = _board_base_url(source_id)
    return f"{base_url}?{urlencode(params)}" if params else base_url


def _board_base_url(source_id: int | None) -> str:
    return f"/board/source/{source_id}" if source_id is not None else "/board"
