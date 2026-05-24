from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import traceback
import uuid
from pathlib import Path

from .config import WorkflowConfig, WorkflowError, parse_workflow
from .github import GitHubClient, GitHubError
from .runner import CodexRunner
from .store import IssueSnapshot, Store
from .worktree import WorktreeManager


class OrchestratorError(RuntimeError):
    """Raised when orchestration cannot continue."""


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
    def __init__(self, config: WorkflowConfig, store: Store, workflow_version_id: int | None = None):
        self.config = config
        self.store = store
        self.workflow_version_id = workflow_version_id
        self.github = GitHubClient(config.github)

    def poll_once(self) -> int:
        synced = 0
        for repo in self.config.github.repos:
            self.store.upsert_repo(repo)
            issues = self.github.list_issues(repo, labels=[self.config.labels.todo])
            for issue in issues:
                self.store.upsert_issue(
                    issue.snapshot(self.config.labels, self.config.workers.default_task_type)
                )
                synced += 1
        return synced

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

    def _claim_issue(self, issue: sqlite3.Row) -> int:
        repo = str(issue["repo"])
        issue_number = int(issue["number"])
        attempt_id = self.store.create_attempt(
            repo=repo,
            issue_number=issue_number,
            task_type=str(issue["task_type"]),
            workflow_version_id=self.workflow_version_id,
            status="queued",
        )
        self.store.record_timeline_event(
            attempt_id,
            phase="queue",
            event_type="claimed",
            message=f"{repo}#{issue_number}",
        )
        if not self.config.policy.dry_run:
            self.github.add_labels(repo, issue_number, [self.config.labels.working])
            try:
                self.github.remove_label(repo, issue_number, self.config.labels.todo)
            except GitHubError:
                pass
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
        repo = str(attempt["repo"])
        issue_number = int(attempt["issue_number"])
        resolved_task_type = str(attempt["task_type"])
        issue = self.store.issue_detail(repo, issue_number)
        title = repo if not issue else str(issue["issue"]["title"])
        worker_id = str(attempt["worker_id"] or f"worker-{attempt_id}-{uuid.uuid4().hex[:8]}")
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
            allocation = WorktreeManager(self.config.workspace).allocate(repo, issue_number, attempt_id)
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
            prompt = build_worker_prompt(
                self.config,
                repo,
                issue_number,
                resolved_task_type,
                title,
                follow_up_context=_format_follow_up_context(self.store.follow_up_source_result(attempt_id)),
            )
            result = CodexRunner(self.config.codex).run(
                prompt=prompt,
                cwd=allocation.worktree_path,
                attempt_id=attempt_id,
                store=self.store,
            )
            self.store.record_worker_result(
                attempt_id=attempt_id,
                repo=repo,
                issue_number=issue_number,
                result_type=_result_type(resolved_task_type),
                title=_result_title(resolved_task_type),
                body=result.final_message.strip(),
                metadata={
                    "dry_run": self.config.policy.dry_run,
                    "task_type": resolved_task_type,
                    "worktree_path": allocation.worktree_path,
                    "branch": allocation.branch,
                },
            )
            self.store.record_worker_log(attempt_id, "info", result.final_message)
            self._complete_github_side_effects(
                attempt_id,
                repo,
                issue_number,
                result.final_message,
            )
            self.store.finish_attempt(attempt_id, "review", "needs_review")
            return attempt_id
        except Exception as exc:
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

    def _complete_github_side_effects(
        self,
        attempt_id: int,
        repo: str,
        issue_number: int,
        final_message: str,
    ) -> None:
        draft_body = final_message.strip()
        if draft_body:
            self.store.record_comment(
                attempt_id,
                repo,
                issue_number,
                "",
                draft_body,
                "drafted",
            )
        if self.config.policy.dry_run:
            return
        self.github.add_labels(repo, issue_number, [self.config.labels.review])
        try:
            self.github.remove_label(repo, issue_number, self.config.labels.working)
        except GitHubError:
            pass


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


def build_worker_prompt(
    config: WorkflowConfig,
    repo: str,
    issue_number: int,
    task_type: str,
    title: str,
    follow_up_context: str = "",
) -> str:
    follow_up_section = f"\nFollow-up context:\n{follow_up_context}\n" if follow_up_context else ""
    return f"""\
You are a Symphony worker for {repo}.

Task type: {task_type}
GitHub issue: https://github.com/{repo}/issues/{issue_number}
Issue title: {title}
{follow_up_section}

Follow this workflow:
{config.instructions}

Before finishing, provide:
- a concise summary of what you did
- tests or checks run, if any
- remaining risks or blockers
"""


def _format_follow_up_context(source_result: sqlite3.Row | None) -> str:
    if not source_result:
        return ""
    body = str(source_result["body"]).strip()
    if not body:
        return ""
    return f"""\
This code task was created from research attempt #{source_result["source_attempt_id"]}.
Use the research findings as implementation guidance, but verify them against the code before editing.

Research result:
{body}
"""


def _result_type(task_type: str) -> str:
    if task_type == "code":
        return "code_summary"
    return "research_answer"


def _result_title(task_type: str) -> str:
    if task_type == "code":
        return "Code Worker Summary"
    return "Research Answer"
