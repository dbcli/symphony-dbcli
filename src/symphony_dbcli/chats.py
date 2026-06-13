from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .clock import utc_now
from .config import WorkflowConfig
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
from .work_items import CONVERSATION_KIND, TASK_TYPES, reasons_json

CHAT_THREAD_ACTIVE = "active"
CHAT_THREAD_IMPLEMENTATION_QUEUED = "implementation_queued"
CHAT_MESSAGE_ROLES = frozenset({"user", "assistant"})
ChatDecisionAction = Literal["ask_followup", "start_work"]
CHAT_DECISION_ACTIONS = frozenset({"ask_followup", "start_work"})

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
    """Raised when a chat operation cannot be completed."""


@dataclass(frozen=True)
class ChatDecision:
    action: ChatDecisionAction
    task_type: str
    message: str


class ChatAssistantModel(Protocol):
    def decide(self, thread: ChatThreadView, message: str, *, context: str = "") -> ChatDecision: ...


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


@dataclass(frozen=True)
class CodexChatAssistant:
    config: WorkflowConfig
    cwd: Path

    def decide(self, thread: ChatThreadView, message: str, *, context: str = "") -> ChatDecision:
        command = [
            self.config.codex.command,
            "exec",
            "--cd",
            str(self.cwd),
            "--sandbox",
            "read-only",
            "-c",
            f'approval_policy="{self.config.codex.approval_policy}"',
        ]
        if self.config.codex.workflow_edit_reasoning_effort:
            command.extend(
                [
                    "-c",
                    f'model_reasoning_effort="{self.config.codex.workflow_edit_reasoning_effort}"',
                ]
            )
        chat_model = self.config.codex.workflow_edit_model or self.config.codex.model
        if chat_model:
            command.extend(["--model", chat_model])
        command.append(_chat_decision_prompt(thread, message, context=context))
        try:
            result = subprocess.run(command, text=True, capture_output=True, check=False)
        except OSError as exc:
            raise ChatError(str(exc)) from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or "codex chat assistant failed"
            raise ChatError(detail)
        return parse_chat_decision(
            result.stdout, fallback_task_type=infer_task_type(_thread_transcript(thread))
        )


class ChatRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def start_thread(self, prompt: str, *, source_id: int | None = None) -> ChatThreadView:
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
                status=CHAT_THREAD_ACTIVE,
                created_at=now,
                updated_at=now,
            )
            session.add(thread)
            session.flush()
            session.add(ChatMessage(thread_id=thread.id, role="user", body=body, created_at=now))
            session.commit()
            return _thread_view(session, thread.id)

    def add_message(self, thread_id: int, body: str) -> ChatThreadView:
        message = body.strip()
        if not message:
            raise ChatError("Message is required.")
        now = utc_now()
        with self._session_factory() as session:
            row = _thread_row(session, thread_id)
            if row is None:
                raise ChatError("Chat thread not found.")
            thread, work_item, source_item = row
            session.add(ChatMessage(thread_id=thread.id, role="user", body=message, created_at=now))
            session.flush()
            transcript = _transcript_for_thread(session, thread.id)
            thread.updated_at = now
            work_item.user_hint = transcript
            work_item.task_type = infer_task_type(transcript)
            work_item.updated_at = now
            source_item.body = transcript
            source_item.updated_at = now
            session.commit()
            return _thread_view(session, thread.id)

    def get_thread(self, thread_id: int) -> ChatThreadView | None:
        with self._session_factory() as session:
            if session.get(ChatThread, thread_id) is None:
                return None
            return _thread_view(session, thread_id)

    def get_thread_for_work_item(self, work_item_id: int) -> ChatThreadView | None:
        with self._session_factory() as session:
            thread = session.scalar(select(ChatThread).where(ChatThread.work_item_id == work_item_id))
            if thread is None:
                return None
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

    def apply_assistant_decision(
        self,
        thread_id: int,
        decision: ChatDecision,
        *,
        context: str = "",
    ) -> ChatThreadView:
        message = decision.message.strip()
        if not message:
            raise ChatError("Assistant message is required.")
        if decision.action not in CHAT_DECISION_ACTIONS:
            raise ChatError("Assistant returned an invalid action.")
        if decision.task_type not in TASK_TYPES:
            raise ChatError("Assistant returned an invalid task type.")
        now = utc_now()
        with self._session_factory() as session:
            row = _thread_row(session, thread_id)
            if row is None:
                raise ChatError("Chat thread not found.")
            thread, work_item, source_item = row
            transcript = _transcript_with_context(_transcript_for_thread(session, thread.id), context)
            task_type = decision.task_type
            work_item.task_type = task_type
            work_item.user_hint = transcript
            work_item.updated_at = now
            source_item.body = transcript
            source_item.updated_at = now
            if decision.action == "start_work":
                thread.status = CHAT_THREAD_IMPLEMENTATION_QUEUED
                _queue_chat_run(session, work_item.id, task_type, transcript, created_at=now)
            elif thread.status != CHAT_THREAD_IMPLEMENTATION_QUEUED:
                thread.status = CHAT_THREAD_ACTIVE
            thread.updated_at = now
            session.add(
                ChatMessage(
                    thread_id=thread.id,
                    role="assistant",
                    body=message,
                    created_at=now,
                )
            )
            session.commit()
            return _thread_view(session, thread.id)


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


