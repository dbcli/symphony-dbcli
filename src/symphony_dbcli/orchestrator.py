from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from .actions import DEFAULT_ACTION_REGISTRY, PrimitiveSideEffect
from .config import WorkflowConfig, WorkflowError, parse_workflow
from .db import create_db_engine, create_session_factory
from .github import (
    GitHubCheckRun,
    GitHubCiFailureContext,
    GitHubCiStatus,
    GitHubClient,
    GitHubComment,
    GitHubIssue,
    GitHubPullRequestReviewComment,
    PullRequest,
    PullRequestMergeStatus,
)
from .models import create_model_tables
from .primitive_executor import (
    PrimitiveContext,
    PrimitiveExecutionError,
    PrimitiveExecutor,
    WorkflowPrimitiveExecutor,
)
from .store import ATTEMPT_ADJUSTMENT_RELATIONSHIP, IssueSnapshot, Store
from .work_items import WorkItemRepository, WorkItemRunClaim
from .worker_prompt import build_worker_prompt as build_worker_prompt
from .workflow_definition import WorkflowTransitionConfig
from .workflow_engine import (
    WorkflowEngine,
    WorkflowEngineError,
    WorkflowExecutionContext,
    WorkflowTransitionBatch,
    WorkflowTransitionMatch,
    transition_retry_available,
)
from .worktree import WorktreeError, WorktreeManager


class OrchestratorError(RuntimeError):
    """Raised when orchestration cannot continue."""


class OrchestratorGitHubClient(Protocol):
    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]: ...

    def issue(self, repo: str, issue_number: int) -> GitHubIssue: ...

    def list_comments(self, repo: str, issue_number: int) -> list[GitHubComment]: ...

    def list_pull_request_review_comments(
        self,
        repo: str,
        pull_request_number: int,
    ) -> list[GitHubPullRequestReviewComment]: ...

    def list_pull_requests(self, repo: str, *, state: str = "open") -> list[PullRequest]: ...

    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> None: ...

    def remove_label(self, repo: str, issue_number: int, label: str) -> None: ...

    def pull_request(self, repo: str, number: int) -> PullRequest: ...

    def merge_status(self, repo: str, pull_request_number: int) -> PullRequestMergeStatus: ...

    def ci_status(self, repo: str, pull_request_number: int) -> GitHubCiStatus: ...

    def ci_failure_context(
        self,
        repo: str,
        pull_request_number: int,
        failed_checks: list[GitHubCheckRun],
    ) -> GitHubCiFailureContext: ...


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
class HumanGateRuntime:
    gate_id: int
    instance: sqlite3.Row
    transition_name: str
    transition: WorkflowTransitionConfig
    action_input: dict[str, Any]


@dataclass(frozen=True)
class WorkflowAdvanceResult:
    current_state: str
    ran_actions: int
    stop_reason: str


@dataclass(frozen=True)
class WorkflowActionExecution:
    action: WorkflowActionRuntime | None
    match: WorkflowTransitionMatch
    output: dict[str, Any]
    error: str = ""


