from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .clock import utc_now
from .db import SessionFactory
from .models import SourceItem, SourceItemLink, WorkItem, WorkItemLink, WorkItemRun, WorkItemStateEvent

KANBAN_STATES = ("todo", "in_progress", "in_review", "done")
TASK_TYPES = frozenset({"research", "code", "operations"})
DONE_STATE = "done"
STATE_LABELS = {
    "todo": "Todo",
    "in_progress": "In Progress",
    "in_review": "In Review",
    "done": "Done",
}
REVIEW_RERUN_REASONS = {
    "address_pr_comments": "Address PR comments",
    "fix_ci": "Fix CI",
    "resolve_merge_conflicts": "Resolve merge conflicts",
    "revise_implementation": "Revise implementation",
}


class WorkItemError(ValueError):
    """Raised when a work item operation cannot be completed."""


@dataclass(frozen=True)
class WorkItemActivation:
    source_item_id: int
    task_type: str
    user_hint: str = ""


@dataclass(frozen=True)
class WorkItemMove:
    work_item_id: int
    target_state: str
    reasons: list[str]
    note: str = ""


@dataclass(frozen=True)
class WorkItemView:
    id: int
    source_id: int
    primary_source_item_id: int
    source_kind: str
    source_number: int
    source_url: str
    active_pr_source_item_id: int | None
    title: str
    state: str
    task_type: str
    user_hint: str
    disposition: str
    disposition_note: str
    created_at: str
    updated_at: str

    @property
    def source_label(self) -> str:
        return "PR" if self.source_kind == "pull_request" else "Issue"

    @property
    def state_label(self) -> str:
        return STATE_LABELS[self.state]


@dataclass(frozen=True)
class WorkItemLinkedSourceView:
    id: int
    kind: str
    number: int
    title: str
    url: str
    relationship: str

    @property
    def source_label(self) -> str:
        return "PR" if self.kind == "pull_request" else "Issue"


@dataclass(frozen=True)
class OperationRunView:
    id: int
    work_item_id: int
    title: str
    status: str
    user_hint: str
    created_at: str
    updated_at: str


class WorkItemRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def activate_source_item(self, activation: WorkItemActivation) -> WorkItemView:
        task_type = _validated_task_type(activation.task_type)
        user_hint = activation.user_hint.strip()
        now = utc_now()
        with self._session_factory() as session:
            source_item = session.get(SourceItem, activation.source_item_id)
            if source_item is None:
                raise WorkItemError("Source item not found.")
            existing = session.scalar(
                select(WorkItem)
                .join(WorkItemLink, WorkItemLink.work_item_id == WorkItem.id)
                .where(
                    WorkItemLink.source_item_id == activation.source_item_id,
                    WorkItem.state != DONE_STATE,
                    WorkItem.disposition == "active",
                )
                .order_by(WorkItem.id.desc())
            )
            if existing:
                return _work_item_view(existing, source_item)

            linked_prs = _linked_prs_for_source_item(session, source_item)
            active_pr_source_item_id = _active_pr_source_item_id(source_item, linked_prs)
            work_item = WorkItem(
                source_id=source_item.source_id,
                primary_source_item_id=source_item.id,
                active_pr_source_item_id=active_pr_source_item_id,
                title=source_item.title,
                state="todo",
                task_type=task_type,
                user_hint=user_hint,
                outcome="",
                created_at=now,
                updated_at=now,
            )
            session.add(work_item)
            session.flush()
            session.add_all(
                [
                    WorkItemLink(
                        work_item_id=work_item.id,
                        source_item_id=source_item.id,
                        relationship=_primary_relationship(source_item),
                        created_at=now,
                    ),
                    *[
                        WorkItemLink(
                            work_item_id=work_item.id,
                            source_item_id=linked_pr.id,
                            relationship="linked_pr",
                            created_at=now,
                        )
                        for linked_pr in linked_prs
                    ],
                ]
            )
            if active_pr_source_item_id and source_item.kind != "pull_request":
                session.add(
                    WorkItemLink(
                        work_item_id=work_item.id,
                        source_item_id=active_pr_source_item_id,
                        relationship="active_pr",
                        created_at=now,
                    )
                )
            session.add(
                WorkItemStateEvent(
                    work_item_id=work_item.id,
                    from_state="backlog",
                    to_state="todo",
                    reasons_json="[]",
                    note=user_hint,
                    created_at=now,
                )
            )
            session.add(
                WorkItemRun(
                    work_item_id=work_item.id,
                    task_type=task_type,
                    trigger="activation",
                    status="queued",
                    reasons_json="[]",
                    user_hint=user_hint,
                    started_at=None,
                    completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            session.refresh(work_item)
            return _work_item_view(work_item, source_item)

    def list_by_state(self, source_id: int, state: str) -> list[WorkItemView]:
        with self._session_factory() as session:
            rows = session.execute(
                select(WorkItem, SourceItem)
                .join(SourceItem, WorkItem.primary_source_item_id == SourceItem.id)
                .where(
                    WorkItem.source_id == source_id,
                    WorkItem.state == state,
                    WorkItem.disposition == "active",
                )
                .order_by(WorkItem.updated_at.desc(), WorkItem.id.desc())
            ).all()
            return [_work_item_view(work_item, source_item) for work_item, source_item in rows]

    def list_all(self) -> list[WorkItemView]:
        with self._session_factory() as session:
            rows = session.execute(
                select(WorkItem, SourceItem)
                .join(SourceItem, WorkItem.primary_source_item_id == SourceItem.id)
                .where(WorkItem.disposition == "active")
                .order_by(WorkItem.updated_at.desc(), WorkItem.id.desc())
            ).all()
            return [_work_item_view(work_item, source_item) for work_item, source_item in rows]

    def list_operations(self) -> list[OperationRunView]:
        with self._session_factory() as session:
            rows = session.execute(
                select(WorkItemRun, WorkItem)
                .join(WorkItem, WorkItemRun.work_item_id == WorkItem.id)
                .where(WorkItem.task_type == "operations")
                .order_by(WorkItemRun.created_at.desc(), WorkItemRun.id.desc())
            ).all()
            return [
                OperationRunView(
                    id=run.id,
                    work_item_id=work_item.id,
                    title=work_item.title,
                    status=run.status,
                    user_hint=run.user_hint,
                    created_at=run.created_at,
                    updated_at=run.updated_at,
                )
                for run, work_item in rows
            ]

    def detail(self, work_item_id: int) -> WorkItemView | None:
        with self._session_factory() as session:
            row = session.execute(
                select(WorkItem, SourceItem)
                .join(SourceItem, WorkItem.primary_source_item_id == SourceItem.id)
                .where(WorkItem.id == work_item_id)
            ).one_or_none()
            if row is None:
                return None
            work_item, source_item = row
            return _work_item_view(work_item, source_item)

    def archive_work_item(self, work_item_id: int, note: str = "") -> WorkItemView:
        now = utc_now()
        with self._session_factory() as session:
            row = session.execute(
                select(WorkItem, SourceItem)
                .join(SourceItem, WorkItem.primary_source_item_id == SourceItem.id)
                .where(WorkItem.id == work_item_id)
            ).one_or_none()
            if row is None:
                raise WorkItemError("Work item not found.")
            work_item, source_item = row
            previous_state = work_item.state
            work_item.disposition = "archived"
            work_item.disposition_note = note.strip()
            work_item.disposition_at = now
            work_item.state = "done"
            work_item.outcome = "archived_by_user"
            work_item.updated_at = now
            session.add(
                WorkItemStateEvent(
                    work_item_id=work_item.id,
                    from_state=previous_state,
                    to_state="done",
                    reasons_json="[]",
                    note=work_item.disposition_note,
                    created_at=now,
                )
            )
            session.commit()
            session.refresh(work_item)
            return _work_item_view(work_item, source_item)

    def linked_source_items(self, work_item_id: int) -> list[WorkItemLinkedSourceView]:
        with self._session_factory() as session:
            rows = session.execute(
                select(SourceItem, WorkItemLink.relationship)
                .join(WorkItemLink, WorkItemLink.source_item_id == SourceItem.id)
                .where(WorkItemLink.work_item_id == work_item_id)
                .order_by(SourceItem.kind.asc(), SourceItem.number.asc(), WorkItemLink.relationship.asc())
            ).all()
            grouped: dict[int, tuple[SourceItem, set[str]]] = {}
            for source_item, relationship in rows:
                _, relationships = grouped.setdefault(source_item.id, (source_item, set()))
                relationships.add(relationship)
            return [
                WorkItemLinkedSourceView(
                    id=source_item.id,
                    kind=source_item.kind,
                    number=source_item.number,
                    title=source_item.title,
                    url=source_item.url,
                    relationship=", ".join(sorted(relationships)),
                )
                for source_item, relationships in grouped.values()
            ]

    def select_active_pr(self, work_item_id: int, source_item_id: int) -> WorkItemView:
        now = utc_now()
        with self._session_factory() as session:
            row = session.execute(
                select(WorkItem, SourceItem)
                .join(SourceItem, WorkItem.primary_source_item_id == SourceItem.id)
                .where(WorkItem.id == work_item_id)
            ).one_or_none()
            if row is None:
                raise WorkItemError("Work item not found.")
            work_item, primary_source_item = row
            linked_pr = session.execute(
                select(SourceItem)
                .join(WorkItemLink, WorkItemLink.source_item_id == SourceItem.id)
                .where(
                    WorkItemLink.work_item_id == work_item_id,
                    SourceItem.id == source_item_id,
                    SourceItem.kind == "pull_request",
                )
            ).scalar_one_or_none()
            if linked_pr is None:
                raise WorkItemError("Active PR must be linked to this work item.")
            work_item.active_pr_source_item_id = linked_pr.id
            work_item.updated_at = now
            _ensure_work_item_link(session, work_item_id, linked_pr.id, "active_pr", now)
            session.commit()
            session.refresh(work_item)
            return _work_item_view(work_item, primary_source_item)

    def link_source_item(
        self,
        *,
        work_item_id: int,
        source_item_id: int,
        relationship: str,
    ) -> WorkItemView:
        now = utc_now()
        with self._session_factory() as session:
            row = session.execute(
                select(WorkItem, SourceItem)
                .join(SourceItem, WorkItem.primary_source_item_id == SourceItem.id)
                .where(WorkItem.id == work_item_id)
            ).one_or_none()
            linked_source_item = session.get(SourceItem, source_item_id)
            if row is None or linked_source_item is None:
                raise WorkItemError("Work item or source item not found.")
            work_item, primary_source_item = row
            if linked_source_item.source_id != work_item.source_id:
                raise WorkItemError("Linked source item must belong to the same source.")
            _ensure_work_item_link(session, work_item_id, source_item_id, relationship, now)
            if relationship == "active_pr":
                if linked_source_item.kind != "pull_request":
                    raise WorkItemError("Active PR source item must be a pull request.")
                work_item.active_pr_source_item_id = linked_source_item.id
            work_item.updated_at = now
            session.commit()
            session.refresh(work_item)
            return _work_item_view(work_item, primary_source_item)

    def move_work_item(self, move: WorkItemMove) -> WorkItemView:
        target_state = _validated_state(move.target_state)
        reasons = _validated_reasons(move.reasons)
        note = move.note.strip()
        now = utc_now()
        with self._session_factory() as session:
            row = session.execute(
                select(WorkItem, SourceItem)
                .join(SourceItem, WorkItem.primary_source_item_id == SourceItem.id)
                .where(WorkItem.id == move.work_item_id)
            ).one_or_none()
            if row is None:
                raise WorkItemError("Work item not found.")
            work_item, source_item = row
            previous_state = work_item.state
            if previous_state == target_state:
                return _work_item_view(work_item, source_item)

            work_item.state = target_state
            work_item.updated_at = now
            session.add(
                WorkItemStateEvent(
                    work_item_id=work_item.id,
                    from_state=previous_state,
                    to_state=target_state,
                    reasons_json=reasons_json(reasons),
                    note=note,
                    created_at=now,
                )
            )
            if target_state == "in_progress":
                session.add(
                    WorkItemRun(
                        work_item_id=work_item.id,
                        task_type=work_item.task_type,
                        trigger="rerun" if previous_state == "in_review" else "manual_move",
                        status="queued",
                        reasons_json=reasons_json(reasons),
                        user_hint=note or work_item.user_hint,
                        started_at=None,
                        completed_at=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
            session.commit()
            session.refresh(work_item)
            return _work_item_view(work_item, source_item)


def _validated_task_type(task_type: str) -> str:
    if task_type not in TASK_TYPES:
        raise WorkItemError("Task type must be research, code, or operations.")
    return task_type


def _validated_state(state: str) -> str:
    if state not in KANBAN_STATES:
        raise WorkItemError("Target state must be todo, in_progress, in_review, or done.")
    return state


def _validated_reasons(reasons: list[str]) -> list[str]:
    invalid = sorted({reason for reason in reasons if reason not in REVIEW_RERUN_REASONS})
    if invalid:
        raise WorkItemError(f"Unknown rerun reason: {', '.join(invalid)}.")
    return reasons


def _work_item_view(work_item: WorkItem, source_item: SourceItem) -> WorkItemView:
    return WorkItemView(
        id=work_item.id,
        source_id=work_item.source_id,
        primary_source_item_id=work_item.primary_source_item_id,
        source_kind=source_item.kind,
        source_number=source_item.number,
        source_url=source_item.url,
        active_pr_source_item_id=work_item.active_pr_source_item_id,
        title=work_item.title,
        state=work_item.state,
        task_type=work_item.task_type,
        user_hint=work_item.user_hint,
        disposition=work_item.disposition,
        disposition_note=work_item.disposition_note,
        created_at=work_item.created_at,
        updated_at=work_item.updated_at,
    )


def reasons_json(reasons: list[str]) -> str:
    return json.dumps(reasons, sort_keys=True)


def _linked_prs_for_source_item(session: Session, source_item: SourceItem) -> list[SourceItem]:
    if source_item.kind == "pull_request":
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


def _active_pr_source_item_id(source_item: SourceItem, linked_prs: list[SourceItem]) -> int | None:
    if source_item.kind == "pull_request":
        return source_item.id
    return linked_prs[0].id if linked_prs else None


def _primary_relationship(source_item: SourceItem) -> str:
    return "source_pr" if source_item.kind == "pull_request" else "primary_issue"


def _ensure_work_item_link(
    session: Session,
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