def parse_chat_decision(output: str, *, fallback_task_type: str) -> ChatDecision:
    payload = _json_object_from_output(output)
    raw_action = str(payload.get("action", "")).strip()
    if raw_action not in CHAT_DECISION_ACTIONS:
        raise ChatError("Codex returned an invalid chat action.")
    raw_task_type = str(payload.get("task_type", "")).strip().lower()
    task_type = raw_task_type if raw_task_type in TASK_TYPES else fallback_task_type
    raw_message = payload.get("message", "")
    message = str(raw_message).strip()
    if not message:
        raise ChatError("Codex did not return an assistant message.")
    return ChatDecision(
        action=cast(ChatDecisionAction, raw_action),
        task_type=task_type,
        message=message,
    )


def _json_object_from_output(output: str) -> dict[str, object]:
    text = output.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ChatError("Codex did not return chat decision JSON.")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ChatError(f"Codex returned invalid chat decision JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ChatError("Codex returned an invalid chat decision.")
    return cast(dict[str, object], payload)


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
        raise ChatError("Add a source before starting a chat.")
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
        return "New Chat"
    if len(line) <= 72:
        return line
    return line[:69].rstrip() + "..."


def _thread_row(session: Session, thread_id: int) -> tuple[ChatThread, WorkItem, SourceItem] | None:
    row = session.execute(
        select(ChatThread, WorkItem, SourceItem)
        .join(WorkItem, ChatThread.work_item_id == WorkItem.id)
        .join(SourceItem, WorkItem.primary_source_item_id == SourceItem.id)
        .where(ChatThread.id == thread_id)
    ).one_or_none()
    if row is None:
        return None
    thread, work_item, source_item = row
    return thread, work_item, source_item


def _thread_view(session: Session, thread_id: int) -> ChatThreadView:
    row = _thread_row(session, thread_id)
    if row is None:
        raise ChatError("Chat thread not found.")
    thread, work_item, _source_item = row
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


def _transcript_for_thread(
    session: Session,
    thread_id: int,
) -> str:
    messages = list(
        session.scalars(
            select(ChatMessage)
            .where(ChatMessage.thread_id == thread_id, ChatMessage.role.in_(CHAT_MESSAGE_ROLES))
            .order_by(ChatMessage.id.asc())
        )
    )
    lines = [f"{message.role}: {message.body.strip()}" for message in messages if message.body.strip()]
    return "\n\n".join(lines)


def _queue_chat_run(
    session: Session,
    work_item_id: int,
    task_type: str,
    user_hint: str,
    *,
    created_at: str,
) -> None:
    existing_run = session.scalars(
        select(WorkItemRun)
        .where(WorkItemRun.work_item_id == work_item_id, WorkItemRun.trigger == "chat_implementation")
        .order_by(WorkItemRun.id.desc())
    ).first()
    if existing_run is not None:
        if existing_run.status == "queued":
            existing_run.task_type = task_type
            existing_run.user_hint = user_hint
            existing_run.updated_at = created_at
            return
        if existing_run.status == "running":
            return
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


def _transcript_with_context(transcript: str, context: str) -> str:
    cleaned_context = context.strip()
    if not cleaned_context:
        return transcript
    if not transcript.strip():
        return cleaned_context
    return f"{transcript.strip()}\n\n{cleaned_context}"


def _thread_transcript(thread: ChatThreadView) -> str:
    lines = [f"{message.role}: {message.body.strip()}" for message in thread.messages if message.body.strip()]
    return "\n\n".join(lines)


def _chat_decision_prompt(thread: ChatThreadView, message: str, *, context: str = "") -> str:
    transcript = _thread_transcript(thread)
    context_section = (
        f"""\nAdditional conversation context:\n{context.strip()}\n""" if context.strip() else ""
    )
    return f"""You are the assistant in Symphony DBCLI, a local dashboard that tracks chats as work items and can queue worker runs.

Decide the next action from the conversation. Return only a compact JSON object with this schema:
{{"action":"start_work"|"ask_followup","task_type":"code"|"research"|"operations","message":"assistant reply"}}

Use "start_work" when the user's request is concrete enough for a worker to begin. This includes coding changes, research, debugging, deployments, restarts, and VM operations.
Use "ask_followup" only when required target details are missing and starting work would likely waste effort.

Task type:
- code: repo, code, tests, docs, or PR-worthy implementation changes
- research: questions, investigation, explanation, or design exploration
- operations: deploy, install, restart, systemd, VM, or local server work

The assistant message should be one or two concise sentences. If starting work, say what you are starting. If asking a follow-up, ask the smallest necessary question.

Examples:
- User: Add a new syntax theme for litecli called gruvbox
  JSON: {{"action":"start_work","task_type":"code","message":"I will start implementing the gruvbox syntax theme for litecli."}}
- User: Why did the workflow transition fail?
  JSON: {{"action":"start_work","task_type":"research","message":"I will investigate the failed workflow transition and report what caused it."}}
- User: Redeploy the VM after pulling latest
  JSON: {{"action":"start_work","task_type":"operations","message":"I will start the pull and redeploy workflow for the VM."}}
- User: Make it better
  JSON: {{"action":"ask_followup","task_type":"research","message":"What specific behavior or screen should I focus on improving?"}}

Conversation:
{transcript}
{context_section}
Latest user message:
{message.strip()}
"""


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
