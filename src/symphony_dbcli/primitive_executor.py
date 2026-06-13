from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, cast

from .chats import ChatRepository
from .config import WorkflowConfig
from .db import create_db_engine, create_session_factory
from .github import (
    GitHubCheckRun,
    GitHubCiFailureContext,
    GitHubCiStatus,
    GitHubClient,
    GitHubComment,
    GitHubError,
    GitHubIssue,
    GitHubPullRequestReviewComment,
    PullRequest,
    PullRequestMergeStatus,
    is_actionable_ci_annotation,
)
from .models import create_model_tables
from .review_actions import (
    GitHubReviewClient,
    PullRequestSourceContext,
    ReviewActionError,
    ReviewActions,
    body_links_issue,
    body_links_source_item,
    issue_link_marker,
    pull_request_source_marker,
)
from .runner import CodexRunner
from .sources import SourceRepository, SourceSyncService
from .store import Store
from .work_items import (
    CONVERSATION_KIND,
    WorkItemActivation,
    WorkItemMove,
    WorkItemRepository,
    WorkItemView,
)
from .worker_prompt import (
    build_pull_request_prompt,
    build_worker_prompt,
    format_follow_up_context,
    result_title,
    result_type,
)
from .workflow_definition import WorkflowTransitionConfig
from .worktree import WorktreeManager


class PrimitiveGitHubClient(Protocol):
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
class PrimitiveContext:
    instance_id: int
    transition_name: str
    transition: WorkflowTransitionConfig
    repo: str
    issue_number: int
    task_type: str
    issue_title: str
    attempt_id: int | None = None
    source_id: int | None = None
    source_item_id: int | None = None
    source_item_kind: str = ""
    source_item_number: int | None = None
    source_item_url: str = ""
    work_item_id: int | None = None
    active_pr_source_item_id: int | None = None
    user_hint: str = ""
    rerun_reasons: list[str] = field(default_factory=list)
    base_repo_path: str = ""
    worktree_path: str = ""
    branch: str = ""
    commit_sha: str = ""
    input_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PrimitiveOutcome:
    output: dict[str, Any]


class WorkflowPrimitiveExecutor(Protocol):
    def fetch_issues(self) -> PrimitiveOutcome: ...

    def execute(self, context: PrimitiveContext) -> PrimitiveOutcome: ...


class PrimitiveExecutionError(RuntimeError):
    def __init__(self, message: str, *, output: dict[str, Any] | None = None):
        super().__init__(message)
        self.output = output or {}


