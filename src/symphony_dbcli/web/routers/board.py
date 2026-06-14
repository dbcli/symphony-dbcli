from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response

from symphony_dbcli.sources import (
    LOCAL_TICKET_KIND,
    SourceItemPage,
    SourceItemView,
    SourceRepository,
    SourceView,
)
from symphony_dbcli.web.dependencies import (
    page_context,
    source_repository,
    templates,
    work_item_repository,
)
from symphony_dbcli.work_items import (
    DONE_STATE,
    KANBAN_STATES,
    STATE_LABELS,
    WorkItemRepository,
    WorkItemView,
)

router = APIRouter(tags=["board"])
BACKLOG_STATE = "backlog"
BOARD_STATE_LABELS = {"backlog": "Backlog", **STATE_LABELS}
BoardKindFilter = Literal["all", "issue", "pull_request"]
SourceItemKind = Literal["issue", "pull_request", "local_ticket"]


@dataclass(frozen=True)
class BoardKindOption:
    label: str
    value: BoardKindFilter
    selected: bool


@dataclass(frozen=True)
class BoardFilters:
    source_id: int | None = None
    q: str = ""
    kind: BoardKindFilter = "all"
    backlog_page: int = 1
    done_page: int = 1

    @property
    def source_item_kinds(self) -> tuple[SourceItemKind, ...] | None:
        if self.kind == "all":
            return None
        if self.kind == "issue":
            return ("issue", LOCAL_TICKET_KIND)
        return (self.kind,)


@dataclass(frozen=True)
class BoardColumn:
    name: str
    label: str
    source_items: list[SourceItemView]
    work_items: list[WorkItemView]
    count: int
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
    q: str = "",
    kind: str = "all",
    backlog_page: int = 1,
    done_page: int = 1,
) -> Response:
    return _render_board(
        request,
        BoardFilters(
            source_id=source_id,
            q=q,
            kind=_board_kind_filter(kind),
            backlog_page=backlog_page,
            done_page=done_page,
        ),
    )


@router.get("/board/source/{source_id}")
def source_index(
    request: Request,
    source_id: int,
    q: str = "",
    kind: str = "all",
    backlog_page: int = 1,
    done_page: int = 1,
) -> Response:
    return _render_board(
        request,
        BoardFilters(
            source_id=source_id,
            q=q,
            kind=_board_kind_filter(kind),
            backlog_page=backlog_page,
            done_page=done_page,
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
    source_id = None if selected_source is None else selected_source.id
    context["board_query"] = filters.q
    context["board_kind"] = filters.kind
    context["board_kind_options"] = _board_kind_options(filters)
    context["board_form_action"] = _board_base_url(source_id)
    context["board_clear_url"] = _board_url(source_id, filters, clear_query=True, backlog_page=1, done_page=1)
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
            query=filters.q,
            kinds=filters.source_item_kinds,
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
    if state == DONE_STATE:
        page = (
            work_items.list_by_state_page(
                selected_source.id,
                state,
                query=filters.q,
                kinds=filters.source_item_kinds,
                page=filters.done_page,
            )
            if selected_source
            else None
        )
        if page is None:
            return BoardColumn(
                name=state,
                label=BOARD_STATE_LABELS[state],
                source_items=[],
                work_items=[],
                count=0,
            )
        return BoardColumn(
            name=state,
            label=BOARD_STATE_LABELS[state],
            source_items=[],
            work_items=page.items,
            count=page.total,
            page=page.page,
            page_start=page.start_index,
            page_end=page.end_index,
            previous_url=_board_url(source_id, filters, done_page=page.previous_page)
            if page.has_previous
            else "",
            next_url=_board_url(source_id, filters, done_page=page.next_page) if page.has_next else "",
        )
    query = filters.q
    items = (
        work_items.list_by_state(
            selected_source.id,
            state,
            query=query,
            kinds=filters.source_item_kinds,
        )
        if selected_source
        else []
    )
    return BoardColumn(
        name=state,
        label=BOARD_STATE_LABELS[state],
        source_items=[],
        work_items=items,
        count=len(items),
    )


def _board_url(
    source_id: int | None,
    filters: BoardFilters,
    *,
    backlog_page: int | None = None,
    done_page: int | None = None,
    clear_query: bool = False,
) -> str:
    params: dict[str, str] = {}
    if filters.q and not clear_query:
        params["q"] = filters.q
    if filters.kind != "all":
        params["kind"] = filters.kind
    page = filters.backlog_page if backlog_page is None else backlog_page
    if page > 1:
        params["backlog_page"] = str(page)
    done = filters.done_page if done_page is None else done_page
    if done > 1:
        params["done_page"] = str(done)
    base_url = _board_base_url(source_id)
    return f"{base_url}?{urlencode(params)}" if params else base_url


def _board_base_url(source_id: int | None) -> str:
    return f"/board/source/{source_id}" if source_id is not None else "/board"


def _board_kind_filter(value: str) -> BoardKindFilter:
    if value == "issue":
        return "issue"
    if value == "pull_request":
        return "pull_request"
    return "all"


def _board_kind_options(filters: BoardFilters) -> tuple[BoardKindOption, ...]:
    options: tuple[tuple[str, BoardKindFilter], ...] = (
        ("All", "all"),
        ("Issues", "issue"),
        ("PRs", "pull_request"),
    )
    return tuple(
        BoardKindOption(label=label, value=value, selected=value == filters.kind) for label, value in options
    )
