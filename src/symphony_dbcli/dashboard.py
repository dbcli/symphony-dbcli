from __future__ import annotations

import json
import mimetypes
import sqlite3
import urllib.parse
from dataclasses import dataclass
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from threading import Lock
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from .ask import AskAnswer, answer_with_links
from .config import WorkflowConfig, WorkflowError, default_config, render_workflow
from .orchestrator import Orchestrator, OrchestratorError, load_and_record_workflow
from .review_actions import DraftPullRequestContent, build_draft_pr_content
from .store import Store
from .supervisor import DispatchResult, WorkerSupervisor
from .workflow_edit import (
    CodexWorkflowEditModel,
    WorkflowEditProposal,
    parsed_config,
    propose_workflow_edit,
    propose_workflow_edit_with_model,
    validate_workflow_edit,
)
from .workflow_visualization import WorkflowFlowchartView


@dataclass(frozen=True)
class DashboardRuntime:
    profile: str
    dry_run: bool
    database_path: str
    start_queued_work_automatically: bool
    workspace_strategy: str
    workspace_root: str
    bare_repos_root: str
    branch_prefix: str
    base_branch: str
    retention_days: int

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
            workspace_strategy=config.workspace.strategy,
            workspace_root=config.workspace.root,
            bare_repos_root=config.workspace.bare_repos_root,
            branch_prefix=config.workspace.branch_prefix,
            base_branch=config.workspace.base_branch or "default branch",
            retention_days=config.workspace.retention_days,
        )


@dataclass(frozen=True)
class DashboardCycleResult:
    synced: int = 0
    advanced: int = 0
    claimed: int = 0
    started: int = 0
    crashed: int = 0
    timed_out: int = 0
    retried: int = 0
    cleaned_worktrees: int = 0
    skipped_worktrees: int = 0
    error: str = ""

    @property
    def succeeded(self) -> bool:
        return not self.error

    @classmethod
    def failed(cls, error: str) -> DashboardCycleResult:
        return cls(error=error)


@dataclass(frozen=True)
class WorkflowStateView:
    name: str
    description: str
    terminal: bool
    gate: str
    active_count: int


@dataclass(frozen=True)
class WorkflowTransitionView:
    name: str
    from_state: str
    to_state: str
    action: str
    trigger: str
    gate: str
    condition: str


@dataclass(frozen=True)
class WorkflowGateView:
    id: int
    gate: str
    transition_name: str
    repo: str
    issue_number: int
    task_type: str
    created_at: str


@dataclass(frozen=True)
class WorkflowGraphView:
    states: list[WorkflowStateView]
    transitions: list[WorkflowTransitionView]
    pending_gates: list[WorkflowGateView]

    @classmethod
    def from_config(cls, config: WorkflowConfig, store: Store) -> WorkflowGraphView:
        state_counts = store.workflow_state_counts()
        return cls(
            states=[
                WorkflowStateView(
                    name=name,
                    description=state.description,
                    terminal=state.terminal,
                    gate=state.gate,
                    active_count=state_counts.get(name, 0),
                )
                for name, state in config.workflow.states.items()
            ],
            transitions=[
                WorkflowTransitionView(
                    name=name,
                    from_state=transition.from_state,
                    to_state=transition.to_state,
                    action=transition.action,
                    trigger=transition.trigger,
                    gate=transition.gate,
                    condition=transition.condition,
                )
                for name, transition in config.workflow.transitions.items()
            ],
            pending_gates=[
                WorkflowGateView(
                    id=int(row["id"]),
                    gate=str(row["gate"]),
                    transition_name=str(row["transition_name"]),
                    repo=str(row["repo"]),
                    issue_number=int(row["issue_number"]),
                    task_type=str(row["task_type"]),
                    created_at=str(row["created_at"]),
                )
                for row in store.pending_workflow_gates(limit=20)
            ],
        )