class PrimitiveExecutor:
    def __init__(
        self,
        config: WorkflowConfig,
        store: Store,
        *,
        github: PrimitiveGitHubClient | None = None,
        review_actions: ReviewActions | None = None,
    ):
        self.config = config
        self.store = store
        self.github = github or GitHubClient(config.github)
        engine = create_db_engine(store.path)
        create_model_tables(engine)
        session_factory = create_session_factory(engine)
        self.sources = SourceRepository(session_factory)
        self.work_items = WorkItemRepository(session_factory)
        self.chats = ChatRepository(session_factory)
        self.review_actions = review_actions or ReviewActions(
            config,
            store,
            github=cast(GitHubReviewClient, self.github),
        )

    def fetch_issues(self) -> PrimitiveOutcome:
        synced = 0
        for repo in self.config.github.repos:
            self.store.upsert_repo(repo)
            issues = self.github.list_issues(repo, labels=[self.config.labels.todo])
            for issue in issues:
                self.store.upsert_issue(
                    issue.snapshot(self.config.labels, self.config.workers.default_task_type)
                )
                synced += 1
        return PrimitiveOutcome({"synced": synced})

    def execute(self, context: PrimitiveContext) -> PrimitiveOutcome:
        if context.transition.action == "workflow.noop":
            return self._noop(context)
        if context.transition.action == "github.fetch_issue":
            return self._fetch_issue(context)
        if context.transition.action == "github.fetch_comments":
            return self._fetch_comments(context)
        if context.transition.action == "github.apply_labels":
            return self._apply_labels(context)
        if context.transition.action == "github.find_issue_pull_requests":
            return self._find_issue_pull_requests(context)
        if context.transition.action == "github.fetch_pull_request":
            return self._fetch_pull_request(context)
        if context.transition.action == "github.fetch_ci_status":
            return self._fetch_ci_status(context)
        if context.transition.action == "github.fetch_ci_failure_context":
            return self._fetch_ci_failure_context(context)
        if context.transition.action == "github.fetch_pr_review_comments":
            return self._fetch_pr_review_comments(context)
        if context.transition.action == "github.detect_merge_conflicts":
            return self._detect_merge_conflicts(context)
        if context.transition.action == "workspace.allocate":
            return self._allocate_workspace(context)
        if context.transition.action == "workspace.run_setup":
            return self._run_setup(context)
        if context.transition.action == "workspace.record_changes":
            return self._record_workspace_changes(context)
        if context.transition.action == "workspace.cleanup_after_merge":
            return self._cleanup_after_merge(context)
        if context.transition.action in {
            "codex.research_issue",
            "codex.fix_issue",
            "codex.address_pr_comments",
            "codex.fix_ci_failures",
            "codex.address_pr_feedback",
            "codex.operations_task",
        }:
            return self._run_codex(context)
        if context.transition.action == "codex.create_draft_pr":
            return self._create_draft_pr_with_codex(context)
        if context.transition.action == "github.create_draft_pr":
            return self._create_draft_pr(context)
        if context.transition.action == "github.push_pr_update":
            return self._push_pr_update(context)
        if context.transition.action == "github.post_issue_comment":
            return self._post_issue_comment(context)
        if context.transition.action == "source.sync":
            return self._source_sync(context)
        if context.transition.action == "source.sync_all":
            return self._source_sync_all()
        if context.transition.action == "work_item.activate":
            return self._work_item_activate(context)
        if context.transition.action == "work_item.move":
            return self._work_item_move(context)
        if context.transition.action == "work_item.link_source_item":
            return self._work_item_link_source_item(context)
        if context.transition.action == "work_item.select_active_pr":
            return self._work_item_select_active_pr(context)
        raise PrimitiveExecutionError(f"Primitive {context.transition.action} is not implemented.")

    def _noop(self, context: PrimitiveContext) -> PrimitiveOutcome:
        return PrimitiveOutcome({"message": context.transition.description or context.transition_name})

    def _fetch_issue(self, context: PrimitiveContext) -> PrimitiveOutcome:
        issue = self.github.issue(context.repo, context.issue_number)
        snapshot = issue.snapshot(self.config.labels, self.config.workers.default_task_type)
        self.store.upsert_issue(snapshot)
        return PrimitiveOutcome({"issue": asdict(snapshot)})

    def _fetch_comments(self, context: PrimitiveContext) -> PrimitiveOutcome:
        comments = self.github.list_comments(context.repo, context.issue_number)
        return PrimitiveOutcome({"comments": [asdict(comment) for comment in comments]})

    def _apply_labels(self, context: PrimitiveContext) -> PrimitiveOutcome:
        add, remove = self._label_changes(context.transition.to_state)
        if not self.config.policy.dry_run:
            if add:
                self.github.add_labels(context.repo, context.issue_number, add)
            for label in remove:
                try:
                    self.github.remove_label(context.repo, context.issue_number, label)
                except GitHubError:
                    continue
        if context.attempt_id is not None and context.transition.to_state == "claimed":
            self.store.record_timeline_event(
                context.attempt_id,
                phase="queue",
                event_type="claimed",
                message=f"{context.repo}#{context.issue_number}",
            )
        return PrimitiveOutcome(
            {
                "dry_run": self.config.policy.dry_run,
                "labels_added": add,
                "labels_removed": remove,
            }
        )

    def _find_issue_pull_requests(self, context: PrimitiveContext) -> PrimitiveOutcome:
        attempt_id = _required_attempt_id(context)
        marker = issue_link_marker(context.repo, context.issue_number)
        by_number: dict[int, PullRequest] = {}

        for row in self.store.issue_pull_request_links(context.repo, context.issue_number):
            try:
                pull_request = self.github.pull_request(context.repo, int(row["pull_request_number"]))
            except GitHubError:
                continue
            by_number[pull_request.number] = pull_request

        for pull_request in self.github.list_pull_requests(context.repo, state="open"):
            if pull_request.head_repo and pull_request.head_repo != context.repo:
                continue
            if not body_links_issue(pull_request.body, context.repo, context.issue_number):
                continue
            by_number[pull_request.number] = pull_request
            self.store.record_issue_pull_request_link(
                repo=context.repo,
                issue_number=context.issue_number,
                pull_request_number=pull_request.number,
                pull_request_url=pull_request.url,
                pull_request_title=pull_request.title,
                state=pull_request.state,
                link_source="description_marker",
                marker=marker,
            )

        pull_requests = sorted(by_number.values(), key=lambda item: item.number, reverse=True)
        if pull_requests:
            primary = pull_requests[0]
            self.store.record_pr(
                attempt_id,
                repo=context.repo,
                number=primary.number,
                url=primary.url,
                title=primary.title,
                state=primary.state,
                merged_at=primary.merged_at,
            )
        else:
            primary = None
        return PrimitiveOutcome(
            {
                "has_pull_request": primary is not None,
                "pull_request_count": len(pull_requests),
                "pull_requests": [_pull_request_output(item) for item in pull_requests],
                "pull_request_number": 0 if primary is None else primary.number,
                "pull_request_url": "" if primary is None else primary.url,
                "pull_request_title": "" if primary is None else primary.title,
                "pull_request_head_ref": "" if primary is None else primary.head_ref,
                "pull_request_head_sha": "" if primary is None else primary.head_sha,
                "pull_request_source_ref": "" if primary is None else _source_ref(primary),
            }
        )

    def _allocate_workspace(self, context: PrimitiveContext) -> PrimitiveOutcome:
        attempt_id = _required_attempt_id(context)
        allocation = WorktreeManager(self.config.workspace).allocate(
            context.repo,
            context.issue_number,
            attempt_id,
            branch_name=str(context.input_data.get("branch") or ""),
            source_ref=str(context.input_data.get("source_ref") or ""),
        )
        self.store.update_attempt_workspace(
            attempt_id,
            base_repo_path=allocation.base_repo_path,
            worktree_path=allocation.worktree_path,
            branch=allocation.branch,
            commit_sha=allocation.commit_sha,
        )
        self.store.record_timeline_event(
            attempt_id,
            phase="worktree",
            event_type="allocated",
            message=allocation.worktree_path,
            data={"branch": allocation.branch, "commit_sha": allocation.commit_sha},
        )
        return PrimitiveOutcome(
            {
                "base_repo_path": allocation.base_repo_path,
                "worktree_path": allocation.worktree_path,
                "branch": allocation.branch,
                "commit_sha": allocation.commit_sha,
                "reused_existing": allocation.reused_existing,
            }
        )

    def _fetch_pull_request(self, context: PrimitiveContext) -> PrimitiveOutcome:
        pull_request_number = _required_int(context.input_data, "pull_request_number")
        pull_request = self.github.pull_request(context.repo, pull_request_number)
        if context.attempt_id is not None:
            self.store.record_pr(
                context.attempt_id,
                repo=context.repo,
                number=pull_request.number,
                url=pull_request.url,
                title=pull_request.title,
                state=pull_request.state,
                merged_at=pull_request.merged_at,
            )
        return PrimitiveOutcome(
            {
                "pull_request_number": pull_request.number,
                "pull_request_url": pull_request.url,
                "pull_request_title": pull_request.title,
                "state": pull_request.state,
                "merged_at": pull_request.merged_at,
                "is_merged": pull_request.is_merged,
                "head_sha": pull_request.head_sha,
                "head_ref": pull_request.head_ref,
            }
        )

    def _fetch_ci_status(self, context: PrimitiveContext) -> PrimitiveOutcome:
        pull_request_number = _required_int(context.input_data, "pull_request_number")
        return PrimitiveOutcome(asdict(self.github.ci_status(context.repo, pull_request_number)))

    def _fetch_ci_failure_context(self, context: PrimitiveContext) -> PrimitiveOutcome:
        pull_request_number = _required_int(context.input_data, "pull_request_number")
        failed_checks = _check_runs_from_input(context.input_data, "failed_checks")
        try:
            failure_context = self.github.ci_failure_context(
                context.repo,
                pull_request_number,
                failed_checks,
            )
        except GitHubError as exc:
            return PrimitiveOutcome(
                {
                    "failure_context": [],
                    "sha": "",
                    "unavailable_reason": str(exc),
                }
            )
        return PrimitiveOutcome(
            {
                "failure_context": [asdict(check) for check in failure_context.failed_checks],
                "sha": failure_context.sha,
                "unavailable_reason": failure_context.unavailable_reason,
            }
        )

    def _fetch_pr_review_comments(self, context: PrimitiveContext) -> PrimitiveOutcome:
        pull_request_number = _required_int(context.input_data, "pull_request_number")
        comments = self.github.list_pull_request_review_comments(context.repo, pull_request_number)
        return PrimitiveOutcome({"comments": [asdict(comment) for comment in comments]})

    def _detect_merge_conflicts(self, context: PrimitiveContext) -> PrimitiveOutcome:
        pull_request_number = _required_int(context.input_data, "pull_request_number")
        merge_status = self.github.merge_status(context.repo, pull_request_number)
        return PrimitiveOutcome(
            {
                "pull_request_number": merge_status.number,
                "pull_request_url": merge_status.url,
                "pull_request_title": merge_status.title,
                "state": merge_status.state,
                "merged_at": merge_status.merged_at,
                "head_sha": merge_status.head_sha,
                "mergeable": merge_status.mergeable,
                "mergeable_state": merge_status.mergeable_state,
                "has_conflicts": merge_status.has_conflicts,
            }
        )

    def _run_setup(self, context: PrimitiveContext) -> PrimitiveOutcome:
        if not context.worktree_path:
            raise PrimitiveExecutionError("Attempt does not have an allocated workspace.")
        setup_results = WorktreeManager(self.config.workspace).run_setup(
            context.worktree_path,
            self.config.setup,
        )
        output = {"steps": [asdict(result) for result in setup_results]}
        blocking_failures = [
            result for result in setup_results if result.status == "failed" and result.blocks_worker
        ]
        if blocking_failures:
            names = ", ".join(result.name for result in blocking_failures)
            raise PrimitiveExecutionError(f"Blocking setup step failed: {names}", output=output)
        return PrimitiveOutcome(output)

    def _record_workspace_changes(self, context: PrimitiveContext) -> PrimitiveOutcome:
        attempt_id = _required_attempt_id(context)
        worktree_path = _context_or_input_str(context, "worktree_path")
        base_commit_sha = _context_or_input_str(context, "commit_sha")
        summary = WorktreeManager(self.config.workspace).record_changes(
            worktree_path=worktree_path,
            base_commit_sha=base_commit_sha,
        )
        self.store.record_timeline_event(
            attempt_id,
            phase="worktree",
            event_type="changes_recorded",
            message=f"{len(summary.changed_files)} changed file(s)",
            data=asdict(summary),
        )
        return PrimitiveOutcome(
            {
                "changed_files": summary.changed_files,
                "uncommitted_files": summary.uncommitted_files,
                "commit_sha": summary.head_commit_sha,
                "head_commit_sha": summary.head_commit_sha,
                "base_commit_sha": summary.base_commit_sha,
                "commit_count": summary.commit_count,
                "has_changes": summary.has_changes,
                "worktree_path": summary.worktree_path,
            }
        )

    def _cleanup_after_merge(self, context: PrimitiveContext) -> PrimitiveOutcome:
        attempt_id = _required_attempt_id(context)
        pull_request = _pull_request_for_cleanup(self.store, attempt_id, context.input_data)
        if not str(pull_request["merged_at"] or ""):
            return PrimitiveOutcome(
                {
                    "removed": False,
                    "reason": "pull_request_not_merged",
                    "worktree_path": _context_or_input_str(context, "worktree_path"),
                }
            )
        try:
            removal = WorktreeManager(self.config.workspace).remove_worktree(
                base_repo_path=_context_or_input_str(context, "base_repo_path"),
                worktree_path=_context_or_input_str(context, "worktree_path"),
            )
        except Exception as exc:
            self.store.mark_pull_request_worktree_cleanup_failed(int(pull_request["id"]), str(exc))
            raise PrimitiveExecutionError(str(exc)) from exc
        self.store.mark_pull_request_worktree_cleaned(int(pull_request["id"]))
        self.store.record_timeline_event(
            attempt_id,
            phase="worktree",
            event_type="cleaned_after_pr_merge",
            message=removal.worktree_path,
            data=asdict(removal),
        )
        return PrimitiveOutcome(asdict(removal))

    def _run_codex(self, context: PrimitiveContext) -> PrimitiveOutcome:
        attempt_id = _required_attempt_id(context)
        if not context.worktree_path:
            raise PrimitiveExecutionError("Attempt does not have an allocated workspace.")
        task_context = _codex_task_context(context)
        prompt = build_worker_prompt(
            self.config,
            context.repo,
            context.issue_number,
            context.task_type,
            context.issue_title,
            follow_up_context=format_follow_up_context(self.store.follow_up_source_result(attempt_id)),
            task_context=task_context,
            primitive_guidance=context.transition.guidance,
            source_context=_pull_request_source_context(context),
        )
        result = CodexRunner(self.config.codex).run(
            prompt=prompt,
            cwd=context.worktree_path,
            attempt_id=attempt_id,
            store=self.store,
            resume_thread_id=self._codex_thread_id(context),
            persistent_thread=self._uses_persistent_codex_thread(context),
        )
        self._save_codex_thread_id(context, result.thread_id)
        body = result.final_message.strip()
        self.store.record_worker_result(
            attempt_id=attempt_id,
            repo=context.repo,
            issue_number=context.issue_number,
            result_type=_codex_result_type(context),
            title=_codex_result_title(context),
            body=body,
            metadata={
                "dry_run": self.config.policy.dry_run,
                "task_type": context.task_type,
                "primitive": context.transition.action,
                "worktree_path": context.worktree_path,
                "branch": context.branch,
                "pull_request_number": context.input_data.get("pull_request_number"),
            },
        )
        self.store.record_worker_log(attempt_id, "info", body)
        if context.task_type == "research" and body:
            self.store.record_comment(
                attempt_id,
                context.repo,
                context.issue_number,
                "",
                body,
                "drafted",
            )
        return PrimitiveOutcome(
            {
                "thread_id": result.thread_id,
                "turn_count": result.turn_count,
                "duration_ms": result.duration_ms,
                "message_chars": len(result.final_message),
                "result_type": _codex_result_type(context),
            }
        )

    def _create_draft_pr_with_codex(self, context: PrimitiveContext) -> PrimitiveOutcome:
        if self.config.policy.dry_run:
            raise PrimitiveExecutionError("policy.dry_run is true; refusing to create a GitHub pull request.")
        attempt_id = _required_attempt_id(context)
        if not context.worktree_path:
            raise PrimitiveExecutionError("Attempt does not have an allocated workspace.")
        if not context.branch:
            raise PrimitiveExecutionError("Attempt does not have an allocated branch.")

        existing = self.store.pull_requests_for_attempt(attempt_id)
        if existing:
            row = existing[0]
            pull_request = self.github.pull_request(context.repo, int(row["number"]))
            return PrimitiveOutcome(_codex_created_pr_output(pull_request, context))

        result = self.store.worker_result_for_attempt(attempt_id)
        worker_result = str(result["body"]) if result else ""
        source_context = _pull_request_source_context(context)
        prompt = build_pull_request_prompt(
            self.config,
            context.repo,
            context.issue_number,
            context.issue_title,
            worktree_path=context.worktree_path,
            branch=context.branch,
            commit_sha=context.commit_sha,
            worker_result=worker_result,
            issue_link_marker=issue_link_marker(context.repo, context.issue_number),
            primitive_guidance=context.transition.guidance,
            source_context=source_context,
        )
        codex_result = CodexRunner(self.config.codex).run(
            prompt=prompt,
            cwd=context.worktree_path,
            attempt_id=attempt_id,
            store=self.store,
            resume_thread_id=self._codex_thread_id(context),
            persistent_thread=self._uses_persistent_codex_thread(context),
        )
        self._save_codex_thread_id(context, codex_result.thread_id)
        final_message = codex_result.final_message.strip()
        self.store.record_worker_log(attempt_id, "info", final_message)
        pull_request = self._pull_request_created_by_codex(context, final_message)
        marker = pull_request_source_marker(context.repo, context.issue_number, source_context)
        marker_present = _body_links_source(pull_request.body, context, source_context)
        if not marker_present:
            self.store.record_error(
                attempt_id,
                phase="github",
                error_type="PullRequestMarkerMissing",
                message="Codex-created pull request body is missing the Symphony source marker.",
                recoverable=True,
            )
        self.store.record_pr(
            attempt_id,
            context.repo,
            pull_request.number,
            pull_request.url,
            pull_request.title,
            state=pull_request.state,
            merged_at=pull_request.merged_at,
        )
        if source_context.kind not in {"local_ticket", "conversation"}:
            self.store.record_issue_pull_request_link(
                repo=context.repo,
                issue_number=context.issue_number,
                pull_request_number=pull_request.number,
                pull_request_url=pull_request.url,
                pull_request_title=pull_request.title,
                state=pull_request.state,
                link_source="created_by_codex",
                marker=marker,
            )
        self.store.update_attempt_workspace(
            attempt_id,
            base_repo_path=context.base_repo_path,
            worktree_path=context.worktree_path,
            branch=context.branch,
            commit_sha=pull_request.head_sha or context.commit_sha,
        )
        self.store.record_timeline_event(
            attempt_id,
            phase="github",
            event_type="pull_request_created",
            message=pull_request.url,
            data={
                "number": pull_request.number,
                "created_by": "codex",
                "thread_id": codex_result.thread_id,
                "issue_marker_present": marker_present,
            },
        )
        if context.work_item_id is not None:
            self.work_items.record_created_pull_request(
                work_item_id=context.work_item_id,
                number=pull_request.number,
                url=pull_request.url,
                title=pull_request.title,
                body=pull_request.body or marker,
                link_source="created_by_codex",
                marker=marker,
            )
        return PrimitiveOutcome(_codex_created_pr_output(pull_request, context))

    def _uses_persistent_codex_thread(self, context: PrimitiveContext) -> bool:
        return self.config.codex.transport == "app-server" and context.work_item_id is not None

    def _codex_thread_id(self, context: PrimitiveContext) -> str | None:
        if not self._uses_persistent_codex_thread(context):
            return None
        if context.attempt_id is not None:
            thread_id = self.work_items.codex_thread_id_for_attempt(context.attempt_id)
            if thread_id is not None:
                return thread_id
        if context.source_item_kind == CONVERSATION_KIND and context.work_item_id is not None:
            return self.chats.codex_thread_id_for_work_item(context.work_item_id)
        return None

    def _save_codex_thread_id(self, context: PrimitiveContext, thread_id: str) -> None:
        if not self._uses_persistent_codex_thread(context):
            return
        if context.attempt_id is not None:
            self.work_items.save_codex_thread_id_for_attempt(context.attempt_id, thread_id)
        if context.source_item_kind == CONVERSATION_KIND and context.work_item_id is not None:
            self.chats.save_codex_thread_id_for_work_item(context.work_item_id, thread_id)

    def _create_draft_pr(self, context: PrimitiveContext) -> PrimitiveOutcome:
        if self.config.policy.dry_run:
            raise PrimitiveExecutionError("policy.dry_run is true; refusing to create a GitHub pull request.")
        attempt_id = _required_attempt_id(context)
        try:
            pull_request = self.review_actions.create_draft_pr(attempt_id)
        except ReviewActionError as exc:
            raise PrimitiveExecutionError(str(exc)) from exc
        source_context = _pull_request_source_context(context)
        marker = pull_request_source_marker(context.repo, context.issue_number, source_context)
        if context.work_item_id is not None:
            self.work_items.record_created_pull_request(
                work_item_id=context.work_item_id,
                number=pull_request.number,
                url=pull_request.url,
                title=pull_request.title,
                body=pull_request.body or marker,
                link_source="created_by_symphony",
                marker=marker,
            )
        return PrimitiveOutcome(_codex_created_pr_output(pull_request, context))

    def _pull_request_created_by_codex(
        self,
        context: PrimitiveContext,
        final_message: str,
    ) -> PullRequest:
        number = _pull_request_number_from_text(context.repo, final_message)
        if number is not None:
            return self.github.pull_request(context.repo, number)
        branch_match = None
        marker_match = None
        source_context = _pull_request_source_context(context)
        for pull_request in self.github.list_pull_requests(context.repo, state="open"):
            if context.branch and pull_request.head_ref == context.branch:
                branch_match = pull_request
                break
            if _body_links_source(pull_request.body, context, source_context):
                marker_match = pull_request
        if branch_match is not None:
            return self.github.pull_request(context.repo, branch_match.number)
        if marker_match is not None:
            return self.github.pull_request(context.repo, marker_match.number)
        raise PrimitiveExecutionError(
            "Codex did not report a created pull request URL and no matching open pull request was found.",
            output={"final_message": final_message},
        )

    def _push_pr_update(self, context: PrimitiveContext) -> PrimitiveOutcome:
        if self.config.policy.dry_run:
            raise PrimitiveExecutionError(
                "policy.dry_run is true; refusing to push a GitHub pull request update."
            )
        attempt_id = _required_attempt_id(context)
        try:
            update = self.review_actions.push_pr_update(attempt_id)
        except ReviewActionError as exc:
            raise PrimitiveExecutionError(str(exc)) from exc
        return PrimitiveOutcome(
            {
                "pull_request_number": update.number,
                "pull_request_url": update.url,
                "pull_request_title": update.title,
                "branch": update.branch,
                "commit_sha": update.commit_sha,
                "pushed": update.pushed,
            }
        )

    def _post_issue_comment(self, context: PrimitiveContext) -> PrimitiveOutcome:
        if self.config.policy.dry_run:
            raise PrimitiveExecutionError("policy.dry_run is true; refusing to post a GitHub issue comment.")
        comment_id = _required_int(context.input_data, "comment_id")
        body = _required_str(context.input_data, "body")
        try:
            posted = self.review_actions.post_comment(comment_id, body)
        except ReviewActionError as exc:
            raise PrimitiveExecutionError(str(exc)) from exc
        return PrimitiveOutcome(
            {
                "comment_id": comment_id,
                "comment_url": posted.url,
                "attempt_id": posted.attempt_id,
                "repo": posted.repo,
                "issue_number": posted.issue_number,
            }
        )

    def _source_sync(self, context: PrimitiveContext) -> PrimitiveOutcome:
        source_id = _context_or_input_int(context, "source_id")
        summary = SourceSyncService(self.sources, self.github).sync_source(source_id)
        return PrimitiveOutcome(asdict(summary))

    def _source_sync_all(self) -> PrimitiveOutcome:
        service = SourceSyncService(self.sources, self.github)
        summaries = [asdict(service.sync_source(source.id)) for source in self.sources.list_sources()]
        return PrimitiveOutcome({"sources": summaries, "synced": len(summaries)})

    def _work_item_activate(self, context: PrimitiveContext) -> PrimitiveOutcome:
        source_item_id = _context_or_input_int(context, "source_item_id")
        task_type = str(context.input_data.get("task_type") or context.task_type)
        user_hint = str(context.input_data.get("user_hint") or context.user_hint)
        work_item = self.work_items.activate_source_item(
            WorkItemActivation(
                source_item_id=source_item_id,
                task_type=task_type,
                user_hint=user_hint,
            )
        )
        return PrimitiveOutcome(_work_item_output(work_item))

    def _work_item_move(self, context: PrimitiveContext) -> PrimitiveOutcome:
        work_item_id = _context_or_input_int(context, "work_item_id")
        target_state = str(context.input_data.get("target_state") or context.transition.to_state)
        reasons = _string_list(context.input_data.get("reasons")) or context.rerun_reasons
        note = str(context.input_data.get("note") or context.user_hint)
        work_item = self.work_items.move_work_item(
            WorkItemMove(
                work_item_id=work_item_id,
                target_state=target_state,
                reasons=reasons,
                note=note,
            )
        )
        return PrimitiveOutcome(_work_item_output(work_item))

    def _work_item_link_source_item(self, context: PrimitiveContext) -> PrimitiveOutcome:
        work_item_id = _context_or_input_int(context, "work_item_id")
        source_item_id = _context_or_input_int(context, "source_item_id")
        relationship = str(context.input_data.get("relationship") or "related")
        work_item = self.work_items.link_source_item(
            work_item_id=work_item_id,
            source_item_id=source_item_id,
            relationship=relationship,
        )
        return PrimitiveOutcome(_work_item_output(work_item))

    def _work_item_select_active_pr(self, context: PrimitiveContext) -> PrimitiveOutcome:
        work_item_id = _context_or_input_int(context, "work_item_id")
        source_item_id = _context_or_input_int(context, "source_item_id")
        work_item = self.work_items.select_active_pr(work_item_id, source_item_id)
        return PrimitiveOutcome(_work_item_output(work_item))

    def _label_changes(self, to_state: str) -> tuple[list[str], list[str]]:
        labels = self.config.labels
        if to_state == "claimed":
            return [labels.working], [labels.todo]
        if to_state == "review":
            return [labels.review], [labels.working]
        if to_state == "blocked":
            return [labels.blocked], [labels.review, labels.working, labels.todo]
        if to_state == "done":
            return [labels.done], [labels.review, labels.working, labels.todo]
        return [], []


