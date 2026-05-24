from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .actions import DEFAULT_ACTION_REGISTRY, PrimitiveSideEffect
from .config import WorkflowConfig, WorkflowError, parse_workflow
from .github import GitHubClient, GitHubIssue, PullRequest
from .primitive_executor import (
    PrimitiveContext,
    PrimitiveExecutionError,
    PrimitiveExecutor,
    WorkflowPrimitiveExecutor,
)
from .store import IssueSnapshot, Store
from .worker_prompt import build_worker_prompt as build_worker_prompt
from .workflow_definition import WorkflowTransitionConfig
from .workflow_engine import WorkflowEngine, WorkflowEngineError, WorkflowExecutionContext
from .worktree import WorktreeError, WorktreeManager


class OrchestratorError(RuntimeError):
    """Raised when orchestration cannot continue."""


class OrchestratorGitHubClient(Protocol):
    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]: ...

    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> None: ...

    def remove_label(self, repo: str, issue_number: int, label: str) -> None: ...

    def pull_request(self, repo: str, number: int) -> PullRequest: ...


@dataclass(frozen=True)
class WorktreeCleanupSummary:
    scanned: int = 0
    merged: int = 0
    cleaned: int = 0
    skipped: int = 0
    errors: int = 0


@dataclass(frozen=True)
class WorkflowActionRuntime:
    instance_id: int
    attempt_id: int | None
    action_run_id: int
    transition_name: str
    action_name: str
    trigger: str
    from_state: str
    to_state: str


@dataclass(frozen=True)
class WorkflowAdvanceResult:
    current_state: str
    ran_actions: int
    stop_reason: str


def load_and_record_workflow(
    store: Store,
    workflow_path: str | Path,
    profile: str | None = None,
) -> tuple[WorkflowConfig, int]:
    path = Path(workflow_path)
    content = path.read_text(encoding="utf-8")
    try:
        config = parse_workflow(content, profile=profile)
    except WorkflowError as exc:
        store.record_workflow_version(path, content, None, status="rejected", error=str(exc))
        raise
    version_id = store.record_workflow_version(path, content, config, status="accepted")
    return config, version_id


class WorkflowWatcher:
    def __init__(self, store: Store, workflow_path: str | Path, profile: str | None = None):
        self.store = store
        self.workflow_path = Path(workflow_path)
        self.profile = profile
        self._last_hash = ""
        self.current_config: WorkflowConfig | None = None
        self.current_version_id: int | None = None

    def reload_if_changed(self) -> tuple[WorkflowConfig, int, bool]:
        content = self.workflow_path.read_text(encoding="utf-8")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest == self._last_hash and self.current_config and self.current_version_id:
            return self.current_config, self.current_version_id, False
        config, version_id = load_and_record_workflow(
            self.store,
            self.workflow_path,
            profile=self.profile,
        )
        self._last_hash = digest
        self.current_config = config
        self.current_version_id = version_id
        return config, version_id, True