class DashboardState:
    def __init__(self, config: WorkflowConfig, *, workflow_path: str = "WORKFLOW.md"):
        self._config = config
        self._workflow_path = workflow_path
        self._lock = Lock()

    def update_config(self, config: WorkflowConfig) -> None:
        with self._lock:
            self._config = config

    def config(self) -> WorkflowConfig:
        with self._lock:
            return self._config

    def workflow_path(self) -> str:
        with self._lock:
            return self._workflow_path

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


def render_index(
    store: Store,
    runtime: DashboardRuntime | None = None,
    config: WorkflowConfig | None = None,
    ask_question: str = "",
    cycle_result: DashboardCycleResult | None = None,
) -> str:
    cfg = config or default_config()
    ask_answer = answer_with_links(store, ask_question) if ask_question else None
    return (
        _templates()
        .get_template("index.html")
        .render(
            title="Symphony DBCLI",
            summary=store.dashboard_summary(),
            workflow_graph=WorkflowGraphView.from_config(cfg, store),
            runtime=runtime
            or DashboardRuntime.from_config(
                cfg,
                start_queued_work_automatically=store.start_queued_work_automatically(),
            ),
            ask_question=ask_question,
            ask_answer=ask_answer,
            cycle_result=cycle_result,
        )
    )


def render_ask_answer(store: Store, question: str) -> AskAnswer:
    return (
        answer_with_links(store, question)
        if question
        else AskAnswer("Ask a question about workers, issues, timing, turns, or errors.", [])
    )


def render_ask(store: Store, question: str) -> str:
    answer = render_ask_answer(store, question)
    return (
        _templates()
        .get_template("ask.html")
        .render(
            title="Ask Symphony",
            question=question,
            answer=answer,
        )
    )