def _required_attempt_id(context: PrimitiveContext) -> int:
    if context.attempt_id is None:
        raise PrimitiveExecutionError(f"Primitive {context.transition.action} requires an attempt.")
    return context.attempt_id


def _required_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value:
        return int(value)
    raise PrimitiveExecutionError(f"Primitive input '{key}' is required.")


def _required_str(data: dict[str, Any], key: str) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise PrimitiveExecutionError(f"Primitive input '{key}' is required.")
    return value


def _context_or_input_str(context: PrimitiveContext, key: str) -> str:
    context_value = getattr(context, key)
    value = str(context.input_data.get(key) or context_value or "").strip()
    if not value:
        raise PrimitiveExecutionError(f"Primitive input '{key}' is required.")
    return value


def _context_or_input_int(context: PrimitiveContext, key: str) -> int:
    value = context.input_data.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value:
        return int(value)
    context_value = getattr(context, key)
    if isinstance(context_value, int):
        return context_value
    raise PrimitiveExecutionError(f"Primitive input '{key}' is required.")


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value:
        return [value]
    return []


def _work_item_output(work_item: WorkItemView) -> dict[str, Any]:
    return {
        "work_item_id": work_item.id,
        "source_id": work_item.source_id,
        "primary_source_item_id": work_item.primary_source_item_id,
        "active_pr_source_item_id": work_item.active_pr_source_item_id,
        "state": work_item.state,
        "task_type": work_item.task_type,
        "title": work_item.title,
    }


