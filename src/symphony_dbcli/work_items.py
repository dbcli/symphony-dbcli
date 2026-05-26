from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import select, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from .clock import utc_now
from .db import SessionFactory
from .models import (
    Source,
    SourceItem,
    SourceItemLink,
    WorkItem,
    WorkItemLink,
    WorkItemRun,
    WorkItemStateEvent,
)
from .search import matching_source_item_ids

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


@dataclass(frozen=True)
class WorkItemRunView:
    id: int | None
    attempt_id: int | None
    workflow_instance_id: int | None
    task_type: str
    trigger: str
    status: str
    reasons: list[str]
    user_hint: str
    started_at: str | None
    completed_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WorkItemRunClaim:
    id: int
    work_item_id: int
    source_id: int
    repo: str
    task_type: str
    title: str
    user_hint: str
    rerun_reasons: list[str]
    primary_source_item_id: int
    source_kind: str
    source_number: int
    source_url: str
    active_pr_source_item_id: int | None
    active_pr_number: int | None
    active_pr_url: str
    active_pr_title: str

    @property
    def issue_number(self) -> int:
        return self.source_number

    @property
    def source_label(self) -> str:
        return "PR" if self.source_kind == "pull_request" else "Issue"

    def workflow_artifacts(self) -> dict[str, object]:
        artifacts: dict[str, object] = {
            "work_item.id": self.work_item_id,
            "work_item.run_id": self.id,
            "work_item.user_hint": self.user_hint,
            "work_item.rerun_reasons": self.rerun_reasons,
            "source.id": self.source_id,
            "source.repo": self.repo,
            "source_item.id": self.primary_source_item_id,
            "source_item.kind": self.source_kind,
            "source_item.number": self.source_number,
            "source_item.url": self.source_url,
            "source_item.title": self.title,
        }
        if self.source_kind == "issue":
            artifacts.update(
                {
                    "linked_issue.source_item_id": self.primary_source_item_id,
                    "linked_issue.number": self.source_number,
                    "linked_issue.url": self.source_url,
                    "linked_issue.title": self.title,
                }
            )
        if self.active_pr_number is not None:
            artifacts.update(
                {
                    "pull_request.exists": True,
                    "pull_request.source_item_id": self.active_pr_source_item_id,
                    "pull_request.number": self.active_pr_number,
                    "pull_request.url": self.active_pr_url,
                    "pull_request.title": self.active_pr_title,
                }
            )
        return artifacts


class WorkItemRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def has_sources(self) -> bool:
        with self._session_factory() as session:
            return session.scalar(select(Source.id).limit(1)) is not None

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

    def list_by_state(self, source_id: int, state: str, *, query: str = "") -> list[WorkItemView]:
        with self._session_factory() as session:
            matching_work_item_ids = _matching_work_item_ids(session, source_id, query)
            if query.strip() and not matching_work_item_ids:
                return []
            conditions = [
                WorkItem.source_id == source_id,
                WorkItem.state == state,
                WorkItem.disposition == "active",
            ]
            if matching_work_item_ids:
                conditions.append(WorkItem.id.in_(matching_work_item_ids))
            rows = session.execute(
                select(WorkItem, SourceItem)
                .join(SourceItem, WorkItem.primary_source_item_id == SourceItem.id)
                .where(*conditions)
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

    def list_runs(self, work_item_id: int) -> list[WorkItemRunView]:
        with self._session_factory() as session:
            context = session.execute(
                select(WorkItem, SourceItem, Source)
                .join(SourceItem, WorkItem.primary_source_item_id == SourceItem.id)
                .join(Source, WorkItem.source_id == Source.id)
                .where(WorkItem.id == work_item_id)
            ).one_or_none()
            if context is None:
                return []
            _work_item, source_item, source = context
            runs = session.scalars(
                select(WorkItemRun)
                .where(WorkItemRun.work_item_id == work_item_id)
                .order_by(WorkItemRun.created_at.desc(), WorkItemRun.id.desc())
            ).all()
            run_views = [_work_item_run_view(run) for run in runs]
            seen_attempt_ids = {run.attempt_id for run in run_views if run.attempt_id is not None}
            related_attempts = session.execute(
                text(
                    """
                    SELECT
                        a.id AS attempt_id,
                        a.task_type AS task_type,
                        a.status AS status,
                        a.started_at AS started_at,
                        a.completed_at AS completed_at,
                        a.created_at AS created_at,
                        a.updated_at AS updated_at,
                        COALESCE(
                            (
                                SELECT wi.id
                                FROM workflow_instances wi
                                WHERE wi.attempt_id = a.id
                                  AND wi.work_item_id = :work_item_id
                                ORDER BY wi.id DESC
                                LIMIT 1
                            ),
                            (
                                SELECT wi.id
                                FROM workflow_instances wi
                                WHERE wi.attempt_id = a.id
                                ORDER BY wi.id DESC
                                LIMIT 1
                            )
                        ) AS workflow_instance_id,
                        CASE
                            WHEN a.work_item_id = :work_item_id THEN 'attempt'
                            WHEN EXISTS (
                                SELECT 1
                                FROM workflow_instances wi
                                WHERE wi.attempt_id = a.id
                                  AND wi.work_item_id = :work_item_id
                            ) THEN 'workflow'
                            ELSE 'legacy'
                        END AS trigger
                    FROM attempts a
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM work_item_runs wir
                        WHERE wir.work_item_id = :work_item_id
                          AND wir.attempt_id = a.id
                    )
                      AND (
                        a.work_item_id = :work_item_id
                        OR EXISTS (
                            SELECT 1
                            FROM workflow_instances wi
                            WHERE wi.attempt_id = a.id
                              AND wi.work_item_id = :work_item_id
                        )
                        OR (
                            a.work_item_id IS NULL
                            AND a.repo = :repo
                            AND a.issue_number = :issue_number
                        )
                      )
                    """
                ),
                {
                    "work_item_id": work_item_id,
                    "repo": source.repo,
                    "issue_number": source_item.number,
                },
            ).mappings()
            run_views.extend(
                _work_item_attempt_view(row)
                for row in related_attempts
                if int(row["attempt_id"]) not in seen_attempt_ids
            )
            return sorted(run_views, key=_work_item_run_sort_key, reverse=True)

    def next_queued_run(self, *, blocked_repos: set[str] | None = None) -> WorkItemRunClaim | None:
        with self._session_factory() as session:
            conditions = [
                WorkItemRun.status == "queued",
                WorkItem.disposition == "active",
                WorkItem.state.in_(("todo", "in_progress")),
            ]
            if blocked_repos:
                conditions.append(Source.repo.not_in(blocked_repos))
            row = session.execute(
                select(WorkItemRun, WorkItem, SourceItem, Source)
                .join(WorkItem, WorkItemRun.work_item_id == WorkItem.id)
                .join(SourceItem, WorkItem.primary_source_item_id == SourceItem.id)
                .join(Source, WorkItem.source_id == Source.id)
                .where(*conditions)
                .order_by(WorkItemRun.created_at.asc(), WorkItemRun.id.asc())
                .limit(1)
            ).one_or_none()
            if row is None:
                return None
            run, work_item, source_item, source = row
            active_pr = (
                session.get(SourceItem, work_item.active_pr_source_item_id)
                if work_item.active_pr_source_item_id
                else None
            )
            return _work_item_run_claim(run, work_item, source_item, source, active_pr)

    def assign_run_attempt(
        self,
        *,
        run_id: int,
        attempt_id: int,
        workflow_instance_id: int,
    ) -> WorkItemRunClaim:
        now = utc_now()
        with self._session_factory() as session:
            row = session.execute(
                select(WorkItemRun, WorkItem, SourceItem, Source)
                .join(WorkItem, WorkItemRun.work_item_id == WorkItem.id)
                .join(SourceItem, WorkItem.primary_source_item_id == SourceItem.id)
                .join(Source, WorkItem.source_id == Source.id)
                .where(WorkItemRun.id == run_id)
            ).one_or_none()
            if row is None:
                raise WorkItemError("Work item run not found.")
            run, work_item, source_item, source = row
            previous_state = work_item.state
            run.attempt_id = attempt_id
            run.workflow_instance_id = workflow_instance_id
            run.updated_at = now
            work_item.state = "in_progress"
            work_item.updated_at = now
            if previous_state != "in_progress":
                session.add(
                    WorkItemStateEvent(
                        work_item_id=work_item.id,
                        from_state=previous_state,
                        to_state="in_progress",
                        reasons_json=run.reasons_json,
                        note=run.user_hint,
                        created_at=now,
                    )
                )
            session.commit()
            active_pr = (
                session.get(SourceItem, work_item.active_pr_source_item_id)
                if work_item.active_pr_source_item_id
                else None
            )
            return _work_item_run_claim(run, work_item, source_item, source, active_pr)

    def start_attempt_run(self, attempt_id: int) -> None:
        now = utc_now()
        with self._session_factory() as session:
            run = session.scalar(select(WorkItemRun).where(WorkItemRun.attempt_id == attempt_id))
            if run is None:
                return
            run.status = "running"
            run.started_at = run.started_at or now
            run.updated_at = now
            session.commit()

    def requeue_attempt_run(self, attempt_id: int, *, reason: str) -> None:
        now = utc_now()
        with self._session_factory() as session:
            run = session.scalar(select(WorkItemRun).where(WorkItemRun.attempt_id == attempt_id))
            if run is None:
                return
            run.status = "queued"
            run.updated_at = now
            session.commit()

    def finish_attempt_run(self, attempt_id: int, *, status: str, outcome: str) -> None:
        now = utc_now()
        with self._session_factory() as session:
            row = session.execute(
                select(WorkItemRun, WorkItem)
                .join(WorkItem, WorkItemRun.work_item_id == WorkItem.id)
                .where(WorkItemRun.attempt_id == attempt_id)
            ).one_or_none()
            if row is None:
                return
            run, work_item = row
            previous_state = work_item.state
            target_state = _target_state_for_attempt_status(status)
            run.status = _run_status_for_attempt_status(status)
            run.completed_at = now
            run.updated_at = now
            work_item.state = target_state
            work_item.outcome = outcome
            work_item.updated_at = now
            if previous_state != target_state:
                session.add(
                    WorkItemStateEvent(
                        work_item_id=work_item.id,
                        from_state=previous_state,
                        to_state=target_state,
                        reasons_json=run.reasons_json,
                        note=outcome,
                        created_at=now,
                    )
                )
            session.commit()

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

    def record_created_pull_request(
        self,
        *,
        work_item_id: int,
        number: int,
        url: str,
        title: str,
        body: str,
    ) -> WorkItemView | None:
        now = utc_now()
        with self._session_factory() as session:
            row = session.execute(
                select(WorkItem, SourceItem)
                .join(SourceItem, WorkItem.primary_source_item_id == SourceItem.id)
                .where(WorkItem.id == work_item_id)
            ).one_or_none()
            if row is None:
                return None
            work_item, primary_source_item = row
            existing = session.scalar(
                select(SourceItem).where(
                    SourceItem.source_id == work_item.source_id,
                    SourceItem.kind == "pull_request",
                    SourceItem.number == number,
                )
            )
            pull_request = existing or SourceItem(
                source_id=work_item.source_id,
                kind="pull_request",
                number=number,
                title=title,
                url=url,
                state="open",
                author="symphony",
                labels_json="[]",
                body=body,
                github_updated_at=now,
                synced_at=now,
                created_at=now,
                updated_at=now,
            )
            if existing is None:
                session.add(pull_request)
                session.flush()
            else:
                pull_request.title = title
                pull_request.url = url
                pull_request.state = "open"
                pull_request.body = body
                pull_request.github_updated_at = now
                pull_request.synced_at = now
                pull_request.updated_at = now
            _ensure_work_item_link(session, work_item.id, pull_request.id, "linked_pr", now)
            _ensure_work_item_link(session, work_item.id, pull_request.id, "active_pr", now)
            work_item.active_pr_source_item_id = pull_request.id
            work_item.updated_at = now
            if primary_source_item.kind == "issue":
                _ensure_source_item_link(
                    session,
                    source_id=work_item.source_id,
                    source_item_id=primary_source_item.id,
                    linked_source_item_id=pull_request.id,
                    relationship="issue_pr",
                    link_source="created_by_symphony",
                    marker=body,
                    now=now,
                )
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


def _matching_work_item_ids(session: Session, source_id: int, query: str) -> set[int]:
    if not query.strip():
        return set()
    source_item_ids = matching_source_item_ids(session, source_id, query)
    if not source_item_ids:
        return set()
    return set(
        session.scalars(
            select(WorkItemLink.work_item_id)
            .join(WorkItem, WorkItemLink.work_item_id == WorkItem.id)
            .where(
                WorkItem.source_id == source_id,
                WorkItemLink.source_item_id.in_(source_item_ids),
                WorkItem.disposition == "active",
            )
        )
    )


def _work_item_run_claim(
    run: WorkItemRun,
    work_item: WorkItem,
    source_item: SourceItem,
    source: Source,
    active_pr: SourceItem | None,
) -> WorkItemRunClaim:
    return WorkItemRunClaim(
        id=run.id,
        work_item_id=work_item.id,
        source_id=work_item.source_id,
        repo=source.repo,
        task_type=run.task_type,
        title=work_item.title,
        user_hint=run.user_hint,
        rerun_reasons=cast(list[str], json.loads(run.reasons_json)),
        primary_source_item_id=work_item.primary_source_item_id,
        source_kind=source_item.kind,
        source_number=source_item.number,
        source_url=source_item.url,
        active_pr_source_item_id=None if active_pr is None else active_pr.id,
        active_pr_number=None if active_pr is None else active_pr.number,
        active_pr_url="" if active_pr is None else active_pr.url,
        active_pr_title="" if active_pr is None else active_pr.title,
    )


def _work_item_run_view(run: WorkItemRun) -> WorkItemRunView:
    return WorkItemRunView(
        id=run.id,
        attempt_id=run.attempt_id,
        workflow_instance_id=run.workflow_instance_id,
        task_type=run.task_type,
        trigger=run.trigger,
        status=run.status,
        reasons=cast(list[str], json.loads(run.reasons_json)),
        user_hint=run.user_hint,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _work_item_attempt_view(row: RowMapping) -> WorkItemRunView:
    return WorkItemRunView(
        id=None,
        attempt_id=int(row["attempt_id"]),
        workflow_instance_id=_optional_int(row["workflow_instance_id"]),
        task_type=str(row["task_type"]),
        trigger=str(row["trigger"]),
        status=str(row["status"]),
        reasons=[],
        user_hint="",
        started_at=_optional_str(row["started_at"]),
        completed_at=_optional_str(row["completed_at"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _work_item_run_sort_key(run: WorkItemRunView) -> tuple[str, int, int]:
    return (run.created_at, run.attempt_id or 0, run.id or 0)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


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


def _ensure_source_item_link(
    session: Session,
    *,
    source_id: int,
    source_item_id: int,
    linked_source_item_id: int,
    relationship: str,
    link_source: str,
    marker: str,
    now: str,
) -> None:
    existing = session.scalar(
        select(SourceItemLink).where(
            SourceItemLink.source_item_id == source_item_id,
            SourceItemLink.linked_source_item_id == linked_source_item_id,
            SourceItemLink.relationship == relationship,
        )
    )
    if existing:
        existing.link_source = link_source
        existing.marker = marker
        existing.verified_at = now
        return
    session.add(
        SourceItemLink(
            source_id=source_id,
            source_item_id=source_item_id,
            linked_source_item_id=linked_source_item_id,
            relationship=relationship,
            link_source=link_source,
            marker=marker,
            verified_at=now,
        )
    )


def _target_state_for_attempt_status(status: str) -> str:
    if status == "done":
        return "done"
    return "in_review"


def _run_status_for_attempt_status(status: str) -> str:
    if status == "review":
        return "needs_review"
    if status == "done":
        return "succeeded"
    return status