def run_dashboard_cycle(store: Store, state: DashboardState) -> DashboardCycleResult:
    config = state.config()
    workflow_path = state.workflow_path()
    workflow_version_id = _latest_workflow_version_id(store)
    path = Path(workflow_path)
    if path.exists():
        config, workflow_version_id = load_and_record_workflow(
            store,
            workflow_path,
            profile=config.profile.active,
        )
        state.update_config(config)

    orchestrator = Orchestrator(config, store, workflow_version_id)
    supervisor = WorkerSupervisor(
        store,
        workflow_path=workflow_path,
        profile=config.profile.active,
    )
    reconciled = supervisor.reconcile(config, workflow_version_id)
    synced = orchestrator.poll_once()
    cleanup = orchestrator.cleanup_merged_pull_request_worktrees()
    advanced = orchestrator.advance_ready_workflow_instances(
        allowed_side_effects={"github_read", "github_write", "workspace_write"}
    )
    claimed = orchestrator.claim_available()
    dispatched = supervisor.start_queued(config)
    return _dashboard_cycle_result(
        synced=synced,
        advanced=advanced,
        claimed=claimed,
        cleanup=cleanup,
        reconciled=reconciled,
        dispatched=dispatched,
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
    pending_gates = store.pending_workflow_gates_for_attempt(attempt_id) if detail else []
    gate_transitions = {str(row["transition_name"]): row for row in pending_gates}
    return (
        _templates()
        .get_template("attempt.html")
        .render(
            title=f"Attempt {attempt_id}",
            attempt_id=attempt_id,
            detail=detail,
            pending_gates=pending_gates,
            create_draft_pr_gate=gate_transitions.get("create_draft_pr"),
            post_answer_gate=gate_transitions.get("post_answer"),
            return_to=f"/attempts/{attempt_id}",
            draft_pr_content=_draft_pr_content(detail),
        )
    )


def render_workflow_edit(
    *,
    proposal: WorkflowEditProposal,
    applied: bool = False,
) -> str:
    workflow_chart = _workflow_chart_for_proposal(proposal)
    return (
        _templates()
        .get_template("workflow_edit.html")
        .render(
            title="Edit Workflow",
            proposal=proposal,
            workflow_chart=workflow_chart,
            applied=applied,
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


def _draft_pr_content(detail: dict[str, Any] | None) -> DraftPullRequestContent | None:
    if not detail or detail["attempt"]["task_type"] != "code" or detail["pull_requests"]:
        return None
    result = detail["result"]
    if not result:
        return None
    return build_draft_pr_content(
        str(detail["attempt"]["repo"]),
        int(detail["attempt"]["issue_number"]),
        str(result["body"] or ""),
    )


def _workflow_chart_for_proposal(proposal: WorkflowEditProposal) -> WorkflowFlowchartView | None:
    if not proposal.valid:
        return None
    try:
        return WorkflowFlowchartView.from_definition(parsed_config(proposal.proposed_content).workflow)
    except WorkflowError:
        return None


def _handler_factory(store: Store, state: DashboardState) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.startswith("/static/"):
                self._send_static(parsed.path.removeprefix("/static/"))
                return
            if parsed.path == "/":
                params = urllib.parse.parse_qs(parsed.query)
                self._send_html(
                    render_index(
                        store,
                        state.runtime(
                            start_queued_work_automatically=store.start_queued_work_automatically()
                        ),
                        state.config(),
                        ask_question=params.get("q", [""])[0],
                    )
                )
                return
            if parsed.path == "/ask/answer":
                params = urllib.parse.parse_qs(parsed.query)
                answer = render_ask_answer(store, params.get("q", [""])[0])
                self._send_json(
                    {
                        "question": params.get("q", [""])[0],
                        "answer": answer.text,
                        "links": [{"label": link.label, "url": link.url} for link in answer.links],
                    }
                )
                return
            if parsed.path == "/ask":
                params = urllib.parse.parse_qs(parsed.query)
                self._send_html(render_ask(store, params.get("q", [""])[0]))
                return
            if parsed.path == "/workflow/edit":
                current = _workflow_content(state)
                self._send_html(
                    render_workflow_edit(
                        proposal=validate_workflow_edit(current, current, ""),
                    )
                )
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
            if len(parts) == 3 and parts[0] == "workflow-gates" and parts[2] == "run":
                try:
                    gate_id = int(parts[1])
                except ValueError:
                    self.send_error(404)
                    return
                self._run_workflow_gate(gate_id)
                return
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
            if parsed.path == "/workflow/run-cycle":
                self._run_dashboard_cycle()
                return
            if parsed.path == "/workflow/edit":
                self._workflow_edit()
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

        def _run_workflow_gate(self, gate_id: int) -> None:
            gate = store.workflow_gate_by_id(gate_id)
            if not gate or str(gate["status"]) != "pending":
                self.send_error(404)
                return
            params = self._read_form()
            input_data = _gate_input_data(params)
            try:
                Orchestrator(state.config(), store).run_human_gate(gate_id, input_data=input_data)
            except OrchestratorError as exc:
                self.send_error(400, str(exc))
                return
            except RuntimeError as exc:
                self.send_error(502, str(exc))
                return
            self._redirect(_safe_return_to(params, gate))

        def _workflow_edit(self) -> None:
            params = self._read_form()
            current = _workflow_content(state)
            action = params.get("action", ["preview"])[0]
            request = params.get("request", [""])[0]
            proposed_content = params.get("proposed_content", [""])[0]
            if action == "generate":
                workflow_dir = Path(state.workflow_path()).resolve().parent
                proposal = propose_workflow_edit_with_model(
                    current,
                    request,
                    model=CodexWorkflowEditModel(state.config(), workflow_dir),
                )
            else:
                proposal = (
                    validate_workflow_edit(current, proposed_content, request)
                    if proposed_content
                    else propose_workflow_edit(current, request)
                )
            if action == "apply" and proposal.valid:
                try:
                    config = parsed_config(proposal.proposed_content)
                except WorkflowError as exc:
                    proposal = validate_workflow_edit(current, proposal.proposed_content, f"{request}\n{exc}")
                    self._send_html(render_workflow_edit(proposal=proposal))
                    return
                Path(state.workflow_path()).write_text(proposal.proposed_content, encoding="utf-8")
                store.record_workflow_version(state.workflow_path(), proposal.proposed_content, config)
                state.update_config(config)
                self._send_html(render_workflow_edit(proposal=proposal, applied=True))
                return
            self._send_html(render_workflow_edit(proposal=proposal))

        def _run_dashboard_cycle(self) -> None:
            try:
                result = run_dashboard_cycle(store, state)
            except (WorkflowError, RuntimeError) as exc:
                result = DashboardCycleResult.failed(str(exc))
            self._send_html(
                render_index(
                    store,
                    state.runtime(start_queued_work_automatically=store.start_queued_work_automatically()),
                    state.config(),
                    cycle_result=result,
                )
            )

        def _create_draft_pr(self, attempt_id: int) -> None:
            params = self._read_form()
            gate = store.pending_workflow_gate_for_attempt(attempt_id, "create_draft_pr")
            if not gate:
                self.send_error(400, "No pending create_draft_pr workflow gate for this attempt.")
                return
            try:
                Orchestrator(state.config(), store).run_human_gate(
                    int(gate["id"]),
                    input_data={
                        "title": params.get("title", [""])[0],
                        "body": params.get("body", [""])[0],
                    },
                )
            except OrchestratorError as exc:
                self.send_error(400, str(exc))
                return
            except RuntimeError as exc:
                self.send_error(502, str(exc))
                return
            self._redirect(f"/attempts/{attempt_id}")

        def _post_comment(self, comment_id: int) -> None:
            params = self._read_form()
            comment = store.comment_by_id(comment_id)
            if not comment:
                self.send_error(404)
                return
            attempt_id = comment["attempt_id"]
            if attempt_id is None:
                self.send_error(400, "Comment is not associated with a workflow attempt.")
                return
            gate = store.pending_workflow_gate_for_attempt(int(attempt_id), "post_answer")
            if not gate:
                self.send_error(400, "No pending post_answer workflow gate for this attempt.")
                return
            try:
                Orchestrator(state.config(), store).run_human_gate(
                    int(gate["id"]),
                    input_data={
                        "comment_id": comment_id,
                        "body": params.get("body", [""])[0],
                    },
                )
            except OrchestratorError as exc:
                self.send_error(400, str(exc))
                return
            except RuntimeError as exc:
                self.send_error(502, str(exc))
                return
            self._redirect(f"/attempts/{attempt_id}")

        def _send_html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_json(self, data: dict[str, Any]) -> None:
            encoded = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
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


def _gate_input_data(params: dict[str, list[str]]) -> dict[str, Any]:
    return {key: values[0] for key, values in params.items() if key != "return_to" and values}


def _safe_return_to(params: dict[str, list[str]], gate: sqlite3.Row) -> str:
    requested = params.get("return_to", [""])[0]
    if requested.startswith("/") and not requested.startswith("//"):
        return requested
    attempt_id = gate["attempt_id"]
    if attempt_id is not None:
        return f"/attempts/{int(attempt_id)}"
    return _issue_path(str(gate["repo"]), int(gate["issue_number"]))


def _workflow_content(state: DashboardState) -> str:
    path = Path(state.workflow_path())
    if path.exists():
        return path.read_text(encoding="utf-8")
    return render_workflow(state.config())


def _latest_workflow_version_id(store: Store) -> int | None:
    workflow = store.latest_workflow_version()
    return int(workflow["id"]) if workflow else None


def _dashboard_cycle_result(
    *,
    synced: int,
    advanced: int,
    claimed: int,
    cleanup: Any,
    reconciled: DispatchResult,
    dispatched: DispatchResult,
) -> DashboardCycleResult:
    return DashboardCycleResult(
        synced=synced,
        advanced=advanced,
        claimed=claimed,
        started=dispatched.started,
        crashed=reconciled.crashed,
        timed_out=reconciled.timed_out,
        retried=reconciled.retried,
        cleaned_worktrees=int(cleanup.cleaned),
        skipped_worktrees=int(cleanup.skipped),
    )
