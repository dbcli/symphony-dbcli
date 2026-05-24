from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, cast

from .config import WorkflowConfig
from .github import GitHubCiStatus, GitHubClient, GitHubComment, GitHubError, GitHubIssue, PullRequest
from .review_actions import GitHubReviewClient, ReviewActionError, ReviewActions
from .runner import CodexRunner
from .store import Store
from .worker_prompt import (
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

    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> None: ...

    def remove_label(self, repo: str, issue_number: int, label: str) -> None: ...

    def pull_request(self, repo: str, number: int) -> PullRequest: ...

    def ci_status(self, repo: str, pull_request_number: int) -> GitHubCiStatus: ...


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
        if context.transition.action == "github.fetch_issue":
            return self._fetch_issue(context)
        if context.transition.action == "github.fetch_comments":
            return self._fetch_comments(context)
        if context.transition.action == "github.apply_labels":
            return self._apply_labels(context)
        if context.transition.action == "github.fetch_pull_request":
            return self._fetch_pull_request(context)
        if context.transition.action == "github.fetch_ci_status":
            return self._fetch_ci_status(context)
        if context.transition.action == "workspace.allocate":
            return self._allocate_workspace(context)
        if context.transition.action == "workspace.run_setup":
            return self._run_setup(context)
        if context.transition.action in {
            "codex.research_issue",
            "codex.fix_issue",
            "codex.address_pr_comments",
            "codex.fix_ci_failures",
        }:
            return self._run_codex(context)
        if context.transition.action == "github.create_draft_pr":
            return self._create_draft_pr(context)
        if context.transition.action == "github.post_issue_comment":
            return self._post_issue_comment(context)
        raise PrimitiveExecutionError(f"Primitive {context.transition.action} is not implemented.")

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

    def _allocate_workspace(self, context: PrimitiveContext) -> PrimitiveOutcome:
        attempt_id = _required_attempt_id(context)
        allocation = WorktreeManager(self.config.workspace).allocate(
            context.repo,
            context.issue_number,
            attempt_id,
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
            }
        )

    def _fetch_ci_status(self, context: PrimitiveContext) -> PrimitiveOutcome:
        pull_request_number = _required_int(context.input_data, "pull_request_number")
        return PrimitiveOutcome(asdict(self.github.ci_status(context.repo, pull_request_number)))

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
        )
        result = CodexRunner(self.config.codex).run(
            prompt=prompt,
            cwd=context.worktree_path,
            attempt_id=attempt_id,
            store=self.store,
        )
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

    def _create_draft_pr(self, context: PrimitiveContext) -> PrimitiveOutcome:
        if self.config.policy.dry_run:
            raise PrimitiveExecutionError("policy.dry_run is true; refusing to create a GitHub pull request.")
        attempt_id = _required_attempt_id(context)
        try:
            pull_request = self.review_actions.create_draft_pr(
                attempt_id,
                title=str(context.input_data.get("title", "")),
                body=str(context.input_data.get("body", "")),
            )
        except ReviewActionError as exc:
            raise PrimitiveExecutionError(str(exc)) from exc
        return PrimitiveOutcome(
            {
                "pull_request_number": pull_request.number,
                "pull_request_url": pull_request.url,
                "pull_request_title": pull_request.title,
                "state": pull_request.state,
                "merged_at": pull_request.merged_at,
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


def _codex_task_context(context: PrimitiveContext) -> str:
    if context.transition.action == "codex.address_pr_comments":
        pull_request_number = _required_int(context.input_data, "pull_request_number")
        comments = _input_dicts(context.input_data, "comments")
        return "\n".join(
            [
                f"Pull request: https://github.com/{context.repo}/pull/{pull_request_number}",
                "Address the unresolved review comments below and keep the existing issue fix focused.",
                _format_records("Review comments", comments),
            ]
        ).strip()
    if context.transition.action == "codex.fix_ci_failures":
        pull_request_number = _required_int(context.input_data, "pull_request_number")
        failed_checks = _input_dicts(context.input_data, "failed_checks")
        checks = _input_dicts(context.input_data, "checks")
        return "\n".join(
            [
                f"Pull request: https://github.com/{context.repo}/pull/{pull_request_number}",
                "Inspect the failing CI checks below, fix the issue, and rerun the narrowest relevant tests.",
                _format_records("Failed checks", failed_checks),
                _format_records("All checks", checks),
            ]
        ).strip()
    return ""


def _codex_result_type(context: PrimitiveContext) -> str:
    if context.transition.action == "codex.address_pr_comments":
        return "pr_review_update"
    if context.transition.action == "codex.fix_ci_failures":
        return "ci_fix_summary"
    return result_type(context.task_type)


def _codex_result_title(context: PrimitiveContext) -> str:
    if context.transition.action == "codex.address_pr_comments":
        return "PR Review Update"
    if context.transition.action == "codex.fix_ci_failures":
        return "CI Fix Summary"
    return result_title(context.task_type)


def _input_dicts(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], data.get(key) or [])


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
