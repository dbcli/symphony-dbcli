from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .clock import utc_now
from .db import SessionFactory
from .models import (
    ChatMessage,
    ChatThread,
    Source,
    SourceItem,
    WorkItem,
    WorkItemLink,
    WorkItemRun,
    WorkItemStateEvent,
)
from .work_items import CONVERSATION_KIND, reasons_json

CHAT_THREAD_ACTIVE = "active"
CHAT_THREAD_IMPLEMENTATION_QUEUED = "implementation_queued"

_CODE_TERMS = frozenset(
    {
        "add",
        "build",
        "change",
        "code",
        "fix",
        "implement",
        "pr",
        "refactor",
        "remove",
        "ship",
        "test",
        "update",
    }
)
_OPERATIONS_TERMS = frozenset(
    {
        "deploy",
        "install",
        "pull",
        "redeploy",
        "restart",
        "server",
        "systemd",
        "vm",
    }
)
_RESEARCH_TERMS = frozenset({"explain", "how", "investigate", "question", "should", "what", "why"})


class ChatError(ValueError):
    """Raised when a conversation work item cannot be created."""


@dataclass(frozen=True)
class ChatMessageView:
    id: int
    role: str
    body: str
    created_at: str

    @classmethod
    def from_model(cls, message: ChatMessage) -> ChatMessageView:
        return cls(
            id=message.id,
            role=message.role,
            body=message.body,
            created_at=message.created_at,
        )


@dataclass(frozen=True)
class ChatThreadView:
    id: int
    work_item_id: int
    source_id: int
    title: str
    status: str
    task_type: str
    messages: list[ChatMessageView]
    latest_run: ChatRunView | None
    created_at: str
    updated_at: str

    @property
    def implementation_queued(self) -> bool:
        return self.status == CHAT_THREAD_IMPLEMENTATION_QUEUED


@dataclass(frozen=True)
class ChatRunView:
    id: int
    attempt_id: int | None
    workflow_instance_id: int | None
    task_type: str
    trigger: str
    status: str
    started_at: str | None
    completed_at: str | None
    created_at: str
    updated_at: str


class ChatRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def start_thread(
        self,
        prompt: str,
        *,
        source_id: int | None = None,
        queue_run: bool = False,
    ) -> ChatThreadView:
        body = prompt.strip()
        if not body:
            raise ChatError("Message is required.")
        now = utc_now()
        with self._session_factory() as session:
            source = _selected_source(session, source_id)
            number = _next_conversation_number(session, source.id)
            title = _thread_title(body)
            source_item = SourceItem(
                source_id=source.id,
                kind=CONVERSATION_KIND,
                number=number,
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
            task_type = infer_task_type(body)
            work_item = WorkItem(
                source_id=source.id,
                primary_source_item_id=source_item.id,
                active_pr_source_item_id=None,
                title=title,
                state="in_progress",
                task_type=task_type,
                user_hint=body,
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
                    relationship="conversation",
                    created_at=now,
                )
            )
            session.add(
                WorkItemStateEvent(
                    work_item_id=work_item.id,
                    from_state="chat",
                    to_state="in_progress",
                    reasons_json="[]",
                    note=body,
                    created_at=now,
                )
            )
            thread = ChatThread(
                work_item_id=work_item.id,
                title=title,
                status=CHAT_THREAD_IMPLEMENTATION_QUEUED if queue_run else CHAT_THREAD_ACTIVE,
                created_at=now,
                updated_at=now,
            )
            session.add(thread)
            session.flush()
            session.add(ChatMessage(thread_id=thread.id, role="user", body=body, created_at=now))
            if queue_run:
                _queue_chat_run(session, work_item.id, task_type, body, created_at=now)
            session.commit()
            return _thread_view(session, thread.id)

    def codex_thread_id_for_work_item(self, work_item_id: int) -> str | None:
        with self._session_factory() as session:
            thread_id = session.scalar(
                select(ChatThread.codex_thread_id).where(ChatThread.work_item_id == work_item_id)
            )
            if thread_id is None:
                return None
            cleaned = thread_id.strip()
            return cleaned or None

    def save_codex_thread_id_for_work_item(self, work_item_id: int, codex_thread_id: str) -> None:
        cleaned = codex_thread_id.strip()
        if not cleaned:
            return
        now = utc_now()
        with self._session_factory() as session:
            thread = session.scalar(select(ChatThread).where(ChatThread.work_item_id == work_item_id))
            if thread is None:
                return
            thread.codex_thread_id = cleaned
            thread.updated_at = now
            session.commit()


