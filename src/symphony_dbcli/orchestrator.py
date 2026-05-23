from __future__ import annotations

import hashlib
import traceback
import uuid
from pathlib import Path

from .config import WorkflowConfig, WorkflowError, parse_workflow
from .github import GitHubClient, GitHubError
from .gitops import GitWorktree
from .runner import CodexRunner
from .store import IssueSnapshot, Store
from .worktree import WorktreeAllocation, WorktreeManager


class OrchestratorError(RuntimeError):
    """Raised when orchestration cannot continue."""


def load_and_record_workflow(store: Store, workflow_path: str | Path) -> tuple[WorkflowConfig, int]:
    path = Path(workflow_path)
    content = path.read_text(encoding="utf-8")
    try:
        config = parse_workflow(content)
    except WorkflowError as exc:
        store.record_workflow_version(path, content, None, status="rejected", error=str(exc))
        raise
    version_id = store.record_workflow_version(path, content, config, status="accepted")
    return config, version_id


class WorkflowWatcher:
    def __init__(self, store: Store, workflow_path: str | Path):
        self.store = store
        self.workflow_path = Path(workflow_path)
        self._last_hash = ""
        self.current_config: WorkflowConfig | None = None
        self.current_version_id: int | None = None

    def reload_if_changed(self) -> tuple[WorkflowConfig, int, bool]:
        content = self.workflow_path.read_text(encoding="utf-8")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest == self._last_hash and self.current_config and self.current_version_id:
            return self.current_config, self.current_version_id, False
        config, version_id = load_and_record_workflow(self.store, self.workflow_path)
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
        issue = eligible[0]
        attempt_id = self.store.create_attempt(
            repo=issue["repo"],
            issue_number=int(issue["number"]),
            task_type=issue["task_type"],
            workflow_version_id=self.workflow_version_id,
            status="queued",
        )
        if not self.config.policy.dry_run:
            self.github.add_labels(issue["repo"], int(issue["number"]), [self.config.labels.working])
            try:
                self.github.remove_label(issue["repo"], int(issue["number"]), self.config.labels.todo)
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
        worker_id = f"worker-{attempt_id}-{uuid.uuid4().hex[:8]}"
        self.store.start_attempt(attempt_id, worker_id)
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
                self.config, repo, issue_number, resolved_task_type, issue_row["title"]
            )
            result = CodexRunner(self.config.codex).run(
                prompt=prompt,
                cwd=allocation.worktree_path,
                attempt_id=attempt_id,
                store=self.store,
            )
            self.store.record_worker_log(attempt_id, "info", result.final_message)
            self._complete_github_side_effects(
                attempt_id,
                repo,
                issue_number,
                resolved_task_type,
                result.final_message,
                allocation,
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

    def _complete_github_side_effects(
        self,
        attempt_id: int,
        repo: str,
        issue_number: int,
        task_type: str,
        final_message: str,
        allocation: WorktreeAllocation,
    ) -> None:
        if self.config.policy.dry_run:
            self.store.record_comment(
                attempt_id,
                repo,
                issue_number,
                "",
                final_message,
                "drafted",
            )
            return
        if task_type == "code" and self.config.policy.open_pull_requests:
            self._publish_code_task(attempt_id, repo, issue_number, final_message, allocation)
        if task_type == "research" and self.config.policy.post_research_answers:
            url = self.github.create_comment(repo, issue_number, final_message)
            self.store.record_comment(attempt_id, repo, issue_number, url, final_message, "posted")
        self.github.add_labels(repo, issue_number, [self.config.labels.review])
        try:
            self.github.remove_label(repo, issue_number, self.config.labels.working)
        except GitHubError:
            pass

    def _publish_code_task(
        self,
        attempt_id: int,
        repo: str,
        issue_number: int,
        final_message: str,
        allocation: WorktreeAllocation,
    ) -> None:
        worktree = GitWorktree(allocation.worktree_path)
        if worktree.has_changes():
            self.store.record_timeline_event(attempt_id, phase="git", event_type="commit_started")
            commit = worktree.commit_all(f"Work on {repo}#{issue_number}")
            self.store.record_timeline_event(
                attempt_id,
                phase="git",
                event_type="committed",
                message=commit.sha,
                data={"message": commit.message},
            )
            commit_sha = commit.sha
        elif worktree.commits_since(allocation.commit_sha) > 0:
            commit_sha = worktree.head_sha()
            self.store.record_timeline_event(
                attempt_id,
                phase="git",
                event_type="existing_commits_detected",
                message=commit_sha,
            )
        else:
            self.store.record_comment(
                attempt_id,
                repo,
                issue_number,
                "",
                "Code worker finished without repository changes.\n\n" + final_message,
                "drafted",
            )
            return
        self.store.update_attempt_workspace(
            attempt_id,
            base_repo_path=allocation.base_repo_path,
            worktree_path=allocation.worktree_path,
            branch=allocation.branch,
            commit_sha=commit_sha,
        )
        self.github.push_branch(repo=repo, worktree_path=allocation.worktree_path, branch=allocation.branch)
        self.store.record_timeline_event(
            attempt_id,
            phase="github",
            event_type="branch_pushed",
            message=allocation.branch,
        )
        pr = self.github.create_pull_request(
            repo=repo,
            title=f"Work on #{issue_number}",
            head=allocation.branch,
            base=self.github.default_branch(repo),
            body=final_message,
            draft=True,
        )
        self.store.record_pr(attempt_id, repo, pr.number, pr.url, pr.title)
        self.store.record_timeline_event(
            attempt_id,
            phase="github",
            event_type="pull_request_created",
            message=pr.url,
            data={"number": pr.number},
        )


def build_worker_prompt(
    config: WorkflowConfig,
    repo: str,
    issue_number: int,
    task_type: str,
    title: str,
) -> str:
    return f"""\
You are a Symphony worker for {repo}.

Task type: {task_type}
GitHub issue: https://github.com/{repo}/issues/{issue_number}
Issue title: {title}

Follow this workflow:
{config.instructions}

Before finishing, provide:
- a concise summary of what you did
- tests or checks run, if any
- remaining risks or blockers
"""
