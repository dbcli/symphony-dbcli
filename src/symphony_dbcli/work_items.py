from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select

from .clock import utc_now
from .db import SessionFactory
from .models import SourceItem, WorkItem, WorkItemLink, WorkItemRun, WorkItemStateEvent

KANBAN_STATES = ("todo", "in_progress", "in_review", "done")
TASK_TYPES = frozenset({"research", "code", "operations"})
DONE_STATE = "done"


class WorkItemError(ValueError):
    """Raised when a work item operation cannot be completed."""


@dataclass(frozen=True)
class WorkItemActivation:
    source_item_id: int
    task_type: str
    user_hint: str = ""


@dataclass(frozen=True)
class WorkItemView:
    id: int
    source_id: int
    primary_source_item_id: int
    source_kind: str
    source_number: int
    source_url: str
    title: str
    state: str
    task_type: str
    user_hint: str
    created_at: str
    updated_at: str

    @property
    def source_label(self) -> str:
        return "PR" if self.source_kind == "pull_request" else "Issue"


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
                )
                .order_by(WorkItem.id.desc())
            )
            if existing:
                return _work_item_view(existing, source_item)

            work_item = WorkItem(
                source_id=source_item.source_id,
                primary_source_item_id=source_item.id,
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
            session.add(
                WorkItemLink(
                    work_item_id=work_item.id,
                    source_item_id=source_item.id,
                    relationship="primary",
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
                .where(WorkItem.source_id == source_id, WorkItem.state == state)
                .order_by(WorkItem.updated_at.desc(), WorkItem.id.desc())
            ).all()
            return [_work_item_view(work_item, source_item) for work_item, source_item in rows]

    def list_all(self) -> list[WorkItemView]:
        with self._session_factory() as session:
            rows = session.execute(
                select(WorkItem, SourceItem)
                .join(SourceItem, WorkItem.primary_source_item_id == SourceItem.id)
                .order_by(WorkItem.updated_at.desc(), WorkItem.id.desc())
            ).all()
            return [_work_item_view(work_item, source_item) for work_item, source_item in rows]

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


def _validated_task_type(task_type: str) -> str:
    if task_type not in TASK_TYPES:
        raise WorkItemError("Task type must be research, code, or operations.")
    return task_type


def _work_item_view(work_item: WorkItem, source_item: SourceItem) -> WorkItemView:
    return WorkItemView(
        id=work_item.id,
        source_id=work_item.source_id,
        primary_source_item_id=work_item.primary_source_item_id,
        source_kind=source_item.kind,
        source_number=source_item.number,
        source_url=source_item.url,
        title=work_item.title,
        state=work_item.state,
        task_type=work_item.task_type,
        user_hint=work_item.user_hint,
        created_at=work_item.created_at,
        updated_at=work_item.updated_at,
    )


def reasons_json(reasons: list[str]) -> str:
    return json.dumps(reasons, sort_keys=True)
