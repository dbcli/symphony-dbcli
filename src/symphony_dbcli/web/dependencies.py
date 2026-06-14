from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from symphony_dbcli.chats import ChatRepository
from symphony_dbcli.config import WorkflowConfig
from symphony_dbcli.db import SessionFactory
from symphony_dbcli.orchestrator import Orchestrator
from symphony_dbcli.runtime import RuntimeCycleResult, RuntimeStatus
from symphony_dbcli.sources import SourceRepository, SourceSyncClient
from symphony_dbcli.store import Store
from symphony_dbcli.web.runtime_views import RuntimeConfigView
from symphony_dbcli.work_items import WorkItemRepository

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
PACIFIC_TIME = ZoneInfo("America/Los_Angeles")
INLINE_MARKDOWN_RE = re.compile(r"(`[^`]+`|\[([^\]]+)\]\(([^)]+)\)|\*\*([^*]+)\*\*|https?://[^\s<)]+)")


def _format_ms(value: object) -> str:
    if value is None:
        return "-"
    ms = int(str(value))
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remaining = divmod(round(seconds), 60)
    return f"{minutes}m {remaining}s"


def _format_tokens(value: object) -> str:
    if value is None:
        return "-"
    tokens = int(str(value))
    if tokens < 1_000:
        return f"{tokens:,}"
    if tokens < 1_000_000:
        return f"{tokens / 1_000:.1f}K".replace(".0K", "K")
    return f"{tokens / 1_000_000:.1f}M".replace(".0M", "M")


def _timestamp_text(value: object) -> str | None:
    if value is None:
        return None
    raw_value = str(value).strip()
    if not raw_value:
        return None
    return raw_value


def _parse_timestamp(raw_value: str) -> datetime | None:
    try:
        timestamp = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp


def _format_localtime(value: object) -> str:
    raw_value = _timestamp_text(value)
    if raw_value is None:
        return "-"
    timestamp = _parse_timestamp(raw_value)
    if timestamp is None:
        return raw_value
    local_time = timestamp.astimezone(PACIFIC_TIME)
    hour = local_time.strftime("%I").lstrip("0") or "0"
    return f"{local_time:%Y-%m-%d} {hour}:{local_time:%M:%S} {local_time:%p} {local_time:%Z}"


def _format_compact_localtime(value: object) -> str:
    return _format_compact_localtime_value(value, include_seconds=False)


def _format_compact_localtime_seconds(value: object) -> str:
    return _format_compact_localtime_value(value, include_seconds=True)


def _format_compact_localtime_value(value: object, *, include_seconds: bool) -> str:
    raw_value = _timestamp_text(value)
    if raw_value is None:
        return "-"
    timestamp = _parse_timestamp(raw_value)
    if timestamp is None:
        return raw_value
    local_time = timestamp.astimezone(PACIFIC_TIME)
    hour = local_time.strftime("%I").lstrip("0") or "0"
    meridiem = local_time.strftime("%p").lower()
    if include_seconds:
        return f"{local_time:%b %d}, {hour}:{local_time:%M}:{local_time:%S}{meridiem}"
    return f"{local_time:%b %d}, {hour}:{local_time:%M}{meridiem}"


def _format_relative_time(value: object) -> str:
    raw_value = _timestamp_text(value)
    if raw_value is None:
        return "-"
    timestamp = _parse_timestamp(raw_value)
    if timestamp is None:
        return raw_value

    seconds = int((datetime.now(UTC) - timestamp.astimezone(UTC)).total_seconds())
    if seconds < 60:
        return "just now"

    units = (
        (365 * 24 * 60 * 60, "y"),
        (30 * 24 * 60 * 60, "mo"),
        (24 * 60 * 60, "d"),
        (60 * 60, "h"),
        (60, "m"),
    )
    for unit_seconds, suffix in units:
        if seconds >= unit_seconds:
            return f"~{max(1, seconds // unit_seconds)}{suffix} ago"
    return "just now"


def _numbered_lines(value: object) -> list[dict[str, object]]:
    lines = str(value).splitlines()
    if not lines:
        lines = [""]
    return [{"number": number, "text": line} for number, line in enumerate(lines, start=1)]