def _pull_request_source_context(context: PrimitiveContext) -> PullRequestSourceContext:
    if context.source_item_kind in {"local_ticket", "conversation"}:
        return PullRequestSourceContext(
            kind=context.source_item_kind,
            source_item_id=context.source_item_id,
            source_item_number=context.source_item_number,
            title=context.issue_title,
        )
    return PullRequestSourceContext(title=context.issue_title)


def _body_links_source(
    body: str,
    context: PrimitiveContext,
    source_context: PullRequestSourceContext,
) -> bool:
    if source_context.kind in {"local_ticket", "conversation"} and source_context.source_item_id is not None:
        return body_links_source_item(body, source_context.source_item_id)
    return body_links_issue(body, context.repo, context.issue_number)


def _pull_request_for_cleanup(
    store: Store,
    attempt_id: int,
    input_data: dict[str, Any],
) -> sqlite3.Row:
    pull_request_id = input_data.get("pull_request_id")
    pull_requests = store.pull_requests_for_attempt(attempt_id)
    if pull_request_id:
        for pull_request in pull_requests:
            if int(pull_request["id"]) == int(pull_request_id):
                return pull_request
        raise PrimitiveExecutionError(f"Pull request row {pull_request_id} does not belong to this attempt.")
    if not pull_requests:
        raise PrimitiveExecutionError("Attempt does not have a recorded pull request.")
    return pull_requests[0]