def infer_task_type(text: str) -> str:
    normalized = text.lower()
    tokens = set(re.findall(r"[a-z0-9_]+", normalized))
    if tokens & _OPERATIONS_TERMS:
        return "operations"
    if tokens & _CODE_TERMS:
        return "code"
    if tokens & _RESEARCH_TERMS:
        return "research"
    if "?" in text:
        return "research"
    return "code" if "pr" in normalized or "pull request" in normalized else "research"


def _selected_source(session: Session, source_id: int | None) -> Source:
    if source_id is not None:
        source = session.get(Source, source_id)
        if source is None:
            raise ChatError("Source not found.")
        return source
    source = session.scalars(
        select(Source).where(Source.enabled.is_(True)).order_by(Source.repo.asc())
    ).first()
    if source is None:
        source = session.scalars(select(Source).order_by(Source.repo.asc())).first()
    if source is None:
        raise ChatError("Add a source before starting work.")
    return source


def _next_conversation_number(session: Session, source_id: int) -> int:
    max_number = session.scalar(
        select(func.max(SourceItem.number)).where(
            SourceItem.source_id == source_id,
            SourceItem.kind == CONVERSATION_KIND,
        )
    )
    return int(max_number or 0) + 1


def _thread_title(body: str) -> str:
    line = " ".join(body.split())
    if not line:
        return "New Work"
    if len(line) <= 72:
        return line
    return line[:69].rstrip() + "..."


def _thread_row(session: Session, thread_id: int) -> tuple[ChatThread, WorkItem] | None:
    row = session.execute(
        select(ChatThread, WorkItem)
        .join(WorkItem, ChatThread.work_item_id == WorkItem.id)
        .where(ChatThread.id == thread_id)
    ).one_or_none()
    if row is None:
        return None
    thread, work_item = row
    return thread, work_item


def _thread_view(session: Session, thread_id: int) -> ChatThreadView:
    row = _thread_row(session, thread_id)
    if row is None:
        raise ChatError("Conversation not found.")
    thread, work_item = row
    messages = list(
        session.scalars(
            select(ChatMessage).where(ChatMessage.thread_id == thread.id).order_by(ChatMessage.id.asc())
        )
    )
    return ChatThreadView(
        id=thread.id,
        work_item_id=thread.work_item_id,
        source_id=work_item.source_id,
        title=thread.title,
        status=thread.status,
        task_type=work_item.task_type,
        messages=[ChatMessageView.from_model(message) for message in messages],
        latest_run=_latest_run_view(session, work_item.id),
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


def _queue_chat_run(
    session: Session,
    work_item_id: int,
    task_type: str,
    user_hint: str,
    *,
    created_at: str,
) -> None:
    session.add(
        WorkItemRun(
            work_item_id=work_item_id,
            task_type=task_type,
            trigger="chat_implementation",
            status="queued",
            reasons_json=reasons_json([]),
            user_hint=user_hint,
            started_at=None,
            completed_at=None,
            created_at=created_at,
            updated_at=created_at,
        )
    )


def _latest_run_view(session: Session, work_item_id: int) -> ChatRunView | None:
    run = session.scalars(
        select(WorkItemRun)
        .where(WorkItemRun.work_item_id == work_item_id)
        .order_by(WorkItemRun.created_at.desc(), WorkItemRun.id.desc())
        .limit(1)
    ).first()
    if run is None:
        return None
    return ChatRunView(
        id=run.id,
        attempt_id=run.attempt_id,
        workflow_instance_id=run.workflow_instance_id,
        task_type=run.task_type,
        trigger=run.trigger,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )
