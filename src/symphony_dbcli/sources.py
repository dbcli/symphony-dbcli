from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any, Literal, Protocol, cast

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .clock import utc_now
from .db import SessionFactory
from .github import GitHubIssue, PullRequest
from .models import (
    ChatMessage,
    ChatThread,
    Source,
    SourceItem,
    SourceItemLink,
    SourceSyncRun,
    WorkItem,
    WorkItemLink,
    WorkItemRun,
    WorkItemStateEvent,
)
from .review_actions import (
    body_links_issue,
    body_links_source_item,
    issue_link_marker,
    source_item_link_marker,
)
from .search import delete_source_item_search, matching_source_item_ids, rebuild_source_item_search

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SOURCE_ITEM_PAGE_SIZE = 20
LOCAL_TICKET_KIND: Literal["local_ticket"] = "local_ticket"
CONVERSATION_KIND: Literal["conversation"] = "conversation"
SourceItemKind = Literal["issue", "pull_request", "local_ticket", "conversation"]


class SourceValidationError(ValueError):
    """Raised when a source cannot be created from user input."""


class SourceSyncClient(Protocol):
    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]: ...

    def list_pull_requests(self, repo: str, *, state: str = "open") -> list[PullRequest]: ...


@dataclass(frozen=True)
class SourceCreate:
    repo: str


@dataclass(frozen=True)
class SourceFilters:
    labels: tuple[str, ...] = ()
    authors: tuple[str, ...] = ()
    updated_after: str = ""
    updated_before: str = ""
    stale_after_days: int | None = None

    @classmethod
    def from_json(cls, value: str) -> SourceFilters:
        data = cast(dict[str, Any], json.loads(value or "{}"))
        return cls(
            labels=tuple(cast(list[str], data.get("labels", []))),
            authors=tuple(cast(list[str], data.get("authors", []))),
            updated_after=str(data.get("updated_after") or ""),
            updated_before=str(data.get("updated_before") or ""),
            stale_after_days=cast(int | None, data.get("stale_after_days")),
        )

    @property
    def has_filters(self) -> bool:
        return bool(
            self.labels
            or self.authors
            or self.updated_after
            or self.updated_before
            or self.stale_after_days is not None
        )

    @property
    def labels_text(self) -> str:
        return ", ".join(self.labels)

    @property
    def authors_text(self) -> str:
        return ", ".join(self.authors)

    def to_json(self) -> str:
        return json.dumps(
            {
                "labels": list(self.labels),
                "authors": list(self.authors),
                "updated_after": self.updated_after,
                "updated_before": self.updated_before,
                "stale_after_days": self.stale_after_days,
            },
            sort_keys=True,
        )


@dataclass(frozen=True)
class SourceUpdate:
    display_name: str
    enabled: bool
    filters: SourceFilters


@dataclass(frozen=True)
class SourceItemUpsert:
    kind: str
    number: int
    title: str
    url: str
    state: str
    author: str
    labels: list[str]
    body: str
    github_updated_at: str


@dataclass(frozen=True)
class LocalTicketCreate:
    source_id: int
    title: str
    body: str = ""


@dataclass(frozen=True)
class SourceItemView:
    id: int
    source_id: int
    kind: str
    number: int
    title: str
    url: str
    state: str
    author: str
    labels: list[str]
    github_updated_at: str
    disposition: str
    disposition_note: str
    synced_at: str
    linked_items: tuple[SourceItemView, ...] = ()

    @classmethod
    def from_model(cls, item: SourceItem) -> SourceItemView:
        return cls(
            id=item.id,
            source_id=item.source_id,
            kind=item.kind,
            number=item.number,
            title=item.title,
            url=item.url,
            state=item.state,
            author=item.author,
            labels=cast(list[str], json.loads(item.labels_json)),
            github_updated_at=item.github_updated_at,
            disposition=item.disposition,
            disposition_note=item.disposition_note,
            synced_at=item.synced_at,
        )

    @property
    def kind_label(self) -> str:
        if self.kind == "pull_request":
            return "PR"
        if self.kind == LOCAL_TICKET_KIND:
            return "Ticket"
        if self.kind == CONVERSATION_KIND:
            return "Chat"
        return "Issue"

    @property
    def default_task_type(self) -> str:
        return "code" if self.kind == "pull_request" or self.linked_items else "research"

    @property
    def activation_label(self) -> str:
        return "Review/fix" if self.default_task_type == "code" else "Research"

    @property
    def has_linked_items(self) -> bool:
        return bool(self.linked_items)