class Orchestrator:
    def __init__(
        self,
        config: WorkflowConfig,
        store: Store,
        workflow_version_id: int | None = None,
        *,
        github: OrchestratorGitHubClient | None = None,
        primitives: WorkflowPrimitiveExecutor | None = None,
    ):
        self.config = config
        self.store = store
        self.workflow_version_id = workflow_version_id
        self.github = github or GitHubClient(config.github)
        self.primitives = primitives or PrimitiveExecutor(config, store, github=self.github)

    def poll_once(self) -> int:
        return int(self.primitives.fetch_issues().output["synced"])

    def claim_next(self) -> int | None:
        eligible = self.store.eligible_issues(self.config.labels.todo, self.config.labels.blocked, limit=1)
        if not eligible:
            return None
        return self._claim_issue(eligible[0])

    def claim_available(self) -> int:
        claimed = 0
        counts = self.store.active_attempt_counts()
        if counts.get("*", 0) >= self.config.workers.max_global:
            return 0
        for issue in self.store.eligible_issues(
            self.config.labels.todo,
            self.config.labels.blocked,
            limit=self.config.workers.max_global,
        ):
            repo = str(issue["repo"])
            if counts.get("*", 0) >= self.config.workers.max_global:
                break
            if counts.get(repo, 0) >= self.config.workers.max_per_repo:
                continue
            self._claim_issue(issue)
            counts[repo] = counts.get(repo, 0) + 1
            counts["*"] = counts.get("*", 0) + 1
            claimed += 1
        return claimed

    def cleanup_merged_pull_request_worktrees(
        self,
        *,
        retry_errors: bool = False,
    ) -> WorktreeCleanupSummary:
        manager = WorktreeManager(self.config.workspace)
        summary = WorktreeCleanupSummary()
        for row in self.store.pending_pull_request_cleanups(retry_errors=retry_errors):
            summary = _cleanup_summary_with(summary, scanned=1)
            pull_request_id = int(row["id"])
            attempt_id = int(row["attempt_id"])
            pr = self.github.pull_request(str(row["repo"]), int(row["number"]))
            self.store.update_pull_request_status(
                pull_request_id,
                state=pr.state,
                merged_at=pr.merged_at,
            )
            if not pr.is_merged:
                summary = _cleanup_summary_with(summary, skipped=1)
                continue
            summary = _cleanup_summary_with(summary, merged=1)
            try:
                removal = manager.remove_worktree(
                    base_repo_path=str(row["base_repo_path"]),
                    worktree_path=str(row["worktree_path"]),
                )
            except WorktreeError as exc:
                self.store.mark_pull_request_worktree_cleanup_failed(pull_request_id, str(exc))
                self.store.record_error(
                    attempt_id,
                    phase="worktree",
                    error_type="WorktreeCleanupError",
                    message=str(exc),
                    recoverable=True,
                )
                summary = _cleanup_summary_with(summary, errors=1)
                continue
            self.store.mark_pull_request_worktree_cleaned(pull_request_id)
            self.store.record_timeline_event(
                attempt_id,
                phase="worktree",
                event_type="cleaned_after_pr_merge",
                message=removal.worktree_path,
                data={
                    "pull_request": pr.number,
                    "merged_at": pr.merged_at,
                    "removed": removal.removed,
                    "reason": removal.reason,
                },
            )
            summary = _cleanup_summary_with(summary, cleaned=1)
        return summary

    def _claim_issue(self, issue: sqlite3.Row) -> int:
        repo = str(issue["repo"])
        issue_number = int(issue["number"])
        task_type = str(issue["task_type"])
        attempt_id = self.store.create_attempt(
            repo=repo,
            issue_number=issue_number,
            task_type=task_type,
            workflow_version_id=self.workflow_version_id,
            status="queued",
        )
        instance_id = self.store.create_workflow_instance(
            repo=repo,
            issue_number=issue_number,
            task_type=task_type,
            workflow_version_id=self.workflow_version_id,
            initial_state=self._transition_from_state(
                "claim_issue", fallback=self.config.workflow.initial_state
            ),
            attempt_id=attempt_id,
        )
        try:
            self._advance_workflow_instance(
                instance_id,
                allowed_side_effects={"github_write"},
            )
        except Exception as exc:
            self._fail_workflow_instance(instance_id, str(exc))
            raise
        return attempt_id

    def run_issue(self, repo: str, issue_number: int, *, task_type: str | None = None) -> int:
        issue = self.store.issue_detail(repo, issue_number)
        if not issue:
            self.store.upsert_issue(
                IssueSnapshot(
                    repo=repo,
                    number=issue_number,
                    title=f"{repo}#{issue_number}",
                    url=f"https://github.com/{repo}/issues/{issue_number}",
                    state="open",
                    labels=[],
                    task_type=task_type or self.config.workers.default_task_type,
                )
            )
            issue = self.store.issue_detail(repo, issue_number)
        assert issue is not None
        issue_row = issue["issue"]
        resolved_task_type = task_type or issue_row["task_type"] or self.config.workers.default_task_type
        attempt_id = self.store.create_attempt(
            repo=repo,
            issue_number=issue_number,
            task_type=resolved_task_type,
            workflow_version_id=self.workflow_version_id,
            status="queued",
        )
        return self.run_attempt(attempt_id)

    def run_attempt(self, attempt_id: int) -> int:
        attempt = self.store.attempt_by_id(attempt_id)
        if not attempt:
            raise OrchestratorError(f"Attempt {attempt_id} does not exist.")
        worker_id = str(attempt["worker_id"] or f"worker-{attempt_id}-{uuid.uuid4().hex[:8]}")
        instance_id = self._workflow_instance_id_for_attempt(
            attempt,
            initial_state=self._transition_from_state("allocate_workspace", fallback="claimed"),
        )
        self.store.start_attempt(
            attempt_id,
            worker_id,
            pid=os.getpid(),
            max_runtime_seconds=self.config.workers.max_runtime_seconds,
        )
        heartbeat = WorkerHeartbeat(
            self.store,
            worker_id,
            interval_seconds=self.config.workers.heartbeat_interval_seconds,
        )
        heartbeat.start()
        self.store.record_timeline_event(attempt_id, phase="worker", event_type="started", message=worker_id)

        try:
            result = self._advance_workflow_instance(
                instance_id,
                allowed_side_effects={"workspace_write", "codex_worker", "github_write"},
            )
            if result.current_state in self.config.workflow.terminal_states:
                self.store.finish_attempt(attempt_id, result.current_state, result.current_state)
                return attempt_id
            if result.stop_reason == "human_gate":
                self.store.finish_attempt(attempt_id, "review", "needs_review")
                return attempt_id
            raise OrchestratorError(
                f"Workflow stopped in state '{result.current_state}' with reason '{result.stop_reason}'."
            )
        except Exception as exc:
            self._fail_workflow_instance(instance_id, str(exc))
            self.store.record_error(
                attempt_id,
                phase="worker",
                error_type=type(exc).__name__,
                message=str(exc),
                recoverable=False,
                log_excerpt=traceback.format_exc(limit=8),
            )
            self.store.finish_attempt(attempt_id, "failed", "failed")
            raise
        finally:
            heartbeat.stop()

    def run_human_gate(
        self,
        gate_id: int,
        *,
        input_data: dict[str, Any] | None = None,
        decided_by: str = "dashboard",
    ) -> WorkflowAdvanceResult:
        gate = self.store.workflow_gate_by_id(gate_id)
        if not gate:
            raise OrchestratorError(f"Workflow gate {gate_id} does not exist.")
        if str(gate["status"]) != "pending":
            raise OrchestratorError(f"Workflow gate {gate_id} is not pending.")
        instance = self.store.workflow_instance_by_id(int(gate["workflow_instance_id"]))
        if not instance:
            raise OrchestratorError(f"Workflow instance {gate['workflow_instance_id']} does not exist.")
        transition_name = str(gate["transition_name"])
        transition = self.config.workflow.transitions.get(transition_name)
        if not transition:
            raise OrchestratorError(f"Workflow transition {transition_name!r} is not configured.")
        if transition.trigger != "human":
            raise OrchestratorError(f"Workflow transition {transition_name!r} is not a human gate.")
        if str(instance["current_state"]) != transition.from_state:
            raise OrchestratorError(
                f"Workflow instance {instance['id']} is in state {instance['current_state']!r}, "
                f"not {transition.from_state!r}."
            )
        action_input = self._workflow_action_input(
            instance,
            transition=transition,
            extra={"gate_id": gate_id} | (input_data or {}),
        )
        action = self._start_workflow_action(
            int(instance["id"]),
            attempt_id=_optional_int(instance["attempt_id"]),
            transition_name=transition_name,
            input_data=action_input,
        )
        try:
            outcome = self.primitives.execute(
                self._primitive_context(
                    instance,
                    transition_name,
                    transition,
                    input_data=action_input,
                )
            )
        except PrimitiveExecutionError as exc:
            self._finish_workflow_action(action, "failed", output_data=exc.output, error=str(exc))
            raise OrchestratorError(str(exc)) from exc
        except Exception as exc:
            self._finish_workflow_action(action, "failed", error=str(exc))
            raise
        self._finish_workflow_action(action, "succeeded", output_data=outcome.output)
        self._transition_workflow_action(
            action,
            status=self._workflow_status_for_state(transition.to_state),
            data=outcome.output,
        )
        self._record_workflow_artifacts(action, transition, outcome.output)
        self.store.resolve_workflow_gate(gate_id, decision="approved", decided_by=decided_by)
        attempt_id = _optional_int(instance["attempt_id"])
        if attempt_id is not None and transition.to_state in self.config.workflow.terminal_states:
            self.store.finish_attempt(attempt_id, transition.to_state, transition.to_state)
        if attempt_id is not None and transition.to_state == "pr_ready":
            self.store.update_attempt_outcome(attempt_id, "draft_pr_created")
        return self._advance_workflow_instance(
            int(instance["id"]),
            allowed_side_effects={"github_read", "github_write", "workspace_write", "codex_worker"},
        )

    def _advance_workflow_instance(
        self,
        instance_id: int,
        *,
        allowed_side_effects: set[PrimitiveSideEffect] | None = None,
    ) -> WorkflowAdvanceResult:
        engine = WorkflowEngine(self.config.workflow)
        ran_actions = 0
        while True:
            instance = self.store.workflow_instance_by_id(instance_id)
            if not instance:
                raise OrchestratorError(f"Workflow instance {instance_id} does not exist.")
            current_state = str(instance["current_state"])
            if current_state in self.config.workflow.terminal_states:
                return WorkflowAdvanceResult(current_state, ran_actions, "terminal")
            context = self._workflow_context(instance)
            try:
                match = engine.single_transition(
                    from_state=current_state,
                    trigger="automatic",
                    context=context,
                )
            except WorkflowEngineError as exc:
                raise OrchestratorError(str(exc)) from exc
            if match is None:
                opened = self._open_human_gates(instance_id, current_state, context.task_type)
                return WorkflowAdvanceResult(
                    current_state,
                    ran_actions,
                    "human_gate" if opened else "no_transition",
                )
            primitive = DEFAULT_ACTION_REGISTRY.get(match.transition.action)
            if primitive is None:
                raise OrchestratorError(f"Unknown primitive: {match.transition.action}")
            if allowed_side_effects is not None and primitive.side_effect not in allowed_side_effects:
                return WorkflowAdvanceResult(current_state, ran_actions, "side_effect_not_allowed")
            action_input = self._workflow_action_input(instance, transition=match.transition)
            action = self._start_workflow_action(
                instance_id,
                attempt_id=_optional_int(instance["attempt_id"]),
                transition_name=match.name,
                input_data=action_input,
            )
            try:
                outcome = self.primitives.execute(
                    self._primitive_context(
                        instance,
                        match.name,
                        match.transition,
                        input_data=action_input,
                    )
                )
            except PrimitiveExecutionError as exc:
                self._finish_workflow_action(action, "failed", output_data=exc.output, error=str(exc))
                raise
            except Exception as exc:
                self._finish_workflow_action(action, "failed", error=str(exc))
                raise
            self._finish_workflow_action(action, "succeeded", output_data=outcome.output)
            self._transition_workflow_action(
                action,
                status=self._workflow_status_for_state(match.transition.to_state),
                data=outcome.output,
            )
            self._record_workflow_artifacts(action, match.transition, outcome.output)
            ran_actions += 1

    def _workflow_context(self, instance: sqlite3.Row) -> WorkflowExecutionContext:
        return WorkflowExecutionContext(
            task_type=str(instance["task_type"]),
            pull_request_is_merged=self._pull_request_is_merged(_optional_int(instance["attempt_id"])),
        )

    def _workflow_action_input(
        self,
        instance: sqlite3.Row,
        *,
        transition: WorkflowTransitionConfig | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "repo": str(instance["repo"]),
            "issue_number": int(instance["issue_number"]),
            "task_type": str(instance["task_type"]),
        }
        attempt_id = _optional_int(instance["attempt_id"])
        if attempt_id is not None:
            data["attempt_id"] = attempt_id
            attempt = self.store.attempt_by_id(attempt_id)
            if attempt:
                data.update(
                    {
                        "base_repo_path": str(attempt["base_repo_path"] or ""),
                        "worktree_path": str(attempt["worktree_path"] or ""),
                        "branch": str(attempt["branch"] or ""),
                        "commit_sha": str(attempt["commit_sha"] or ""),
                    }
                )
        if transition:
            for field, source in transition.inputs.items():
                data[field] = self._resolve_workflow_input(instance, source, data)
        data.update(extra or {})
        return data

    def _resolve_workflow_input(
        self,
        instance: sqlite3.Row,
        source: str,
        current_input: dict[str, Any],
    ) -> Any:
        context_values = self._workflow_context_values(instance)
        if source in current_input:
            return current_input[source]
        if source in context_values:
            return context_values[source]
        if source.startswith("artifact."):
            return self.store.workflow_artifact(int(instance["id"]), source.removeprefix("artifact."))
        if source.startswith("outputs."):
            transition_name, field = _split_transition_output(source.removeprefix("outputs."))
            return self.store.latest_workflow_action_output(int(instance["id"]), transition_name).get(field)
        raise OrchestratorError(f"Unsupported workflow input source: {source}")

    def _workflow_context_values(self, instance: sqlite3.Row) -> dict[str, Any]:
        values: dict[str, Any] = {
            "issue.repo": str(instance["repo"]),
            "issue.number": int(instance["issue_number"]),
            "task.type": str(instance["task_type"]),
        }
        attempt_id = _optional_int(instance["attempt_id"])
        if attempt_id is None:
            return values
        values["attempt.id"] = attempt_id
        attempt = self.store.attempt_by_id(attempt_id)
        if not attempt:
            return values
        values.update(
            {
                "attempt.base_repo_path": str(attempt["base_repo_path"] or ""),
                "attempt.worktree_path": str(attempt["worktree_path"] or ""),
                "attempt.branch": str(attempt["branch"] or ""),
                "attempt.commit_sha": str(attempt["commit_sha"] or ""),
            }
        )
        return values

    def _primitive_context(
        self,
        instance: sqlite3.Row,
        transition_name: str,
        transition: WorkflowTransitionConfig,
        input_data: dict[str, Any] | None = None,
    ) -> PrimitiveContext:
        repo = str(instance["repo"])
        issue_number = int(instance["issue_number"])
        attempt_id = _optional_int(instance["attempt_id"])
        attempt = self.store.attempt_by_id(attempt_id) if attempt_id is not None else None
        issue = self.store.issue_detail(repo, issue_number)
        issue_title = repo if not issue else str(issue["issue"]["title"])
        return PrimitiveContext(
            instance_id=int(instance["id"]),
            transition_name=transition_name,
            transition=transition,
            repo=repo,
            issue_number=issue_number,
            task_type=str(instance["task_type"]),
            issue_title=issue_title,
            attempt_id=attempt_id,
            base_repo_path="" if not attempt else str(attempt["base_repo_path"] or ""),
            worktree_path="" if not attempt else str(attempt["worktree_path"] or ""),
            branch="" if not attempt else str(attempt["branch"] or ""),
            commit_sha="" if not attempt else str(attempt["commit_sha"] or ""),
            input_data=input_data or {},
        )

    def _workflow_status_for_state(self, state: str) -> str:
        if state in self.config.workflow.terminal_states:
            return state
        return "active"

    def _pull_request_is_merged(self, attempt_id: int | None) -> bool:
        if attempt_id is None:
            return False
        return any(bool(row["merged_at"]) for row in self.store.pull_requests_for_attempt(attempt_id))

    def _record_workflow_artifacts(
        self,
        action: WorkflowActionRuntime | None,
        transition: WorkflowTransitionConfig,
        output: dict[str, Any],
    ) -> None:
        if not action:
            return
        artifacts = _workflow_artifacts_from_output(action.transition_name, transition, output)
        self.store.record_workflow_artifacts(
            action.instance_id,
            artifacts,
            workflow_version_id=self.workflow_version_id,
            action_run_id=action.action_run_id,
        )

    def _workflow_instance_id_for_attempt(self, attempt: sqlite3.Row, *, initial_state: str) -> int:
        attempt_id = int(attempt["id"])
        existing = self.store.workflow_instance_for_attempt(attempt_id)
        if existing:
            return int(existing["id"])
        return self.store.create_workflow_instance(
            repo=str(attempt["repo"]),
            issue_number=int(attempt["issue_number"]),
            task_type=str(attempt["task_type"]),
            workflow_version_id=self.workflow_version_id,
            initial_state=initial_state,
            attempt_id=attempt_id,
        )

    def _transition_from_state(self, transition_name: str, *, fallback: str) -> str:
        transition = self.config.workflow.transitions.get(transition_name)
        if not transition:
            return fallback
        return transition.from_state

    def _start_workflow_action(
        self,
        instance_id: int,
        *,
        attempt_id: int | None,
        transition_name: str,
        input_data: dict[str, Any] | None = None,
    ) -> WorkflowActionRuntime | None:
        transition = self.config.workflow.transitions.get(transition_name)
        if not transition:
            return None
        action_run_id = self.store.start_workflow_action_run(
            instance_id=instance_id,
            workflow_version_id=self.workflow_version_id,
            attempt_id=attempt_id,
            transition_name=transition_name,
            action_name=transition.action,
            input_data=input_data,
            idempotency_key=_workflow_action_idempotency_key(instance_id, transition_name),
        )
        return WorkflowActionRuntime(
            instance_id=instance_id,
            attempt_id=attempt_id,
            action_run_id=action_run_id,
            transition_name=transition_name,
            action_name=transition.action,
            trigger=transition.trigger,
            from_state=transition.from_state,
            to_state=transition.to_state,
        )

    def _finish_workflow_action(
        self,
        action: WorkflowActionRuntime | None,
        status: str,
        *,
        output_data: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        if not action:
            return
        self.store.finish_workflow_action_run(
            action.action_run_id,
            status=status,
            output_data=output_data,
            error=error,
        )

    def _transition_workflow_action(
        self,
        action: WorkflowActionRuntime | None,
        *,
        status: str = "active",
        data: dict[str, Any] | None = None,
    ) -> None:
        if not action:
            return
        try:
            self.store.transition_workflow_instance(
                action.instance_id,
                workflow_version_id=self.workflow_version_id,
                transition_name=action.transition_name,
                action_name=action.action_name,
                trigger=action.trigger,
                from_state=action.from_state,
                to_state=action.to_state,
                status=status,
                data=data,
            )
        except ValueError as exc:
            if action.attempt_id is not None:
                self.store.record_error(
                    action.attempt_id,
                    phase="workflow",
                    error_type="WorkflowRuntimeError",
                    message=str(exc),
                    recoverable=True,
                )
            raise OrchestratorError(str(exc)) from exc

    def _fail_workflow_instance(self, instance_id: int, message: str) -> None:
        try:
            self.store.fail_workflow_instance(
                instance_id,
                workflow_version_id=self.workflow_version_id,
                message=message,
            )
        except ValueError:
            return

    def _open_human_gates(self, instance_id: int, state: str, task_type: str) -> int:
        try:
            matches = WorkflowEngine(self.config.workflow).matching_transitions(
                from_state=state,
                trigger="human",
                context=WorkflowExecutionContext(task_type=task_type),
            )
        except WorkflowEngineError:
            return 0
        opened = 0
        for match in matches:
            if not match.transition.gate:
                continue
            self.store.open_workflow_gate(
                instance_id=instance_id,
                workflow_version_id=self.workflow_version_id,
                gate=match.transition.gate,
                transition_name=match.name,
                state=match.transition.from_state,
                prompt=match.transition.description,
            )
            opened += 1
        return opened


class WorkerHeartbeat:
    def __init__(self, store: Store, worker_id: str, *, interval_seconds: int):
        self.store = store
        self.worker_id = worker_id
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"heartbeat-{worker_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.store.heartbeat_worker(self.worker_id)


def _cleanup_summary_with(
    summary: WorktreeCleanupSummary,
    *,
    scanned: int = 0,
    merged: int = 0,
    cleaned: int = 0,
    skipped: int = 0,
    errors: int = 0,
) -> WorktreeCleanupSummary:
    return WorktreeCleanupSummary(
        scanned=summary.scanned + scanned,
        merged=summary.merged + merged,
        cleaned=summary.cleaned + cleaned,
        skipped=summary.skipped + skipped,
        errors=summary.errors + errors,
    )


def _workflow_action_idempotency_key(instance_id: int, transition_name: str) -> str:
    return f"workflow:{instance_id}:{transition_name}"


def _workflow_artifacts_from_output(
    transition_name: str,
    transition: WorkflowTransitionConfig,
    output: dict[str, Any],
) -> dict[str, Any]:
    artifacts = {f"{transition_name}.{field}": value for field, value in output.items()}
    for field, target in transition.outputs.items():
        if field not in output:
            continue
        artifact_name = target.removeprefix("artifact.") if target.startswith("artifact.") else target
        artifacts[artifact_name] = output[field]
    return artifacts


def _split_transition_output(source: str) -> tuple[str, str]:
    if "." not in source:
        raise OrchestratorError(f"Workflow output source must be outputs.<transition>.<field>: {source}")
    transition_name, field = source.split(".", 1)
    return transition_name, field


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value:
        return int(value)
    if isinstance(value, str):
        return None
    raise TypeError(f"Expected integer-compatible value, got {type(value).__name__}.")