def _pull_request_output(pull_request: PullRequest) -> dict[str, Any]:
    return {
        "number": pull_request.number,
        "url": pull_request.url,
        "title": pull_request.title,
        "state": pull_request.state,
        "merged_at": pull_request.merged_at,
        "head_sha": pull_request.head_sha,
        "head_ref": pull_request.head_ref,
        "head_repo": pull_request.head_repo,
    }


def _codex_created_pr_output(pull_request: PullRequest, context: PrimitiveContext) -> dict[str, Any]:
    source_context = _pull_request_source_context(context)
    return {
        "pull_request_number": pull_request.number,
        "pull_request_url": pull_request.url,
        "pull_request_title": pull_request.title,
        "state": pull_request.state,
        "merged_at": pull_request.merged_at,
        "head_ref": pull_request.head_ref,
        "head_sha": pull_request.head_sha,
        "issue_marker_present": _body_links_source(pull_request.body, context, source_context),
    }


def _pull_request_number_from_text(repo: str, text: str) -> int | None:
    pattern = rf"https://github\.com/{re.escape(repo)}/pull/(\d+)"
    match = re.search(pattern, text)
    if not match:
        return None
    return int(match.group(1))


def _source_ref(pull_request: PullRequest) -> str:
    if not pull_request.head_ref:
        return ""
    return f"origin/{pull_request.head_ref}"


