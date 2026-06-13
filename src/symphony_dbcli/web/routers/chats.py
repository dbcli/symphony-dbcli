from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request, status
from starlette.background import BackgroundTask
from starlette.responses import RedirectResponse, Response

from symphony_dbcli.chats import ChatDecision, ChatError, ChatRunView, ChatThreadView
from symphony_dbcli.store import CodexTokenUsage
from symphony_dbcli.web.dependencies import (
    BreadcrumbItem,
    chat_assistant,
    chat_repository,
    get_app_state,
    page_context,
    templates,
    work_item_repository,
)

router = APIRouter(tags=["chats"])
ACTIVE_RUN_STATUSES = frozenset({"queued", "running"})
SUMMARY_MARKER_RE = re.compile(r"\*\*\s*summary\s*\*\*|^summary:\s*", re.IGNORECASE | re.MULTILINE)
PROGRESS_SENTENCE_RE = re.compile(r"(?<=[.!?])\s*(?=(?:[`A-Z]|\*\*|I[’']))")
HEADING_RE = re.compile(r"^\*\*(?P<bold>[^*]{1,80})\*\*:?\s*(?P<bold_rest>.*)$")
NAMED_SECTION_RE = re.compile(
    r"^(?P<title>Summary|Checks run|Verification|Risks/blockers|Risks|Blockers|Notes|Result):\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
BULLET_RE = re.compile(r"^[-*]\s+(?P<body>.+)$")
ChatResultSectionKind = Literal["summary", "section"]


@dataclass(frozen=True)
class ChatProgressView:
    run: ChatRunView | None
    attempt: object | None
    token_usage: CodexTokenUsage | None
    final_response: ChatFinalResponseView | None
    poll: bool
    thread_poll: bool


@dataclass(frozen=True)
class ChatFinalResponseView:
    attempt_id: int
    title: str
    body: str
    result_type: str
    status: str
    updated_at: str
    formatted: ChatFormattedResult


@dataclass(frozen=True)
class ChatFormattedResult:
    updates: list[str]
    sections: list[ChatResultSection]
    has_summary: bool


@dataclass(frozen=True)
class ChatResultSection:
    title: str
    kind: ChatResultSectionKind
    paragraphs: list[str]
    bullets: list[str]


@router.post("/chats")
def create(
    request: Request,
    message: Annotated[str, Form()],
    source_id: Annotated[int | None, Form()] = None,
) -> Response:
    try:
        thread = chat_repository(request).start_thread(message, source_id=source_id)
    except ChatError as exc:
        context = page_context(request, title="Start Chat", active="chats")
        context["error"] = str(exc)
        context["message"] = message
        return templates.TemplateResponse(
            request=request,
            name="chats/new.html",
            context=context,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return _redirect_after_assistant_reply(request, thread, message)


@router.get("/chats/new")
def new(request: Request) -> Response:
    context = page_context(request, title="Start Chat", active="chats")
    context["error"] = ""
    context["message"] = ""
    return templates.TemplateResponse(
        request=request,
        name="chats/new.html",
        context=context,
    )


@router.get("/chats/{thread_id}/status")
def status_panel(request: Request, thread_id: int) -> Response:
    thread = chat_repository(request).get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return templates.TemplateResponse(
        request=request,
        name="chats/_status.html",
        context=_chat_status_context(request, thread),
    )


@router.get("/chats/{thread_id}/thread")
def thread_panel(request: Request, thread_id: int) -> Response:
    thread = chat_repository(request).get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return templates.TemplateResponse(
        request=request,
        name="chats/_thread.html",
        context=_chat_status_context(request, thread),
    )


@router.get("/chats/{thread_id}")
def detail(request: Request, thread_id: int, error: str = "") -> Response:
    thread = chat_repository(request).get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    context = page_context(request, title=thread.title, active="chats")
    context["breadcrumbs"] = [
        BreadcrumbItem("Board", f"/board/source/{thread.source_id}"),
        BreadcrumbItem(f"Chat #{thread.id}"),
    ]
    context["thread"] = thread
    context["progress"] = _chat_progress(request, thread)
    context["message"] = ""
    context["error"] = error
    return templates.TemplateResponse(
        request=request,
        name="chats/detail.html",
        context=context,
    )


@router.post("/chats/{thread_id}/messages")
def add_message(
    request: Request,
    thread_id: int,
    message: Annotated[str, Form()],
) -> Response:
    try:
        thread = chat_repository(request).add_message(thread_id, message)
    except ChatError:
        return RedirectResponse(f"/chats/{thread_id}?error=Message%20is%20required.", status_code=303)
    return _redirect_after_assistant_reply(request, thread, message)


@router.get("/work-items/{work_item_id}/chat")
def work_item_chat(request: Request, work_item_id: int) -> Response:
    thread = chat_repository(request).get_thread_for_work_item(work_item_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return RedirectResponse(f"/chats/{thread.id}", status_code=status.HTTP_303_SEE_OTHER)


def _schedule_runtime_cycle(request: Request, response: Response) -> Response:
    runtime = get_app_state(request).runtime
    if runtime is None:
        return response
    response.background = BackgroundTask(runtime.run_cycle, trigger="chat_implementation")
    return response


def _chat_status_context(request: Request, thread: ChatThreadView) -> dict[str, object]:
    return {
        "request": request,
        "thread": thread,
        "progress": _chat_progress(request, thread),
    }


def _chat_progress(request: Request, thread: ChatThreadView) -> ChatProgressView:
    run = thread.latest_run
    attempt = None
    token_usage = None
    final_response = None
    if run and run.attempt_id is not None:
        detail = get_app_state(request).store.attempt_detail(run.attempt_id)
        if detail is not None:
            attempt = detail["attempt"]
            token_usage = detail["token_usage"]
            final_response = _final_response_from_attempt_detail(detail)
    poll = thread.implementation_queued and (run is None or run.status in ACTIVE_RUN_STATUSES)
    thread_poll = poll and final_response is None
    return ChatProgressView(
        run=run,
        attempt=attempt,
        token_usage=token_usage,
        final_response=final_response,
        poll=poll,
        thread_poll=thread_poll,
    )


def _final_response_from_attempt_detail(detail: dict[str, Any]) -> ChatFinalResponseView | None:
    result = detail.get("result")
    attempt = detail.get("attempt")
    if attempt is None:
        return None
    attempt_id = int(attempt["id"])
    if result is not None:
        body = str(result["body"] or "").strip() or "The worker completed without a final message."
        return ChatFinalResponseView(
            attempt_id=attempt_id,
            title=str(result["title"] or "Worker result"),
            body=body,
            result_type=str(result["result_type"] or "worker_result"),
            status=str(result["status"] or attempt["status"]),
            updated_at=str(result["updated_at"] or attempt["updated_at"]),
            formatted=_format_final_response_body(body),
        )
    attempt_status = str(attempt["status"])
    if attempt_status in {"queued", "running"}:
        return None
    body = str(attempt["outcome"] or "").strip()
    if not body:
        errors = detail.get("errors") or []
        latest_error = errors[-1] if errors else None
        if latest_error is not None:
            message = str(latest_error["message"] or "").strip()
            log_excerpt = str(latest_error["log_excerpt"] or "").strip()
            body = message if message else f"The worker finished with status {attempt_status}."
            if log_excerpt:
                body = f"{body}\n\n{log_excerpt}"
        else:
            body = f"The worker finished with status {attempt_status}."
    return ChatFinalResponseView(
        attempt_id=attempt_id,
        title="Worker outcome",
        body=body,
        result_type="worker_outcome",
        status=attempt_status,
        updated_at=str(attempt["updated_at"]),
        formatted=_format_final_response_body(body),
    )


def _format_final_response_body(body: str) -> ChatFormattedResult:
    stripped = body.strip()
    if not stripped:
        return ChatFormattedResult(updates=[], sections=[], has_summary=False)
    summary_match = SUMMARY_MARKER_RE.search(stripped)
    if summary_match is None:
        return ChatFormattedResult(
            updates=[],
            sections=_result_sections(stripped, default_title="Result", first_kind="section"),
            has_summary=False,
        )
    progress_text = stripped[: summary_match.start()].strip()
    summary_text = stripped[summary_match.end() :].strip()
    return ChatFormattedResult(
        updates=_progress_updates(progress_text),
        sections=_result_sections(summary_text, default_title="Summary", first_kind="summary"),
        has_summary=True,
    )


def _progress_updates(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", stripped) if paragraph.strip()]
    if len(paragraphs) > 1:
        return paragraphs
    sentences = [sentence.strip() for sentence in PROGRESS_SENTENCE_RE.split(stripped) if sentence.strip()]
    if len(sentences) <= 1:
        return [stripped]
    updates: list[str] = []
    current: list[str] = []
    for sentence in sentences:
        current.append(sentence)
        current_text = " ".join(current)
        if len(current) >= 2 or len(current_text) >= 260:
            updates.append(current_text)
            current = []
    if current:
        updates.append(" ".join(current))
    return updates


def _result_sections(
    text: str,
    *,
    default_title: str,
    first_kind: ChatResultSectionKind,
) -> list[ChatResultSection]:
    current_title = default_title
    current_kind = first_kind
    paragraphs: list[str] = []
    bullets: list[str] = []
    paragraph_lines: list[str] = []
    sections: list[ChatResultSection] = []

    def flush_paragraph() -> None:
        if paragraph_lines:
            paragraphs.append(" ".join(paragraph_lines).strip())
            paragraph_lines.clear()

    def flush_section() -> None:
        flush_paragraph()
        if paragraphs or bullets:
            sections.append(
                ChatResultSection(
                    title=current_title,
                    kind=current_kind,
                    paragraphs=list(paragraphs),
                    bullets=list(bullets),
                )
            )
        paragraphs.clear()
        bullets.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        heading = _result_heading(line)
        if heading is not None:
            title, rest = heading
            flush_section()
            current_title = title
            current_kind = "summary" if title.lower() == "summary" else "section"
            if rest:
                paragraph_lines.append(rest)
            continue
        bullet = BULLET_RE.match(line)
        if bullet:
            flush_paragraph()
            bullets.append(bullet.group("body").strip())
            continue
        paragraph_lines.append(line)

    flush_section()
    if sections:
        return sections
    return [
        ChatResultSection(
            title=default_title,
            kind=first_kind,
            paragraphs=[text.strip()],
            bullets=[],
        )
    ]


def _result_heading(line: str) -> tuple[str, str] | None:
    bold_heading = HEADING_RE.match(line)
    if bold_heading is not None:
        title = bold_heading.group("bold").strip().rstrip(":")
        return title, bold_heading.group("bold_rest").strip()
    named_heading = NAMED_SECTION_RE.match(line)
    if named_heading is not None:
        return _section_title(named_heading.group("title")), named_heading.group("rest").strip()
    return None


def _section_title(value: str) -> str:
    if value.lower() == "risks/blockers":
        return "Risks/blockers"
    return value[:1].upper() + value[1:].lower()


def _redirect_after_assistant_reply(
    request: Request,
    thread: ChatThreadView,
    latest_message: str,
) -> Response:
    final_response = _latest_final_response(request, thread)
    context = _final_response_context(final_response)
    decision = _assistant_decision(request, thread, latest_message, context=context)
    try:
        updated_thread = chat_repository(request).apply_assistant_decision(
            thread.id,
            decision,
            context=context,
        )
    except ChatError as exc:
        return RedirectResponse(
            f"/chats/{thread.id}?error={_query_value(str(exc))}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    response: Response = RedirectResponse(
        f"/chats/{updated_thread.id}", status_code=status.HTTP_303_SEE_OTHER
    )
    if decision.action == "start_work":
        return _schedule_runtime_cycle(request, response)
    return response


def _latest_final_response(
    request: Request,
    thread: ChatThreadView,
) -> ChatFinalResponseView | None:
    progress = _chat_progress(request, thread)
    if progress.final_response is not None:
        return progress.final_response
    store = get_app_state(request).store
    for run in work_item_repository(request).list_runs(thread.work_item_id):
        if run.attempt_id is None:
            continue
        detail = store.attempt_detail(run.attempt_id)
        if detail is None:
            continue
        final_response = _final_response_from_attempt_detail(detail)
        if final_response is not None:
            return final_response
    return None


def _final_response_context(final_response: ChatFinalResponseView | None) -> str:
    if final_response is None:
        return ""
    return "\n".join(
        [
            (
                f"Assistant final result from attempt #{final_response.attempt_id} "
                f"({final_response.result_type}, {final_response.status}):"
            ),
            f"Title: {final_response.title}",
            final_response.body,
        ]
    )


def _assistant_decision(
    request: Request,
    thread: ChatThreadView,
    latest_message: str,
    *,
    context: str = "",
) -> ChatDecision:
    try:
        return chat_assistant(request).decide(thread, latest_message, context=context)
    except ChatError as exc:
        return ChatDecision(
            action="ask_followup",
            task_type=thread.task_type,
            message=f"I could not get a model-backed reply: {exc}",
        )


def _query_value(value: str) -> str:
    return quote(value, safe="")