@dataclass(frozen=True)
class SourceItemPage:
    items: list[SourceItemView]
    total: int
    page: int
    limit: int
    query: str

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page * self.limit < self.total

    @property
    def previous_page(self) -> int:
        return max(1, self.page - 1)

    @property
    def next_page(self) -> int:
        return self.page + 1

    @property
    def start_index(self) -> int:
        if self.total == 0:
            return 0
        return ((self.page - 1) * self.limit) + 1

    @property
    def end_index(self) -> int:
        return min(self.page * self.limit, self.total)


@dataclass(frozen=True)
class SourceSyncSummary:
    source_id: int
    run_id: int
    issue_count: int
    pull_request_count: int


@dataclass(frozen=True)
class SourceView:
    id: int
    kind: str
    repo: str
    display_name: str
    filters: SourceFilters
    enabled: bool
    sync_status: str
    last_synced_at: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_model(cls, source: Source) -> SourceView:
        return cls(
            id=source.id,
            kind=source.kind,
            repo=source.repo,
            display_name=source.display_name,
            filters=SourceFilters.from_json(source.filters_json),
            enabled=source.enabled,
            sync_status=source.sync_status,
            last_synced_at=source.last_synced_at,
            created_at=source.created_at,
            updated_at=source.updated_at,
        )

    @property
    def filter_summary(self) -> str:
        if not self.filters.has_filters:
            return "All open issues and PRs"
        parts: list[str] = []
        if self.filters.labels:
            parts.append(f"labels: {', '.join(self.filters.labels)}")
        if self.filters.authors:
            parts.append(f"authors: {', '.join(self.filters.authors)}")
        if self.filters.updated_after:
            parts.append(f"updated after: {self.filters.updated_after}")
        if self.filters.updated_before:
            parts.append(f"updated before: {self.filters.updated_before}")
        if self.filters.stale_after_days is not None:
            parts.append(f"stale after: {self.filters.stale_after_days} days")
        return "; ".join(parts)


class SourceRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def list_sources(self) -> list[SourceView]:
        with self._session_factory() as session:
            rows = session.scalars(select(Source).order_by(Source.repo.asc())).all()
            return [SourceView.from_model(row) for row in rows]

    def get_source(self, source_id: int) -> SourceView | None:
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            return SourceView.from_model(source) if source else None

    def create_source(self, source: SourceCreate) -> SourceView:
        repo = normalize_repo(source.repo)
        now = utc_now()
        model = Source(
            kind="github_repo",
            repo=repo,
            display_name=repo,
            filters_json="{}",
            enabled=True,
            sync_status="never",
            created_at=now,
            updated_at=now,
        )
        with self._session_factory() as session:
            session.add(model)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.scalar(select(Source).where(Source.repo == repo))
                if existing is None:
                    raise
                return SourceView.from_model(existing)
            session.refresh(model)
            return SourceView.from_model(model)

    def update_source(self, source_id: int, update: SourceUpdate) -> SourceView:
        display_name = update.display_name.strip()
        if not display_name:
            raise SourceValidationError("Display name is required.")
        now = utc_now()
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise SourceValidationError("Source not found.")
            source.display_name = display_name
            source.enabled = update.enabled
            source.filters_json = update.filters.to_json()
            source.updated_at = now
            session.commit()
            session.refresh(source)
            return SourceView.from_model(source)

    def delete_source(self, source_id: int) -> SourceView | None:
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                return None
            deleted = SourceView.from_model(source)
            source_item_ids = list(
                session.scalars(select(SourceItem.id).where(SourceItem.source_id == source_id))
            )
            work_item_ids = list(session.scalars(select(WorkItem.id).where(WorkItem.source_id == source_id)))
            _delete_source_dependents(session, source_id, source_item_ids, work_item_ids)
            delete_source_item_search(session, source_id)
            session.execute(delete(Source).where(Source.id == source_id))
            session.commit()
            return deleted

    def open_source_items(self, source_id: int) -> list[SourceItemView]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(SourceItem)
                .where(
                    SourceItem.source_id == source_id,
                    SourceItem.state == "open",
                    SourceItem.disposition == "active",
                )
                .order_by(SourceItem.kind.asc(), SourceItem.number.desc())
            ).all()
            return [SourceItemView.from_model(row) for row in rows]

    def backlog_source_items(self, source_id: int) -> list[SourceItemView]:
        return self.backlog_source_item_page(source_id).items

    def backlog_source_item_page(
        self,
        source_id: int,
        *,
        query: str = "",
        kinds: tuple[SourceItemKind, ...] | None = None,
        page: int = 1,
        limit: int = SOURCE_ITEM_PAGE_SIZE,
    ) -> SourceItemPage:
        page_number = _positive_page(page)
        page_limit = _positive_page_limit(limit)
        linked_work_source_items = select(WorkItemLink.source_item_id)
        with self._session_factory() as session:
            visible_match_ids = _visible_match_ids(session, source_id, query)
            if query.strip() and not visible_match_ids:
                return SourceItemPage(items=[], total=0, page=1, limit=page_limit, query=query)
            conditions = [
                SourceItem.source_id == source_id,
                SourceItem.state == "open",
                SourceItem.disposition == "active",
                ~SourceItem.id.in_(linked_work_source_items),
            ]
            if visible_match_ids:
                conditions.append(SourceItem.id.in_(visible_match_ids))
            if kinds is not None:
                conditions.append(SourceItem.kind.in_(kinds))
            rows = list(
                session.scalars(
                    select(SourceItem)
                    .where(*conditions)
                    .order_by(
                        SourceItem.github_updated_at.desc(),
                        SourceItem.updated_at.desc(),
                        SourceItem.id.desc(),
                    )
                )
            )
            linked_prs_by_issue_id = _linked_prs_by_issue_id(session, source_id, rows)
            linked_pr_ids = {item.id for linked in linked_prs_by_issue_id.values() for item in linked}
            visible_items = [
                _source_item_view_with_links(row, linked_prs_by_issue_id.get(row.id, []))
                for row in rows
                if row.id not in linked_pr_ids
            ]
            total = len(visible_items)
            page_number = _clamped_page(page_number, total, page_limit)
            start = (page_number - 1) * page_limit
            return SourceItemPage(
                items=visible_items[start : start + page_limit],
                total=total,
                page=page_number,
                limit=page_limit,
                query=query,
            )

    def linked_source_items(self, source_item_id: int) -> list[SourceItemView]:
        with self._session_factory() as session:
            links = session.execute(
                select(SourceItem)
                .join(SourceItemLink, SourceItemLink.linked_source_item_id == SourceItem.id)
                .where(SourceItemLink.source_item_id == source_item_id)
                .order_by(SourceItem.number.asc())
            ).scalars()
            return [SourceItemView.from_model(item) for item in links]

    def get_source_item(self, source_item_id: int) -> SourceItemView | None:
        with self._session_factory() as session:
            source_item = session.get(SourceItem, source_item_id)
            if source_item is None:
                return None
            return _source_item_view_with_links(
                source_item,
                _linked_prs_for_issue(session, source_item),
            )

    def ignore_source_item(self, source_item_id: int, note: str = "") -> SourceItemView:
        now = utc_now()
        with self._session_factory() as session:
            source_item = session.get(SourceItem, source_item_id)
            if source_item is None:
                raise SourceValidationError("Source item not found.")
            source_item.disposition = "ignored"
            source_item.disposition_note = note.strip()
            source_item.disposition_at = now
            source_item.updated_at = now
            session.commit()
            session.refresh(source_item)
            return SourceItemView.from_model(source_item)

    def create_local_ticket(self, ticket: LocalTicketCreate) -> SourceItemView:
        title = ticket.title.strip()
        if not title:
            raise SourceValidationError("Title is required.")
        body = ticket.body.strip()
        now = utc_now()
        with self._session_factory() as session:
            source = session.get(Source, ticket.source_id)
            if source is None:
                raise SourceValidationError("Source not found.")
            next_number = _next_local_ticket_number(session, ticket.source_id)
            source_item = SourceItem(
                source_id=ticket.source_id,
                kind=LOCAL_TICKET_KIND,
                number=next_number,
                title=title,
                url="",
                state="open",
                author="local",
                labels_json="[]",
                body=body,
                github_updated_at=now,
                synced_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(source_item)
            session.flush()
            rebuild_source_item_search(session, ticket.source_id)
            session.commit()
            session.refresh(source_item)
            return SourceItemView.from_model(source_item)

    def start_sync_run(self, source_id: int) -> int:
        now = utc_now()
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise SourceValidationError("Source not found.")
            source.sync_status = "syncing"
            source.updated_at = now
            run = SourceSyncRun(
                source_id=source_id,
                status="running",
                issue_count=0,
                pull_request_count=0,
                error="",
                started_at=now,
                completed_at=None,
            )
            session.add(run)
            session.commit()
            return run.id

    def finish_sync_run(
        self,
        *,
        source_id: int,
        run_id: int,
        issue_count: int,
        pull_request_count: int,
    ) -> None:
        now = utc_now()
        with self._session_factory() as session:
            run = session.get(SourceSyncRun, run_id)
            source = session.get(Source, source_id)
            if run is None or source is None:
                raise SourceValidationError("Source sync run not found.")
            run.status = "succeeded"
            run.issue_count = issue_count
            run.pull_request_count = pull_request_count
            run.completed_at = now
            source.sync_status = "synced"
            source.last_synced_at = now
            source.updated_at = now
            session.commit()

    def fail_sync_run(self, *, source_id: int, run_id: int, error: str) -> None:
        now = utc_now()
        with self._session_factory() as session:
            run = session.get(SourceSyncRun, run_id)
            source = session.get(Source, source_id)
            if run:
                run.status = "failed"
                run.error = error
                run.completed_at = now
            if source:
                source.sync_status = "failed"
                source.updated_at = now
            session.commit()

    def upsert_source_items(
        self,
        *,
        source_id: int,
        items: list[SourceItemUpsert],
    ) -> None:
        now = utc_now()
        active_by_kind = _active_numbers_by_kind(items)
        with self._session_factory() as session:
            for item in items:
                existing = session.scalar(
                    select(SourceItem).where(
                        SourceItem.source_id == source_id,
                        SourceItem.kind == item.kind,
                        SourceItem.number == item.number,
                    )
                )
                if existing:
                    existing.title = item.title
                    existing.url = item.url
                    existing.state = item.state
                    existing.author = item.author
                    existing.labels_json = json.dumps(item.labels, sort_keys=True)
                    existing.body = item.body
                    existing.github_updated_at = item.github_updated_at
                    existing.synced_at = now
                    existing.updated_at = now
                else:
                    session.add(
                        SourceItem(
                            source_id=source_id,
                            kind=item.kind,
                            number=item.number,
                            title=item.title,
                            url=item.url,
                            state=item.state,
                            author=item.author,
                            labels_json=json.dumps(item.labels, sort_keys=True),
                            body=item.body,
                            github_updated_at=item.github_updated_at,
                            synced_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                    )
            session.flush()
            for kind, active_numbers in active_by_kind.items():
                stale_items = session.scalars(
                    select(SourceItem).where(SourceItem.source_id == source_id, SourceItem.kind == kind)
                ).all()
                for stale_item in stale_items:
                    if stale_item.number not in active_numbers:
                        stale_item.state = "closed"
                        stale_item.updated_at = now
            _record_issue_pr_links(session, source_id, now)
            _auto_complete_work_items(session, source_id, now)
            rebuild_source_item_search(session, source_id)
            session.commit()


class SourceSyncService:
    def __init__(self, repository: SourceRepository, client: SourceSyncClient):
        self._repository = repository
        self._client = client

    def sync_source(self, source_id: int) -> SourceSyncSummary:
        source = self._repository.get_source(source_id)
        if source is None:
            raise SourceValidationError("Source not found.")
        if not source.enabled:
            raise SourceValidationError("Source is disabled.")
        run_id = self._repository.start_sync_run(source_id)
        try:
            issue_labels = list(source.filters.labels) if source.filters.labels else None
            issues = self._client.list_issues(source.repo, labels=issue_labels)
            pull_requests = self._client.list_pull_requests(source.repo, state="open")
            items = _filtered_items(
                [
                    *[_item_from_issue(issue) for issue in issues],
                    *[_item_from_pull_request(pull_request) for pull_request in pull_requests],
                ],
                source.filters,
            )
            issue_count = sum(1 for item in items if item.kind == "issue")
            pull_request_count = sum(1 for item in items if item.kind == "pull_request")
            self._repository.upsert_source_items(
                source_id=source_id,
                items=items,
            )
            self._repository.finish_sync_run(
                source_id=source_id,
                run_id=run_id,
                issue_count=issue_count,
                pull_request_count=pull_request_count,
            )
            return SourceSyncSummary(
                source_id=source_id,
                run_id=run_id,
                issue_count=issue_count,
                pull_request_count=pull_request_count,
            )
        except Exception as exc:
            self._repository.fail_sync_run(source_id=source_id, run_id=run_id, error=str(exc))
            raise


def source_filters_from_form(
    *,
    labels: str,
    authors: str,
    updated_after: str,
    updated_before: str,
    stale_after_days: str,
) -> SourceFilters:
    date_floor = _date_value(updated_after, "Updated after")
    date_ceiling = _date_value(updated_before, "Updated before")
    if date_floor and date_ceiling and date_floor > date_ceiling:
        raise SourceValidationError("Updated after must be on or before updated before.")
    return SourceFilters(
        labels=tuple(_tokens(labels)),
        authors=tuple(_tokens(authors)),
        updated_after=date_floor,
        updated_before=date_ceiling,
        stale_after_days=_positive_int(stale_after_days, "Stale after days"),
    )


def normalize_repo(repo: str) -> str:
    normalized = repo.strip()
    if not REPO_RE.match(normalized):
        raise SourceValidationError("Use a GitHub repository in owner/name format.")
    return normalized


def _item_from_issue(issue: GitHubIssue) -> SourceItemUpsert:
    return SourceItemUpsert(
        kind="issue",
        number=issue.number,
        title=issue.title,
        url=issue.url,
        state=issue.state,
        author=issue.author,
        labels=issue.labels,
        body=issue.body,
        github_updated_at=issue.updated_at,
    )


def _item_from_pull_request(pull_request: PullRequest) -> SourceItemUpsert:
    return SourceItemUpsert(
        kind="pull_request",
        number=pull_request.number,
        title=pull_request.title,
        url=pull_request.url,
        state=pull_request.state or "open",
        author=pull_request.author,
        labels=pull_request.labels or [],
        body=pull_request.body,
        github_updated_at=pull_request.updated_at,
    )


def _active_numbers_by_kind(items: list[SourceItemUpsert]) -> dict[str, set[int]]:
    numbers: dict[str, set[int]] = {"issue": set(), "pull_request": set()}
    for item in items:
        numbers.setdefault(item.kind, set()).add(item.number)
    return numbers


def _next_local_ticket_number(session: Session, source_id: int) -> int:
    max_number = session.scalar(
        select(func.max(SourceItem.number)).where(
            SourceItem.source_id == source_id,
            SourceItem.kind == LOCAL_TICKET_KIND,
        )
    )
    return int(max_number or 0) + 1


def _delete_source_dependents(
    session: Session,
    source_id: int,
    source_item_ids: list[int],
    work_item_ids: list[int],
) -> None:
    if work_item_ids:
        thread_ids = list(
            session.scalars(select(ChatThread.id).where(ChatThread.work_item_id.in_(work_item_ids)))
        )
        if thread_ids:
            session.execute(delete(ChatMessage).where(ChatMessage.thread_id.in_(thread_ids)))
        session.execute(delete(ChatThread).where(ChatThread.work_item_id.in_(work_item_ids)))
        session.execute(delete(WorkItemRun).where(WorkItemRun.work_item_id.in_(work_item_ids)))
        session.execute(delete(WorkItemStateEvent).where(WorkItemStateEvent.work_item_id.in_(work_item_ids)))
        session.execute(delete(WorkItemLink).where(WorkItemLink.work_item_id.in_(work_item_ids)))
    if source_item_ids:
        session.execute(delete(WorkItemLink).where(WorkItemLink.source_item_id.in_(source_item_ids)))
        session.execute(delete(SourceItemLink).where(SourceItemLink.source_item_id.in_(source_item_ids)))
        session.execute(
            delete(SourceItemLink).where(SourceItemLink.linked_source_item_id.in_(source_item_ids))
        )
    if work_item_ids:
        session.execute(delete(WorkItem).where(WorkItem.id.in_(work_item_ids)))
    session.execute(delete(SourceItemLink).where(SourceItemLink.source_id == source_id))
    session.execute(delete(SourceSyncRun).where(SourceSyncRun.source_id == source_id))
    session.execute(delete(SourceItem).where(SourceItem.source_id == source_id))


def _source_item_view_with_links(item: SourceItem, linked_items: list[SourceItem]) -> SourceItemView:
    return replace(
        SourceItemView.from_model(item),
        linked_items=tuple(SourceItemView.from_model(linked_item) for linked_item in linked_items),
    )


def _visible_match_ids(session: Session, source_id: int, query: str) -> set[int]:
    if not query.strip():
        return set()
    matched_ids = matching_source_item_ids(session, source_id, query)
    if not matched_ids:
        return set()
    linked_issue_ids = set(
        session.scalars(
            select(SourceItemLink.source_item_id).where(
                SourceItemLink.source_id == source_id,
                SourceItemLink.relationship == "issue_pr",
                SourceItemLink.linked_source_item_id.in_(matched_ids),
            )
        )
    )
    return matched_ids | linked_issue_ids


def _linked_prs_by_issue_id(
    session: Session,
    source_id: int,
    source_items: list[SourceItem],
) -> dict[int, list[SourceItem]]:
    issue_ids = {item.id for item in source_items if item.kind == "issue"}
    available_ids = {item.id for item in source_items}
    if not issue_ids:
        return {}
    rows = session.execute(
        select(SourceItemLink.source_item_id, SourceItem)
        .join(SourceItem, SourceItemLink.linked_source_item_id == SourceItem.id)
        .where(
            SourceItemLink.source_id == source_id,
            SourceItemLink.relationship == "issue_pr",
            SourceItemLink.source_item_id.in_(issue_ids),
            SourceItemLink.linked_source_item_id.in_(available_ids),
            SourceItem.state == "open",
        )
        .order_by(SourceItem.number.asc())
    ).all()
    grouped: dict[int, list[SourceItem]] = {}
    for issue_id, linked_item in rows:
        grouped.setdefault(issue_id, []).append(linked_item)
    return grouped


def _linked_prs_for_issue(session: Session, source_item: SourceItem) -> list[SourceItem]:
    if source_item.kind != "issue":
        return []
    return list(
        session.scalars(
            select(SourceItem)
            .join(SourceItemLink, SourceItemLink.linked_source_item_id == SourceItem.id)
            .where(
                SourceItemLink.source_item_id == source_item.id,
                SourceItemLink.relationship == "issue_pr",
                SourceItem.kind == "pull_request",
                SourceItem.state == "open",
            )
            .order_by(SourceItem.number.asc())
        )
    )


def _record_issue_pr_links(session: Session, source_id: int, now: str) -> None:
    source = session.get(Source, source_id)
    if source is None:
        return
    issues = list(
        session.scalars(
            select(SourceItem).where(
                SourceItem.source_id == source_id,
                SourceItem.kind == "issue",
                SourceItem.state == "open",
            )
        )
    )
    local_tickets = list(
        session.scalars(
            select(SourceItem).where(
                SourceItem.source_id == source_id,
                SourceItem.kind == LOCAL_TICKET_KIND,
                SourceItem.state == "open",
            )
        )
    )
    pull_requests = list(
        session.scalars(
            select(SourceItem).where(
                SourceItem.source_id == source_id,
                SourceItem.kind == "pull_request",
                SourceItem.state == "open",
            )
        )
    )
    existing_links = list(
        session.scalars(
            select(SourceItemLink).where(
                SourceItemLink.source_id == source_id,
                SourceItemLink.relationship.in_(("issue_pr", "ticket_pr")),
            )
        )
    )
    existing_by_pair = {(link.source_item_id, link.linked_source_item_id): link for link in existing_links}
    verified_pairs: set[tuple[int, int]] = set()
    for issue in issues:
        marker = issue_link_marker(source.repo, issue.number)
        for pull_request in pull_requests:
            if not body_links_issue(pull_request.body, source.repo, issue.number):
                continue
            pair = (issue.id, pull_request.id)
            verified_pairs.add(pair)
            existing = existing_by_pair.get(pair)
            if existing:
                existing.link_source = "description_marker"
                existing.marker = marker
                existing.verified_at = now
            else:
                session.add(
                    SourceItemLink(
                        source_id=source_id,
                        source_item_id=issue.id,
                        linked_source_item_id=pull_request.id,
                        relationship="issue_pr",
                        link_source="description_marker",
                        marker=marker,
                        verified_at=now,
                    )
                )
    for ticket in local_tickets:
        marker = source_item_link_marker(ticket.id)
        for pull_request in pull_requests:
            if not body_links_source_item(pull_request.body, ticket.id):
                continue
            pair = (ticket.id, pull_request.id)
            verified_pairs.add(pair)
            existing = existing_by_pair.get(pair)
            if existing:
                existing.link_source = "description_marker"
                existing.marker = marker
                existing.verified_at = now
            else:
                session.add(
                    SourceItemLink(
                        source_id=source_id,
                        source_item_id=ticket.id,
                        linked_source_item_id=pull_request.id,
                        relationship="ticket_pr",
                        link_source="description_marker",
                        marker=marker,
                        verified_at=now,
                    )
                )
    for pair, link in existing_by_pair.items():
        if link.link_source == "description_marker" and pair not in verified_pairs:
            session.delete(link)
    session.flush()
    _attach_linked_prs_to_active_work_items(session, source_id, now)


def _attach_linked_prs_to_active_work_items(session: Session, source_id: int, now: str) -> None:
    rows = session.execute(
        select(WorkItem, SourceItemLink)
        .join(SourceItemLink, SourceItemLink.source_item_id == WorkItem.primary_source_item_id)
        .where(
            WorkItem.source_id == source_id,
            WorkItem.state != "done",
            SourceItemLink.relationship.in_(("issue_pr", "ticket_pr")),
        )
    ).all()
    for work_item, source_item_link in rows:
        _ensure_work_item_link(
            session,
            work_item_id=work_item.id,
            source_item_id=source_item_link.linked_source_item_id,
            relationship="linked_pr",
            now=now,
        )
        if work_item.active_pr_source_item_id is None:
            work_item.active_pr_source_item_id = source_item_link.linked_source_item_id
            work_item.updated_at = now
            _ensure_work_item_link(
                session,
                work_item_id=work_item.id,
                source_item_id=source_item_link.linked_source_item_id,
                relationship="active_pr",
                now=now,
            )


def _ensure_work_item_link(
    session: Session,
    *,
    work_item_id: int,
    source_item_id: int,
    relationship: str,
    now: str,
) -> None:
    existing = session.scalar(
        select(WorkItemLink).where(
            WorkItemLink.work_item_id == work_item_id,
            WorkItemLink.source_item_id == source_item_id,
            WorkItemLink.relationship == relationship,
        )
    )
    if existing is None:
        session.add(
            WorkItemLink(
                work_item_id=work_item_id,
                source_item_id=source_item_id,
                relationship=relationship,
                created_at=now,
            )
        )


def _auto_complete_work_items(session: Session, source_id: int, now: str) -> None:
    work_items = list(
        session.scalars(
            select(WorkItem).where(
                WorkItem.source_id == source_id,
                WorkItem.state != "done",
                WorkItem.disposition == "active",
            )
        )
    )
    for work_item in work_items:
        primary_item = session.get(SourceItem, work_item.primary_source_item_id)
        active_pr = (
            session.get(SourceItem, work_item.active_pr_source_item_id)
            if work_item.active_pr_source_item_id
            else None
        )
        outcome = _completion_outcome(primary_item, active_pr)
        if not outcome:
            continue
        previous_state = work_item.state
        work_item.state = "done"
        work_item.outcome = outcome
        work_item.updated_at = now
        session.add(
            WorkItemStateEvent(
                work_item_id=work_item.id,
                from_state=previous_state,
                to_state="done",
                reasons_json="[]",
                note=outcome,
                created_at=now,
            )
        )


def _completion_outcome(primary_item: SourceItem | None, active_pr: SourceItem | None) -> str:
    if primary_item is None:
        return "primary_source_missing"
    if primary_item.kind == "issue" and primary_item.state != "open":
        return "issue_closed_external"
    if active_pr and active_pr.state != "open":
        return "linked_pr_closed_or_merged_external"
    if primary_item.kind == "pull_request" and primary_item.state != "open":
        return "pull_request_closed_or_merged_external"
    return ""


def _filtered_items(items: list[SourceItemUpsert], filters: SourceFilters) -> list[SourceItemUpsert]:
    if not filters.has_filters:
        return items
    return [item for item in items if _matches_filters(item, filters)]


def _matches_filters(item: SourceItemUpsert, filters: SourceFilters) -> bool:
    checks = [
        _matches_labels(item.labels, filters.labels),
        _matches_author(item.author, filters.authors),
        _matches_date_floor(item.github_updated_at, filters.updated_after),
        _matches_date_ceiling(item.github_updated_at, filters.updated_before),
        _matches_stale_after(item.github_updated_at, filters.stale_after_days),
    ]
    return all(checks)


def _matches_labels(item_labels: list[str], filter_labels: tuple[str, ...]) -> bool:
    if not filter_labels:
        return True
    normalized = {label.lower() for label in item_labels}
    return all(label.lower() in normalized for label in filter_labels)


def _matches_author(author: str, authors: tuple[str, ...]) -> bool:
    return not authors or author.lower() in {value.lower() for value in authors}


def _matches_date_floor(updated_at: str, updated_after: str) -> bool:
    return not updated_after or _updated_date(updated_at) >= updated_after


def _matches_date_ceiling(updated_at: str, updated_before: str) -> bool:
    return not updated_before or _updated_date(updated_at) <= updated_before


def _matches_stale_after(updated_at: str, stale_after_days: int | None) -> bool:
    if stale_after_days is None:
        return True
    cutoff = (datetime.now(UTC) - timedelta(days=stale_after_days)).date().isoformat()
    return _updated_date(updated_at) <= cutoff


def _updated_date(updated_at: str) -> str:
    if not updated_at:
        return ""
    return datetime.fromisoformat(updated_at.replace("Z", "+00:00")).date().isoformat()


def _tokens(value: str) -> list[str]:
    return [token.strip() for token in re.split(r"[,\n]+", value) if token.strip()]


def _date_value(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    try:
        return datetime.strptime(normalized, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise SourceValidationError(f"{label} must use YYYY-MM-DD.") from exc


def _positive_int(value: str, label: str) -> int | None:
    normalized = value.strip()
    if not normalized:
        return None
    try:
        parsed = int(normalized)
    except ValueError as exc:
        raise SourceValidationError(f"{label} must be a number.") from exc
    if parsed < 1:
        raise SourceValidationError(f"{label} must be at least 1.")
    return parsed


def _positive_page(page: int) -> int:
    return max(1, page)


def _positive_page_limit(limit: int) -> int:
    return min(100, max(1, limit))


def _clamped_page(page: int, total: int, limit: int) -> int:
    if total == 0:
        return 1
    return min(page, ceil(total / limit))