def _codex_task_context(context: PrimitiveContext) -> str:
    work_item_context = _work_item_task_context(context)
    if context.transition.action == "codex.address_pr_comments":
        pull_request_number = _required_int(context.input_data, "pull_request_number")
        comments = _input_dicts(context.input_data, "comments")
        return "\n".join(
            [
                work_item_context,
                f"Pull request: https://github.com/{context.repo}/pull/{pull_request_number}",
                "Address the unresolved review comments below and keep the existing issue fix focused.",
                _format_records("Review comments", comments),
            ]
        ).strip()
    if context.transition.action == "codex.fix_ci_failures":
        pull_request_number = _required_int(context.input_data, "pull_request_number")
        failed_checks = _input_dicts(context.input_data, "failed_checks")
        checks = _input_dicts(context.input_data, "checks")
        failure_context = _input_dicts(context.input_data, "failure_context")
        return "\n".join(
            [
                work_item_context,
                f"Pull request: https://github.com/{context.repo}/pull/{pull_request_number}",
                "Inspect the failing CI checks below, fix the issue, and rerun the narrowest relevant tests.",
                _format_records("Failed checks", failed_checks),
                _format_ci_failure_context(failure_context),
                _format_records("All checks", checks),
            ]
        ).strip()
    if context.transition.action == "codex.address_pr_feedback":
        pull_request_number = _required_int(context.input_data, "pull_request_number")
        comments = _input_dicts(context.input_data, "comments")
        failed_checks = _input_dicts(context.input_data, "failed_checks")
        checks = _input_dicts(context.input_data, "checks")
        failure_context = _input_dicts(context.input_data, "failure_context")
        has_conflicts = bool(context.input_data.get("has_conflicts"))
        mergeable_state = str(context.input_data.get("mergeable_state") or "")
        conflict_text = (
            f"Merge conflicts: yes; mergeable_state={mergeable_state}"
            if has_conflicts
            else f"Merge conflicts: no; mergeable_state={mergeable_state or 'unknown'}"
        )
        return "\n".join(
            [
                work_item_context,
                f"Pull request: https://github.com/{context.repo}/pull/{pull_request_number}",
                "Address the pull request feedback below in one focused update.",
                conflict_text,
                _format_records("Failed checks", failed_checks),
                _format_ci_failure_context(failure_context),
                _format_records("All checks", checks),
                _format_records("Review comments", comments),
            ]
        ).strip()
    if context.transition.action == "codex.operations_task":
        user_hint = str(context.input_data.get("user_hint") or context.user_hint).strip()
        return "\n".join(
            [
                work_item_context,
                "Perform the requested operational task and return a durable summary.",
                f"Operator hint: {user_hint or 'none provided'}",
            ]
        ).strip()
    return work_item_context


