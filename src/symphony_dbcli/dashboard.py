from __future__ import annotations

import mimetypes
import sqlite3
import urllib.parse
from dataclasses import dataclass
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from threading import Lock
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from .ask import answer_question
from .config import WorkflowConfig, default_config
from .review_actions import ReviewActionError, ReviewActions
from .store import Store


@dataclass(frozen=True)
class DashboardRuntime:
    profile: str
    dry_run: bool
    database_path: str
    start_queued_work_automatically: bool

    @classmethod
    def from_config(
        cls,
        config: WorkflowConfig,
        *,
        start_queued_work_automatically: bool = True,
    ) -> DashboardRuntime:
        return cls(
            profile=config.profile.active,
            dry_run=config.policy.dry_run,
            database_path=config.database.path,
            start_queued_work_automatically=start_queued_work_automatically,
        )


class DashboardState:
    def __init__(self, config: WorkflowConfig):
        self._config = config
        self._lock = Lock()

    def update_config(self, config: WorkflowConfig) -> None:
        with self._lock:
            self._config = config

    def config(self) -> WorkflowConfig:
        with self._lock:
            return self._config

    def runtime(self, *, start_queued_work_automatically: bool = True) -> DashboardRuntime:
        with self._lock:
            return DashboardRuntime.from_config(
                self._config,
                start_queued_work_automatically=start_queued_work_automatically,
            )


def serve_dashboard(store: Store, host: str, port: int, state: DashboardState | None = None) -> None:
    handler = _handler_factory(store, state or DashboardState(default_config()))
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Dashboard listening on http://{host}:{port}")
    server.serve_forever()


def render_index(store: Store, runtime: DashboardRuntime | None = None) -> str:
    return (
        _templates()
        .get_template("index.html")
        .render(
            title="Symphony DBCLI",
            summary=store.dashboard_summary(),
            runtime=runtime
            or DashboardRuntime.from_config(
                default_config(),
                start_queued_work_automatically=store.start_queued_work_automatically(),
            ),
        )
    )


def render_ask(store: Store, question: str) -> str:
    answer = (
        answer_question(store, question)
        if question
        else "Ask a question about workers, issues, timing, turns, or errors."
    )
    return (
        _templates()
        .get_template("ask.html")
        .render(
            title="Ask Symphony",
            question=question,
            answer=answer,
        )
    )


def render_issue(store: Store, repo: str, number: int) -> str:
    detail = store.issue_detail(repo, number)
    return (
        _templates()
        .get_template("issue.html")
        .render(
            title=f"{repo}#{number}",
            repo=repo,
            number=number,
            detail=detail,
        )
    )


def render_attempt(store: Store, attempt_id: int) -> str:
    detail = store.attempt_detail(attempt_id)
    return (
        _templates()
        .get_template("attempt.html")
        .render(
            title=f"Attempt {attempt_id}",
            attempt_id=attempt_id,
            detail=detail,
        )
    )


def render_github_app_callback(code: str, state: str) -> str:
    return (
        _templates()
        .get_template("github_app_callback.html")
        .render(
            title="GitHub App Created",
            code=code,
            state=state,
        )
    )