@dataclass(frozen=True)
class WorkflowParallelBatchResult:
    completed: bool
    ran_actions: int
    outputs: dict[str, dict[str, Any]]


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
        engine = create_db_engine(store.path)
        create_model_tables(engine)
        self.work_items = WorkItemRepository(create_session_factory(engine))

    def poll_once(self) -> int:
        if self.work_items.has_sources():
            return int(
                self.primitives.execute(
                    PrimitiveContext(
                        instance_id=0,
                        transition_name="source.sync_all",
                        transition=WorkflowTransitionConfig(
                            from_state="poll",
                            to_state="poll",
                            action="source.sync_all",
                        ),
                        repo="",
                        issue_number=0,
                        task_type="",
                        issue_title="",
                    )
                ).output["synced"]
            )
        return int(self.primitives.fetch_issues().output["synced"])

    def claim_next(self) -> int | None:
        work_item_attempt_id = self._claim_next_work_item()
        if work_item_attempt_id is not None:
            return work_item_attempt_id
        eligible = self.store.eligible_issues(self.config.labels.todo, self.config.labels.blocked, limit=1)
        if not eligible:
            return None
        return self._claim_issue(eligible[0])

    def claim_work_item_run(self, run_id: int) -> int | None:
        run = self.work_items.queued_run_by_id(run_id)
        if run is None:
            return None
        return self._claim_work_item_run(run)

    def claim_available(self) -> int:
        claimed = 0
        counts = self.store.active_attempt_counts()
        if counts.get("*", 0) >= self.config.workers.max_global:
            return 0
        while counts.get("*", 0) < self.config.workers.max_global:
            blocked_repos = {
                repo
                for repo, count in counts.items()
                if repo != "*" and count >= self.config.workers.max_per_repo
            }
            attempt_id = self._claim_next_work_item(blocked_repos=blocked_repos)
            if attempt_id is None:
                break
            attempt = self.store.attempt_by_id(attempt_id)
            repo = "" if attempt is None else str(attempt["repo"])
            counts[repo] = counts.get(repo, 0) + 1
            counts["*"] = counts.get("*", 0) + 1
            claimed += 1
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

    def advance_ready_workflow_instances(
        self,
        *,
        limit: int = 50,
        allowed_side_effects: set[PrimitiveSideEffect] | None = None,
    ) -> int:
        advanced = 0
        for instance in self.store.workflow_instances_ready_for_automation(limit=limit):
            try:
                result = self._advance_workflow_instance(
                    int(instance["id"]),
                    allowed_side_effects=allowed_side_effects,
                )
            except Exception as exc:
                self._fail_workflow_instance(int(instance["id"]), str(exc))
                raise
            advanced += result.ran_actions
            attempt_id = _optional_int(instance["attempt_id"])
            if attempt_id is not None and result.current_state in self.config.workflow.terminal_states:
                self.store.finish_attempt(attempt_id, result.current_state, result.current_state)
        return advanced

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
                allowed_side_effects={"github_write", "none"},
            )
        except Exception as exc:
            self._fail_workflow_instance(instance_id, str(exc))
            raise
        return attempt_id

    def _claim_next_work_item(self, *, blocked_repos: set[str] | None = None) -> int | None:
        run = self.work_items.next_queued_run(blocked_repos=blocked_repos)
        if run is None:
            return None
        return self._claim_work_item_run(run)

    def _claim_work_item_run(self, run: WorkItemRunClaim) -> int:
        self.store.upsert_issue(
            IssueSnapshot(
                repo=run.repo,
                number=run.issue_number,
                title=run.title,
                url=run.source_url,
                state="open",
                labels=[],
                task_type=run.task_type,
            )
        )
        attempt_id = self.store.create_attempt(
            repo=run.repo,
            issue_number=run.issue_number,
            task_type=run.task_type,
            workflow_version_id=self.workflow_version_id,
            status="queued",
            work_item_id=run.work_item_id,
            work_item_run_id=run.id,
        )
        instance_id = self.store.create_workflow_instance(
            repo=run.repo,
            issue_number=run.issue_number,
            task_type=run.task_type,
            workflow_version_id=self.workflow_version_id,
            initial_state=self._transition_from_state(
                "find_issue_pull_requests",
                fallback=self._transition_from_state("allocate_workspace", fallback="claimed"),
            ),
            attempt_id=attempt_id,
            work_item_id=run.work_item_id,
            work_item_run_id=run.id,
        )
        assigned = self.work_items.assign_run_attempt(
            run_id=run.id,
            attempt_id=attempt_id,
            workflow_instance_id=instance_id,
        )
        self.store.record_workflow_artifacts(
            instance_id,
            assigned.workflow_artifacts(),
            workflow_version_id=self.workflow_version_id,
        )
        if run.source_attempt_id is not None:
            self.store.record_attempt_link(
                source_attempt_id=run.source_attempt_id,
                target_attempt_id=attempt_id,
                relationship=ATTEMPT_ADJUSTMENT_RELATIONSHIP,
                metadata={"work_item_run_id": run.id},
            )
            self.store.record_timeline_event(
                attempt_id,
                phase="queue",
                event_type="created_from_adjustment",
                message=f"attempt {run.source_attempt_id}",
                data={"source_attempt_id": run.source_attempt_id, "work_item_run_id": run.id},
            )
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
            initial_state=self._transition_from_state(
                "find_issue_pull_requests",
                fallback=self._transition_from_state("allocate_workspace", fallback="claimed"),
            ),
        )
        self.store.start_attempt(
            attempt_id,
            worker_id,
            pid=os.getpid(),
            max_runtime_seconds=self.config.workers.max_runtime_seconds,
        )
        self.work_items.start_attempt_run(attempt_id)
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
                allowed_side_effects={
                    "github_read",
                    "workspace_write",
                    "codex_worker",
                    "github_write",
                    "none",
                },
            )
            self._finish_attempt_from_workflow_result(attempt_id, result)
            return attempt_id
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
            self.work_items.finish_attempt_run(attempt_id, status="failed", outcome="failed")
            raise
        finally:
            heartbeat.stop()

    def retry_failed_workflow_action(self, action_run_id: int) -> WorkflowAdvanceResult:
        action_run = self.store.workflow_action_run_by_id(action_run_id)
        if not action_run:
            raise OrchestratorError(f"Workflow action run {action_run_id} does not exist.")
        if str(action_run["status"]) != "failed":
            raise OrchestratorError(f"Workflow action run {action_run_id} is not failed.")
        attempt_id = _optional_int(action_run["attempt_id"])
        if attempt_id is None:
            raise OrchestratorError(f"Workflow action run {action_run_id} is not associated with an attempt.")
        attempt = self.store.attempt_by_id(attempt_id)
        if not attempt:
            raise OrchestratorError(f"Attempt {attempt_id} does not exist.")
        if str(attempt["status"]) != "failed":
            raise OrchestratorError(f"Attempt {attempt_id} is not failed.")
        instance_id = int(action_run["workflow_instance_id"])
        instance = self.store.workflow_instance_by_id(instance_id)
        if not instance:
            raise OrchestratorError(f"Workflow instance {instance_id} does not exist.")
        if str(instance["status"]) != "failed":
            raise OrchestratorError(f"Workflow instance {instance_id} is not failed.")
        transition_name = str(action_run["transition_name"])
        transition = self.config.workflow.transitions.get(transition_name)
        if not transition:
            raise OrchestratorError(f"Workflow transition {transition_name!r} is not configured.")
        if not _manual_retry_supported(transition):
            raise OrchestratorError(f"Workflow transition {transition_name!r} cannot be retried manually.")

        self.store.prepare_workflow_action_retry(
            instance_id=instance_id,
            attempt_id=attempt_id,
            state=transition.from_state,
            transition_name=transition_name,
        )
        self.work_items.start_attempt_run(attempt_id)
        self.store.record_timeline_event(
            attempt_id,
            phase="workflow",
            event_type="manual_retry_started",
            message=transition_name,
            data={"action_run_id": action_run_id},
        )

        try:
            reset_instance = self.store.workflow_instance_by_id(instance_id)
            if not reset_instance:
                raise OrchestratorError(f"Workflow instance {instance_id} does not exist.")
            match = WorkflowTransitionMatch(transition_name, transition)
            if not self._run_single_automatic_transition(reset_instance, match):
                raise OrchestratorError(
                    self._workflow_retry_limit_error(
                        instance_id,
                        f"Workflow action retry failed: {transition_name}.",
                        [transition_name],
                    )
                )
            result = self._advance_workflow_instance(
                instance_id,
                allowed_side_effects={"github_read", "github_write", "workspace_write", "none"},
            )
            self._finish_attempt_from_workflow_result(attempt_id, result)
            return result
        except Exception as exc:
            self._fail_workflow_instance(instance_id, str(exc))
            self.store.record_error(
                attempt_id,
                phase="workflow",
                error_type=type(exc).__name__,
                message=str(exc),
                recoverable=True,
                log_excerpt=traceback.format_exc(limit=8),
            )
            self.store.finish_attempt(attempt_id, "failed", "failed")
            self.work_items.finish_attempt_run(attempt_id, status="failed", outcome="failed")
            raise

    def run_human_gate(
        self,
        gate_id: int,
        *,
        input_data: dict[str, Any] | None = None,
        decided_by: str = "dashboard",
    ) -> WorkflowAdvanceResult:
        return self._run_human_gate(
            gate_id,
            input_data=input_data,
            decided_by=decided_by,
            expected_status="pending",
        )

    def start_human_gate(
        self,
        gate_id: int,
        *,
        input_data: dict[str, Any] | None = None,
        decided_by: str = "dashboard",
    ) -> None:
        self._human_gate_runtime(gate_id, input_data=input_data, expected_status="pending")
        if not self.store.start_workflow_gate(gate_id, decided_by=decided_by):
            raise OrchestratorError(f"Workflow gate {gate_id} is not pending.")

    def run_started_human_gate(
        self,
        gate_id: int,
        *,
        input_data: dict[str, Any] | None = None,
        decided_by: str = "dashboard",
    ) -> WorkflowAdvanceResult:
        try:
            return self._run_human_gate(
                gate_id,
                input_data=input_data,
                decided_by=decided_by,
                expected_status="running",
            )
        except Exception:
            self.store.reopen_workflow_gate(gate_id)
            raise

    def _human_gate_runtime(
        self,
        gate_id: int,
        *,
        input_data: dict[str, Any] | None = None,
        expected_status: str,
    ) -> HumanGateRuntime:
        gate = self.store.workflow_gate_by_id(gate_id)
        if not gate:
            raise OrchestratorError(f"Workflow gate {gate_id} does not exist.")
        if str(gate["status"]) != expected_status:
            raise OrchestratorError(f"Workflow gate {gate_id} is not {expected_status}.")
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
        context = self._workflow_context(instance)
        if not transition_retry_available(transition_name, transition, context):
            raise OrchestratorError(
                self._workflow_retry_limit_error(
                    int(instance["id"]),
                    f"Workflow transition retry limit exceeded: {transition_name}.",
                    [transition_name],
                )
            )
        action_input = self._workflow_action_input(
            instance,
            transition=transition,
            extra={"gate_id": gate_id} | (input_data or {}),
        )
        return HumanGateRuntime(
            gate_id=gate_id,
            instance=instance,
            transition_name=transition_name,
            transition=transition,
            action_input=action_input,
        )

    def _run_human_gate(
        self,
        gate_id: int,
        *,
        input_data: dict[str, Any] | None = None,
        decided_by: str,
        expected_status: str,
    ) -> WorkflowAdvanceResult:
        runtime = self._human_gate_runtime(
            gate_id,
            input_data=input_data,
            expected_status=expected_status,
        )
        instance = runtime.instance
        transition = runtime.transition
        transition_name = runtime.transition_name
        action_input = runtime.action_input
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
            allowed_side_effects={"github_read", "github_write", "workspace_write", "codex_worker", "none"},
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
                batch = engine.automatic_batch(
                    from_state=current_state,
                    context=context,
                )
            except WorkflowEngineError as exc:
                exhausted = engine.exhausted_transitions(
                    from_state=current_state,
                    trigger="automatic",
                    context=context,
                )
                raise OrchestratorError(
                    self._workflow_retry_limit_error(
                        int(instance["id"]),
                        str(exc),
                        [match.name for match in exhausted],
                    )
                ) from exc
            if batch is None:
                opened = self._open_human_gates(instance_id, current_state, context)
                return WorkflowAdvanceResult(
                    current_state,
                    ran_actions,
                    "human_gate" if opened else "no_transition",
                )
            if not self._batch_side_effects_allowed(batch, allowed_side_effects):
                return WorkflowAdvanceResult(current_state, ran_actions, "side_effect_not_allowed")
            if batch.is_parallel:
                result = self._run_parallel_batch(instance, batch)
                ran_actions += result.ran_actions
                if not result.completed:
                    continue
                self._transition_parallel_batch(instance, batch, result.outputs)
                continue
            match = batch.transitions[0]
            if self._run_single_automatic_transition(instance, match):
                ran_actions += 1

    def _batch_side_effects_allowed(
        self,
        batch: WorkflowTransitionBatch,
        allowed_side_effects: set[PrimitiveSideEffect] | None,
    ) -> bool:
        if allowed_side_effects is None:
            return True
        for match in batch.transitions:
            primitive = DEFAULT_ACTION_REGISTRY.get(match.transition.action)
            if primitive is None:
                raise OrchestratorError(f"Unknown primitive: {match.transition.action}")
            if primitive.side_effect not in allowed_side_effects:
                return False
        return True

    def _finish_attempt_from_workflow_result(
        self,
        attempt_id: int,
        result: WorkflowAdvanceResult,
    ) -> None:
        if result.current_state in self.config.workflow.terminal_states:
            self.store.finish_attempt(attempt_id, result.current_state, result.current_state)
            self.work_items.finish_attempt_run(
                attempt_id,
                status=result.current_state,
                outcome=result.current_state,
            )
            return
        if result.stop_reason == "human_gate":
            self.store.finish_attempt(attempt_id, "review", "needs_review")
            self.work_items.finish_attempt_run(
                attempt_id,
                status="review",
                outcome="needs_review",
            )
            return
        raise OrchestratorError(
            f"Workflow stopped in state '{result.current_state}' with reason '{result.stop_reason}'."
        )

    def _run_single_automatic_transition(
        self,
        instance: sqlite3.Row,
        match: WorkflowTransitionMatch,
    ) -> bool:
        checkpoint = self._succeeded_workflow_action(instance, match.name, match.transition)
        if checkpoint is not None:
            checkpoint_action, output = checkpoint
            self._transition_workflow_action(
                checkpoint_action,
                status=self._workflow_status_for_state(match.transition.to_state),
                data=output,
            )
            self._record_workflow_artifacts(checkpoint_action, match.transition, output)
            return False
        action_input = self._workflow_action_input(instance, transition=match.transition)
        action = self._start_workflow_action(
            int(instance["id"]),
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
            return False
        except Exception as exc:
            self._finish_workflow_action(action, "failed", error=str(exc))
            return False
        self._finish_workflow_action(action, "succeeded", output_data=outcome.output)
        self._transition_workflow_action(
            action,
            status=self._workflow_status_for_state(match.transition.to_state),
            data=outcome.output,
        )
        self._record_workflow_artifacts(action, match.transition, outcome.output)
        return True

    def _run_parallel_batch(
        self,
        instance: sqlite3.Row,
        batch: WorkflowTransitionBatch,
    ) -> WorkflowParallelBatchResult:
        outputs: dict[str, dict[str, Any]] = {}
        pending: list[tuple[WorkflowTransitionMatch, WorkflowActionRuntime | None, dict[str, Any]]] = []
        for match in batch.transitions:
            checkpoint = self._succeeded_workflow_action(
                instance,
                match.name,
                match.transition,
                consuming_transition_names=[match.name, batch.name],
            )
            if checkpoint is not None:
                checkpoint_action, output = checkpoint
                self._record_workflow_artifacts(checkpoint_action, match.transition, output)
                outputs[match.name] = output
                continue
            action_input = self._workflow_action_input(instance, transition=match.transition)
            action = self._start_workflow_action(
                int(instance["id"]),
                attempt_id=_optional_int(instance["attempt_id"]),
                transition_name=match.name,
                input_data=action_input,
            )
            pending.append((match, action, action_input))

        failures = 0
        ran_actions = 0
        with ThreadPoolExecutor(max_workers=max(len(pending), 1)) as executor:
            futures = {
                executor.submit(
                    self._execute_workflow_action, instance, match, action, action_input
                ): match.name
                for match, action, action_input in pending
            }
            for future in as_completed(futures):
                execution = future.result()
                if execution.error:
                    failures += 1
                    self._finish_workflow_action(
                        execution.action,
                        "failed",
                        output_data=execution.output,
                        error=execution.error,
                    )
                    continue
                ran_actions += 1
                outputs[execution.match.name] = execution.output
                self._finish_workflow_action(execution.action, "succeeded", output_data=execution.output)
                self._record_workflow_artifacts(
                    execution.action,
                    execution.match.transition,
                    execution.output,
                )
        return WorkflowParallelBatchResult(
            completed=failures == 0 and len(outputs) == len(batch.transitions),
            ran_actions=ran_actions,
            outputs=outputs,
        )

    def _execute_workflow_action(
        self,
        instance: sqlite3.Row,
        match: WorkflowTransitionMatch,
        action: WorkflowActionRuntime | None,
        action_input: dict[str, Any],
    ) -> WorkflowActionExecution:
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
            return WorkflowActionExecution(action, match, exc.output, str(exc))
        except Exception as exc:
            return WorkflowActionExecution(action, match, {}, str(exc))
        return WorkflowActionExecution(action, match, outcome.output)

    def _transition_parallel_batch(
        self,
        instance: sqlite3.Row,
        batch: WorkflowTransitionBatch,
        outputs: dict[str, dict[str, Any]],
    ) -> None:
        now_state = str(instance["current_state"])
        status = self._workflow_status_for_state(batch.to_state)
        try:
            self.store.transition_workflow_instance(
                int(instance["id"]),
                workflow_version_id=self.workflow_version_id,
                transition_name=batch.name,
                action_name="workflow.parallel",
                trigger="automatic",
                from_state=now_state,
                to_state=batch.to_state,
                status=status,
                data={"parallel_group": batch.name, "outputs": outputs},
            )
        except ValueError as exc:
            attempt_id = _optional_int(instance["attempt_id"])
            if attempt_id is not None:
                self.store.record_error(
                    attempt_id,
                    phase="workflow",
                    error_type="WorkflowRuntimeError",
                    message=str(exc),
                    recoverable=True,
                )
            raise OrchestratorError(str(exc)) from exc

    def _workflow_context(self, instance: sqlite3.Row) -> WorkflowExecutionContext:
        return WorkflowExecutionContext(
            task_type=str(instance["task_type"]),
            pull_request_is_merged=self._pull_request_is_merged(_optional_int(instance["attempt_id"])),
            artifacts=self.store.workflow_artifacts(int(instance["id"])),
            transition_failure_counts=self.store.workflow_action_failure_counts(int(instance["id"])),
        )

    def _workflow_retry_limit_error(
        self,
        instance_id: int,
        message: str,
        transition_names: list[str],
    ) -> str:
        failure_messages: list[str] = []
        for transition_name in transition_names:
            for row in self.store.failed_workflow_action_runs(instance_id, transition_name):
                error = _compact_error(str(row["error"] or ""))
                if error:
                    retry_count = int(row["retry_count"])
                    failure_messages.append(f"{transition_name} retry {retry_count}: {error}")
        if not failure_messages:
            return message
        return f"{message} Recorded transition failures: {' | '.join(failure_messages)}"

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
        work_item_values = self._work_item_context_values(instance)
        values.update(work_item_values)
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
        work_item_values = self._work_item_context_values(instance)
        return PrimitiveContext(
            instance_id=int(instance["id"]),
            transition_name=transition_name,
            transition=transition,
            repo=repo,
            issue_number=issue_number,
            task_type=str(instance["task_type"]),
            issue_title=issue_title,
            attempt_id=attempt_id,
            source_id=_optional_int(work_item_values.get("source.id")),
            source_item_id=_optional_int(work_item_values.get("source_item.id")),
            source_item_kind=str(work_item_values.get("source_item.kind") or ""),
            source_item_number=_optional_int(work_item_values.get("source_item.number")),
            source_item_url=str(work_item_values.get("source_item.url") or ""),
            work_item_id=_optional_int(instance["work_item_id"]),
            active_pr_source_item_id=_optional_int(work_item_values.get("pull_request.source_item_id")),
            user_hint=str(work_item_values.get("work_item.user_hint") or ""),
            rerun_reasons=_string_list(work_item_values.get("work_item.rerun_reasons")),
            base_repo_path="" if not attempt else str(attempt["base_repo_path"] or ""),
            worktree_path="" if not attempt else str(attempt["worktree_path"] or ""),
            branch="" if not attempt else str(attempt["branch"] or ""),
            commit_sha="" if not attempt else str(attempt["commit_sha"] or ""),
            input_data=input_data or {},
        )

    def _work_item_context_values(self, instance: sqlite3.Row) -> dict[str, Any]:
        work_item_id = _optional_int(instance["work_item_id"])
        if work_item_id is None:
            return {}
        artifacts = self.store.workflow_artifacts(int(instance["id"]))
        return {
            key: value
            for key, value in artifacts.items()
            if key.startswith(("work_item.", "source.", "source_item.", "linked_issue.", "pull_request."))
        }

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
        retry_count = self.store.workflow_action_failure_counts(instance_id).get(transition_name, 0)
        action_run_id = self.store.start_workflow_action_run(
            instance_id=instance_id,
            workflow_version_id=self.workflow_version_id,
            attempt_id=attempt_id,
            transition_name=transition_name,
            action_name=transition.action,
            input_data=input_data,
            idempotency_key=_workflow_action_idempotency_key(instance_id, transition_name),
            retry_count=retry_count,
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

    def _succeeded_workflow_action(
        self,
        instance: sqlite3.Row,
        transition_name: str,
        transition: WorkflowTransitionConfig,
        *,
        consuming_transition_names: list[str] | None = None,
    ) -> tuple[WorkflowActionRuntime, dict[str, Any]] | None:
        row = self.store.latest_succeeded_workflow_action_run(int(instance["id"]), transition_name)
        if not row:
            return None
        completed_at = str(row["completed_at"] or "")
        transition_names = consuming_transition_names or [transition_name]
        if self.store.workflow_transition_exists_after(
            int(instance["id"]),
            transition_names,
            completed_at,
        ):
            return None
        return (
            WorkflowActionRuntime(
                instance_id=int(instance["id"]),
                attempt_id=_optional_int(row["attempt_id"]),
                action_run_id=int(row["id"]),
                transition_name=transition_name,
                action_name=str(row["action_name"] or transition.action),
                trigger=transition.trigger,
                from_state=transition.from_state,
                to_state=transition.to_state,
            ),
            cast(dict[str, Any], json.loads(str(row["output_json"]))),
        )

    def _fail_workflow_instance(self, instance_id: int, message: str) -> None:
        try:
            self.store.fail_workflow_instance(
                instance_id,
                workflow_version_id=self.workflow_version_id,
                message=message,
            )
        except ValueError:
            return

    def _open_human_gates(
        self,
        instance_id: int,
        state: str,
        context: WorkflowExecutionContext,
    ) -> int:
        try:
            matches = WorkflowEngine(self.config.workflow).matching_transitions(
                from_state=state,
                trigger="human",
                context=context,
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


def _manual_retry_supported(transition: WorkflowTransitionConfig) -> bool:
    if transition.trigger != "automatic":
        return False
    primitive = DEFAULT_ACTION_REGISTRY.get(transition.action)
    if primitive is None:
        return False
    return primitive.side_effect in {"github_read", "github_write", "workspace_write", "none"}


def _compact_error(error: str) -> str:
    return " ".join(error.split())


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


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    if isinstance(value, str) and value:
        return [value]
    return []