def _inline_markdown(value: object) -> Markup:
    text = str(value)
    cursor = 0
    parts: list[Markup] = []
    for match in INLINE_MARKDOWN_RE.finditer(text):
        if match.start() > cursor:
            parts.append(escape(text[cursor : match.start()]))
        token = match.group(0)
        code_text = token[1:-1] if token.startswith("`") and token.endswith("`") else None
        link_label = match.group(2)
        link_url = match.group(3)
        strong_text = match.group(4)
        if code_text is not None:
            parts.append(Markup("<code>") + escape(code_text) + Markup("</code>"))
        elif link_label and link_url and _safe_markdown_url(link_url):
            parts.append(
                Markup('<a href="')
                + escape(link_url)
                + Markup('" target="_blank" rel="noreferrer">')
                + escape(link_label)
                + Markup("</a>")
            )
        elif strong_text:
            parts.append(Markup("<strong>") + escape(strong_text) + Markup("</strong>"))
        elif _safe_markdown_url(token):
            escaped_url = escape(token)
            parts.append(
                Markup('<a href="')
                + escaped_url
                + Markup('" target="_blank" rel="noreferrer">')
                + escaped_url
                + Markup("</a>")
            )
        else:
            parts.append(escape(token))
        cursor = match.end()
    if cursor < len(text):
        parts.append(escape(text[cursor:]))
    return Markup("").join(parts)


def _safe_markdown_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https", "mailto"}


templates.env.filters["ms"] = _format_ms
templates.env.filters["tokens"] = _format_tokens
templates.env.filters["localtime"] = _format_localtime
templates.env.filters["compact_localtime"] = _format_compact_localtime
templates.env.filters["compact_localtime_seconds"] = _format_compact_localtime_seconds
templates.env.filters["relative_time"] = _format_relative_time
templates.env.filters["numbered_lines"] = _numbered_lines
templates.env.filters["inline_markdown"] = _inline_markdown


def _static_version() -> str:
    mtimes: list[int] = []
    for filename in ("web.css", "web.js"):
        path = STATIC_DIR / filename
        try:
            mtimes.append(int(path.stat().st_mtime))
        except FileNotFoundError:
            continue
    return str(max(mtimes, default=0))


class WebRuntime(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def run_cycle(self, *, trigger: str = "manual") -> RuntimeCycleResult: ...

    def status(self) -> RuntimeStatus: ...


@dataclass(frozen=True)
class WebAppState:
    config: WorkflowConfig
    store: Store
    session_factory: SessionFactory
    workflow_path: str
    workflow_version_id: int | None = None
    source_sync_client: SourceSyncClient | None = None
    runtime: WebRuntime | None = None


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    href: str


@dataclass(frozen=True)
class BreadcrumbItem:
    label: str
    href: str = ""


NAV_ITEMS = (
    NavItem("board", "Board", "/board"),
    NavItem("sources", "Sources", "/sources"),
    NavItem("work_items", "Work Items", "/work-items"),
    NavItem("operations", "Operations", "/operations"),
    NavItem("workers", "Workers", "/workers"),
    NavItem("workflow", "Workflow", "/workflow"),
    NavItem("ask", "Ask", "/ask"),
    NavItem("settings", "Settings", "/settings"),
)


def get_app_state(request: Request) -> WebAppState:
    return cast(WebAppState, request.app.state.symphony)


def source_repository(request: Request) -> SourceRepository:
    return SourceRepository(get_app_state(request).session_factory)


def work_item_repository(request: Request) -> WorkItemRepository:
    return WorkItemRepository(get_app_state(request).session_factory)


def chat_repository(request: Request) -> ChatRepository:
    return ChatRepository(get_app_state(request).session_factory)


def orchestrator_for_state(state: WebAppState) -> Orchestrator:
    return Orchestrator(state.config, state.store, state.workflow_version_id)


def page_context(request: Request, *, title: str, active: str) -> dict[str, object]:
    state = get_app_state(request)
    return {
        "request": request,
        "title": title,
        "active": active,
        "nav_items": NAV_ITEMS,
        "breadcrumbs": _default_breadcrumbs(title, active),
        "runtime": RuntimeConfigView.from_config(state.config),
        "workflow_path": state.workflow_path,
        "static_version": _static_version(),
    }


def _default_breadcrumbs(title: str, active: str) -> list[BreadcrumbItem]:
    section = next((item for item in NAV_ITEMS if item.key == active), None)
    if section is None or section.label == title:
        return []
    return [
        BreadcrumbItem(section.label, section.href),
        BreadcrumbItem(title),
    ]