def _work_item_task_context(context: PrimitiveContext) -> str:
    lines: list[str] = []
    if context.work_item_id is not None:
        lines.append(f"Work item: #{context.work_item_id}")
    if context.user_hint:
        lines.append(f"Operator hint: {context.user_hint}")
    if context.rerun_reasons:
        lines.append("Rerun reasons: " + ", ".join(context.rerun_reasons))
    return "\n".join(lines)


def _codex_result_type(context: PrimitiveContext) -> str:
    if context.transition.action == "codex.operations_task":
        return "operations_summary"
    if context.transition.action == "codex.address_pr_comments":
        return "pr_review_update"
    if context.transition.action == "codex.fix_ci_failures":
        return "ci_fix_summary"
    if context.transition.action == "codex.address_pr_feedback":
        return "pr_feedback_update"
    return result_type(context.task_type)


def _codex_result_title(context: PrimitiveContext) -> str:
    if context.transition.action == "codex.operations_task":
        return "Operations Summary"
    if context.transition.action == "codex.address_pr_comments":
        return "PR Review Update"
    if context.transition.action == "codex.fix_ci_failures":
        return "CI Fix Summary"
    if context.transition.action == "codex.address_pr_feedback":
        return "PR Feedback Update"
    return result_title(context.task_type)