def _handler_factory(store: Store, state: DashboardState) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.startswith("/static/"):
                self._send_static(parsed.path.removeprefix("/static/"))
                return
            if parsed.path == "/":
                self._send_html(
                    render_index(
                        store,
                        state.runtime(
                            start_queued_work_automatically=store.start_queued_work_automatically()
                        ),
                    )
                )
                return
            if parsed.path == "/ask":
                params = urllib.parse.parse_qs(parsed.query)
                self._send_html(render_ask(store, params.get("q", [""])[0]))
                return
            if parsed.path == "/github-app/callback":
                params = urllib.parse.parse_qs(parsed.query)
                self._send_html(
                    render_github_app_callback(
                        params.get("code", [""])[0],
                        params.get("state", [""])[0],
                    )
                )
                return
            if parsed.path.startswith("/issues/"):
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 4:
                    self._send_html(render_issue(store, f"{parts[1]}/{parts[2]}", int(parts[3])))
                    return
            if parsed.path.startswith("/attempts/"):
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 2:
                    self._send_html(render_attempt(store, int(parts[1])))
                    return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "attempts" and parts[2] == "follow-up-code":
                try:
                    source_attempt_id = int(parts[1])
                except ValueError:
                    self.send_error(404)
                    return
                self._create_code_follow_up(source_attempt_id)
                return
            if len(parts) == 3 and parts[0] == "attempts" and parts[2] == "draft-pr":
                try:
                    attempt_id = int(parts[1])
                except ValueError:
                    self.send_error(404)
                    return
                self._create_draft_pr(attempt_id)
                return
            if len(parts) == 3 and parts[0] == "comments" and parts[2] == "post":
                try:
                    comment_id = int(parts[1])
                except ValueError:
                    self.send_error(404)
                    return
                self._post_comment(comment_id)
                return
            if parsed.path == "/settings/start-queued-work-automatically":
                params = self._read_form()
                enabled = params.get("enabled", ["false"])[0] == "true"
                store.set_start_queued_work_automatically(enabled)
                self._redirect("/")
                return
            self.send_error(404)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _create_code_follow_up(self, source_attempt_id: int) -> None:
            try:
                workflow = store.latest_workflow_version()
                workflow_version_id = int(workflow["id"]) if workflow else None
                target_attempt_id = store.create_code_follow_up_attempt(
                    source_attempt_id, workflow_version_id
                )
            except (ValueError, sqlite3.Error) as exc:
                self.send_error(400, str(exc))
                return
            self._redirect(f"/attempts/{target_attempt_id}")

        def _create_draft_pr(self, attempt_id: int) -> None:
            try:
                ReviewActions(state.config(), store).create_draft_pr(attempt_id)
            except ReviewActionError as exc:
                self.send_error(400, str(exc))
                return
            except RuntimeError as exc:
                self.send_error(502, str(exc))
                return
            self._redirect(f"/attempts/{attempt_id}")

        def _post_comment(self, comment_id: int) -> None:
            params = self._read_form()
            try:
                posted = ReviewActions(state.config(), store).post_comment(
                    comment_id,
                    params.get("body", [""])[0],
                )
            except ReviewActionError as exc:
                self.send_error(400, str(exc))
                return
            except RuntimeError as exc:
                self.send_error(502, str(exc))
                return
            if posted.attempt_id is not None:
                self._redirect(f"/attempts/{posted.attempt_id}")
                return
            self._redirect(_issue_path(posted.repo, posted.issue_number))

        def _send_html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_static(self, relative_path: str) -> None:
            if "/" in relative_path or "\\" in relative_path or relative_path.startswith("."):
                self.send_error(404)
                return
            resource = files("symphony_dbcli").joinpath("static", relative_path)
            if not resource.is_file():
                self.send_error(404)
                return
            body = resource.read_bytes()
            content_type = mimetypes.guess_type(relative_path)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "public, max-age=300")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_form(self) -> dict[str, list[str]]:
            raw_length = self.headers.get("Content-Length", "0")
            length = int(raw_length) if raw_length.isdecimal() else 0
            body = self.rfile.read(length).decode("utf-8") if length else ""
            return urllib.parse.parse_qs(body)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return DashboardHandler


@lru_cache(maxsize=1)
def _templates() -> Environment:
    env = Environment(
        loader=PackageLoader("symphony_dbcli", "templates"),
        autoescape=select_autoescape(["html", "xml"]),
        undefined=StrictUndefined,
    )
    env.filters["ms"] = _format_ms
    env.filters["issue_path"] = _issue_path
    return env


def _format_ms(value: Any) -> str:
    if value is None:
        return "-"
    ms = int(value)
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remaining = divmod(round(seconds), 60)
    return f"{minutes}m {remaining}s"


def _issue_path(repo: str, number: int) -> str:
    return f"/issues/{repo}/{number}"
