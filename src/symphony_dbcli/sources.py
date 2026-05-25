from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .clock import utc_now
from .db import SessionFactory
from .github import GitHubIssue, PullRequest
from .models import Source, SourceItem, SourceSyncRun, WorkItem, WorkItemLink

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


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
    synced_at: str

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
            synced_at=item.synced_at,
        )

    @property
    def kind_label(self) -> str:
        return "PR" if self.kind == "pull_request" else "Issue"

    @property
    def default_task_type(self) -> str:
        return "code" if self.kind == "pull_request" else "research"


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

    def open_source_items(self, source_id: int) -> list[SourceItemView]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(SourceItem)
                .where(SourceItem.source_id == source_id, SourceItem.state == "open")
                .order_by(SourceItem.kind.asc(), SourceItem.number.desc())
            ).all()
            return [SourceItemView.from_model(row) for row in rows]

    def backlog_source_items(self, source_id: int) -> list[SourceItemView]:
        linked_active_source_items = (
            select(WorkItemLink.source_item_id)
            .join(WorkItem, WorkItemLink.work_item_id == WorkItem.id)
            .where(WorkItem.state != "done")
        )
        with self._session_factory() as session:
            rows = session.scalars(
                select(SourceItem)
                .where(
                    SourceItem.source_id == source_id,
                    SourceItem.state == "open",
                    ~SourceItem.id.in_(linked_active_source_items),
                )
                .order_by(SourceItem.kind.asc(), SourceItem.number.desc())
            ).all()
            return [SourceItemView.from_model(row) for row in rows]

    def get_source_item(self, source_item_id: int) -> SourceItemView | None:
        with self._session_factory() as session:
            source_item = session.get(SourceItem, source_item_id)
            return SourceItemView.from_model(source_item) if source_item else None

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
            for kind, active_numbers in active_by_kind.items():
                stale_items = session.scalars(
                    select(SourceItem).where(SourceItem.source_id == source_id, SourceItem.kind == kind)
                ).all()
                for stale_item in stale_items:
                    if stale_item.number not in active_numbers:
                        stale_item.state = "closed"
                        stale_item.updated_at = now
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