def _input_dicts(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], data.get(key) or [])


def _check_runs_from_input(data: dict[str, Any], key: str) -> list[GitHubCheckRun]:
    return [
        GitHubCheckRun(
            name=str(item.get("name") or ""),
            status=str(item.get("status") or ""),
            conclusion=str(item.get("conclusion") or ""),
            url=str(item.get("url") or ""),
        )
        for item in _input_dicts(data, key)
    ]


def _format_records(title: str, records: list[dict[str, Any]]) -> str:
    if not records:
        return f"{title}: none provided."
    lines = [f"{title}:"]
    for index, record in enumerate(records, start=1):
        fields = [
            f"{name}={value}"
            for name, value in record.items()
            if value not in ("", None) and value != [] and value != {}
        ]
        lines.append(f"{index}. " + "; ".join(fields))
    return "\n".join(lines)


def _format_ci_failure_context(records: list[dict[str, Any]]) -> str:
    if not records:
        return "CI failure context: none provided."
    lines = ["CI failure context:"]
    for index, record in enumerate(records, start=1):
        name = str(record.get("name") or "unnamed check")
        conclusion = str(record.get("conclusion") or "unknown")
        url = str(record.get("url") or record.get("details_url") or "")
        lines.append(f"{index}. {name} ({conclusion})" + (f" {url}" if url else ""))
        has_actionable_detail = False
        for record_field, label in (
            ("summary", "summary"),
            ("text", "output"),
            ("log_excerpt", "log excerpt"),
        ):
            value = str(record.get(record_field) or "").strip()
            if value:
                has_actionable_detail = True
                lines.append(f"   {label}:")
                lines.extend(f"   {line}" for line in value.splitlines())
        annotations = cast(list[dict[str, Any]], record.get("annotations") or [])
        for annotation in annotations:
            title = str(annotation.get("title") or "").strip()
            message = str(annotation.get("message") or "").strip()
            raw_details = str(annotation.get("raw_details") or "").strip()
            if not is_actionable_ci_annotation(
                annotation_level=str(annotation.get("annotation_level") or ""),
                title=title,
                message=message,
                raw_details=raw_details,
            ):
                continue
            location = _annotation_location(annotation)
            details = " - ".join(part for part in (title, message) if part)
            if details:
                has_actionable_detail = True
                lines.append(f"   annotation: {location}{details}")
        unavailable_reason = str(record.get("unavailable_reason") or "").strip()
        if unavailable_reason:
            has_actionable_detail = True
            lines.append(f"   unavailable: {unavailable_reason}")
        elif not has_actionable_detail:
            lines.append("   unavailable: no actionable CI failure output captured.")
    return "\n".join(lines)


def _annotation_location(annotation: dict[str, Any]) -> str:
    path = str(annotation.get("path") or "").strip()
    line = annotation.get("start_line") or annotation.get("line")
    if path and line:
        return f"{path}:{line} "
    if path:
        return f"{path} "
    return ""
