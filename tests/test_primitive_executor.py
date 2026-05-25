from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

from symphony_dbcli.config import CodexConfig, PolicyConfig, WorkflowConfig, WorkspaceConfig, default_config
from symphony_dbcli.github import (
    GitHubCheckRun,
    GitHubCiStatus,
    GitHubComment,
    GitHubIssue,
    GitHubPullRequestReviewComment,
    PullRequest,
    PullRequestMergeStatus,
)
from symphony_dbcli.primitive_executor import PrimitiveContext, PrimitiveExecutor
from symphony_dbcli.review_actions import issue_link_marker
from symphony_dbcli.sources import SourceCreate
from symphony_dbcli.store import Store
from symphony_dbcli.workflow_definition import WorkflowTransitionConfig


def test_fetch_issue_persists_latest_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    executor = PrimitiveExecutor(default_config(), store, github=FakePrimitiveGitHub())

    output = executor.execute(_context("github.fetch_issue")).output

    detail = store.issue_detail("dbcli/litecli", 245)
    assert output["issue"]["title"] == "Logging path support"
    assert detail is not None
    assert detail["issue"]["title"] == "Logging path support"
    assert {row["label"] for row in detail["labels"]} == {"symphony:todo", "symphony:type:code"}


def test_fetch_comments_returns_comment_snapshots(tmp_path: Path) -> None:
    executor = PrimitiveExecutor(default_config(), _store(tmp_path), github=FakePrimitiveGitHub())

    output = executor.execute(_context("github.fetch_comments")).output

    assert output["comments"] == [
        {
            "id": 99,
            "url": "https://github.com/dbcli/litecli/issues/245#issuecomment-99",
            "body": "I can reproduce this on 1.12.",
            "author": "amjith",
            "created_at": "2026-05-24T10:00:00Z",
            "updated_at": "2026-05-24T10:00:00Z",
        }
    ]


def test_source_sync_primitive_persists_source_items(tmp_path: Path) -> None:
    executor = PrimitiveExecutor(default_config(), _store(tmp_path), github=FakePrimitiveGitHub())
    source = executor.sources.create_source(SourceCreate(repo="dbcli/litecli"))

    output = executor.execute(_context("source.sync", input_data={"source_id": source.id})).output
    backlog = executor.sources.backlog_source_items(source.id)

    assert output["source_id"] == source.id
    assert output["issue_count"] == 1
    assert output["pull_request_count"] == 2
    assert [item.title for item in backlog] == ["Logging path support", "Mention issue without marker"]
    assert backlog[0].default_task_type == "code"
    assert backlog[0].linked_items[0].number == 12


def test_work_item_primitives_activate_and_move_work(tmp_path: Path) -> None:
    executor = PrimitiveExecutor(default_config(), _store(tmp_path), github=FakePrimitiveGitHub())
    source = executor.sources.create_source(SourceCreate(repo="dbcli/litecli"))
    executor.execute(_context("source.sync", input_data={"source_id": source.id}))
    source_item = executor.sources.backlog_source_items(source.id)[0]

    activated = executor.execute(
        _context(
            "work_item.activate",
            input_data={
                "source_item_id": source_item.id,
                "task_type": "code",
                "user_hint": "Prefer unit tests.",
            },
        )
    ).output
    moved = executor.execute(
        _context(
            "work_item.move",
            input_data={
                "work_item_id": activated["work_item_id"],
                "target_state": "in_progress",
                "reasons": ["fix_ci"],
                "note": "Rerun after CI failure.",
            },
        )
    ).output

    assert activated["state"] == "todo"
    assert activated["active_pr_source_item_id"] == source_item.linked_items[0].id
    assert moved["state"] == "in_progress"


def test_fetch_pull_request_records_attempt_pr(tmp_path: Path) -> None:
    store = _store(tmp_path)
    executor_github = FakePrimitiveGitHub()
    store.upsert_issue(
        executor_github.issue("dbcli/litecli", 245).snapshot(default_config().labels, "research")
    )
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="review",
    )
    executor = PrimitiveExecutor(default_config(), store, github=executor_github)

    output = executor.execute(
        _context(
            "github.fetch_pull_request",
            attempt_id=attempt_id,
            input_data={"pull_request_number": 12},
        )
    ).output

    detail = store.attempt_detail(attempt_id)
    assert output["pull_request_url"] == "https://github.com/dbcli/litecli/pull/12"
    assert output["is_merged"] is False
    assert detail is not None
    assert detail["pull_requests"][0]["number"] == 12


def test_find_issue_pull_requests_uses_exact_marker_and_records_link(tmp_path: Path) -> None:
    store = _store(tmp_path)
    github = FakePrimitiveGitHub()
    store.upsert_issue(github.issue("dbcli/litecli", 245).snapshot(default_config().labels, "code"))
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="running",
    )
    executor = PrimitiveExecutor(default_config(), store, github=github)

    output = executor.execute(_context("github.find_issue_pull_requests", attempt_id=attempt_id)).output

    links = store.issue_pull_request_links("dbcli/litecli", 245)
    detail = store.attempt_detail(attempt_id)
    assert output["has_pull_request"] is True
    assert output["pull_request_count"] == 1
    assert output["pull_request_number"] == 12
    assert output["pull_request_head_ref"] == "symphony/existing-pr"
    assert output["pull_request_source_ref"] == "origin/symphony/existing-pr"
    assert len(links) == 1
    assert links[0]["link_source"] == "description_marker"
    assert detail is not None
    assert detail["pull_requests"][0]["number"] == 12


def test_fetch_ci_status_returns_failed_checks(tmp_path: Path) -> None:
    executor = PrimitiveExecutor(default_config(), _store(tmp_path), github=FakePrimitiveGitHub())

    output = executor.execute(
        _context("github.fetch_ci_status", input_data={"pull_request_number": 12})
    ).output

    assert output["sha"] == "abc123"
    assert output["state"] == "failure"
    assert output["failed_checks"] == [
        {
            "name": "tests",
            "status": "completed",
            "conclusion": "failure",
            "url": "https://github.com/dbcli/litecli/actions/runs/1",
        }
    ]


def test_fetch_pr_review_comments_returns_review_and_inline_comments(tmp_path: Path) -> None:
    executor = PrimitiveExecutor(default_config(), _store(tmp_path), github=FakePrimitiveGitHub())

    output = executor.execute(
        _context("github.fetch_pr_review_comments", input_data={"pull_request_number": 12})
    ).output

    assert output["comments"] == [
        {
            "id": 501,
            "url": "https://github.com/dbcli/litecli/pull/12#pullrequestreview-501",
            "body": "Overall this needs a regression test.",
            "author": "reviewer",
            "created_at": "2026-05-24T12:00:00Z",
            "updated_at": "2026-05-24T12:00:00Z",
            "kind": "review",
            "review_id": None,
            "path": "",
            "line": None,
            "original_line": None,
            "side": "",
            "diff_hunk": "",
            "state": "CHANGES_REQUESTED",
        },
        {
            "id": 502,
            "url": "https://github.com/dbcli/litecli/pull/12#discussion_r502",
            "body": "Please cover tilde expansion here.",
            "author": "reviewer",
            "created_at": "2026-05-24T12:01:00Z",
            "updated_at": "2026-05-24T12:01:00Z",
            "kind": "inline",
            "review_id": 501,
            "path": "litecli/main.py",
            "line": 42,
            "original_line": 41,
            "side": "RIGHT",
            "diff_hunk": "@@ -40,6 +40,7 @@",
            "state": "",
        },
    ]


def test_detect_merge_conflicts_returns_mergeability(tmp_path: Path) -> None:
    executor = PrimitiveExecutor(default_config(), _store(tmp_path), github=FakePrimitiveGitHub())

    output = executor.execute(
        _context("github.detect_merge_conflicts", input_data={"pull_request_number": 12})
    ).output

    assert output["pull_request_number"] == 12
    assert output["mergeable"] is False
    assert output["mergeable_state"] == "dirty"
    assert output["has_conflicts"] is True


def test_noop_returns_transition_message(tmp_path: Path) -> None:
    executor = PrimitiveExecutor(default_config(), _store(tmp_path), github=FakePrimitiveGitHub())

    output = executor.execute(_context("workflow.noop")).output

    assert output == {"message": "workflow.noop"}


def test_push_pr_update_commits_and_pushes_existing_pr_branch(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git(worktree, "init")
    (worktree / "README.md").write_text("start\n", encoding="utf-8")
    _git(worktree, "add", "README.md")
    _git(worktree, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    base_sha = _git(worktree, "rev-parse", "HEAD")
    (worktree / "README.md").write_text("fixed\n", encoding="utf-8")
    store = _store(tmp_path)
    attempt_id = _seed_issue_attempt(store, worktree)
    store.update_attempt_workspace(
        attempt_id,
        base_repo_path=str(tmp_path / "repo.git"),
        worktree_path=str(worktree),
        branch="symphony/test",
        commit_sha=base_sha,
    )
    store.record_pr(
        attempt_id,
        repo="dbcli/litecli",
        number=12,
        url="https://github.com/dbcli/litecli/pull/12",
        title="Fix logging path support",
    )
    github = FakePrimitiveGitHub()
    config = replace(default_config(), policy=PolicyConfig(dry_run=False))
    executor = PrimitiveExecutor(config, store, github=github)

    output = executor.execute(
        _context(
            "github.push_pr_update",
            attempt_id=attempt_id,
            worktree_path=str(worktree),
        )
    ).output

    attempt = store.attempt_by_id(attempt_id)
    assert output["pull_request_number"] == 12
    assert output["pushed"] is True
    assert output["commit_sha"] != base_sha
    assert github.pushed_branches == ["symphony/test"]
    assert attempt is not None
    assert attempt["commit_sha"] == output["commit_sha"]


def test_address_pr_comments_runs_codex_with_review_context(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt_id, worktree = _seed_attempt(store, tmp_path)
    prompt_path = tmp_path / "prompt.txt"
    config = _config_with_fake_codex(tmp_path, prompt_path, "Addressed review comments.")
    executor = PrimitiveExecutor(config, store, github=FakePrimitiveGitHub())

    output = executor.execute(
        _context(
            "codex.address_pr_comments",
            attempt_id=attempt_id,
            worktree_path=str(worktree),
            input_data={
                "pull_request_number": 12,
                "comments": [
                    {
                        "author": "reviewer",
                        "body": "Please add a regression test.",
                        "url": "https://github.com/dbcli/litecli/pull/12#discussion_r1",
                    }
                ],
            },
        )
    ).output

    detail = store.attempt_detail(attempt_id)
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "Pull request: https://github.com/dbcli/litecli/pull/12" in prompt
    assert "Please add a regression test." in prompt
    assert output["result_type"] == "pr_review_update"
    assert detail is not None
    assert detail["result"]["result_type"] == "pr_review_update"
    assert detail["result"]["body"] == "Addressed review comments."


def test_fix_ci_failures_runs_codex_with_failed_check_context(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt_id, worktree = _seed_attempt(store, tmp_path)
    prompt_path = tmp_path / "prompt.txt"
    config = _config_with_fake_codex(tmp_path, prompt_path, "Fixed failing CI.")
    executor = PrimitiveExecutor(config, store, github=FakePrimitiveGitHub())

    output = executor.execute(
        _context(
            "codex.fix_ci_failures",
            attempt_id=attempt_id,
            worktree_path=str(worktree),
            input_data={
                "pull_request_number": 12,
                "failed_checks": [{"name": "tests", "conclusion": "failure", "url": "https://ci/1"}],
                "checks": [{"name": "lint", "conclusion": "success"}],
            },
        )
    ).output

    detail = store.attempt_detail(attempt_id)
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "Failed checks:" in prompt
    assert "name=tests" in prompt
    assert "All checks:" in prompt
    assert output["result_type"] == "ci_fix_summary"
    assert detail is not None
    assert detail["result"]["result_type"] == "ci_fix_summary"
    assert detail["result"]["body"] == "Fixed failing CI."


def test_address_pr_feedback_runs_codex_with_combined_pr_context(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt_id, worktree = _seed_attempt(store, tmp_path)
    prompt_path = tmp_path / "prompt.txt"
    config = _config_with_fake_codex(tmp_path, prompt_path, "Addressed PR feedback.")
    executor = PrimitiveExecutor(config, store, github=FakePrimitiveGitHub())

    output = executor.execute(
        _context(
            "codex.address_pr_feedback",
            attempt_id=attempt_id,
            worktree_path=str(worktree),
            input_data={
                "pull_request_number": 12,
                "failed_checks": [{"name": "tests", "conclusion": "failure", "url": "https://ci/1"}],
                "checks": [{"name": "lint", "conclusion": "success"}],
                "comments": [{"body": "Please add a regression test.", "author": "reviewer"}],
                "has_conflicts": True,
                "mergeable_state": "dirty",
            },
        )
    ).output

    detail = store.attempt_detail(attempt_id)
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "Address the pull request feedback below in one focused update." in prompt
    assert "Merge conflicts: yes; mergeable_state=dirty" in prompt
    assert "name=tests" in prompt
    assert "Please add a regression test." in prompt
    assert output["result_type"] == "pr_feedback_update"
    assert detail is not None
    assert detail["result"]["result_type"] == "pr_feedback_update"
    assert detail["result"]["body"] == "Addressed PR feedback."


def test_operations_task_runs_codex_and_records_operation_summary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt_id, worktree = _seed_attempt(store, tmp_path)
    prompt_path = tmp_path / "prompt.txt"
    config = _config_with_fake_codex(tmp_path, prompt_path, "Restarted the local fixture service.")
    executor = PrimitiveExecutor(config, store, github=FakePrimitiveGitHub())

    output = executor.execute(
        _context(
            "codex.operations_task",
            attempt_id=attempt_id,
            worktree_path=str(worktree),
            input_data={"user_hint": "Check why the fixture service is stopped."},
        )
    ).output

    detail = store.attempt_detail(attempt_id)
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "Task type: code" in prompt
    assert output["result_type"] == "operations_summary"
    assert detail is not None
    assert detail["result"]["result_type"] == "operations_summary"
    assert detail["result"]["body"] == "Restarted the local fixture service."


def test_record_workspace_changes_reports_changed_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt_id, worktree = _seed_attempt(store, tmp_path)
    _git(worktree, "init")
    (worktree / "README.md").write_text("start\n", encoding="utf-8")
    _git(worktree, "add", "README.md")
    _git(worktree, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    base_sha = _git(worktree, "rev-parse", "HEAD")
    (worktree / "README.md").write_text("changed\n", encoding="utf-8")

    output = (
        PrimitiveExecutor(default_config(), store, github=FakePrimitiveGitHub())
        .execute(
            _context(
                "workspace.record_changes",
                attempt_id=attempt_id,
                worktree_path=str(worktree),
                input_data={"commit_sha": base_sha},
            )
        )
        .output
    )

    assert output["has_changes"] is True
    assert output["changed_files"] == ["README.md"]
    assert output["uncommitted_files"] == ["README.md"]
    assert output["base_commit_sha"] == base_sha


def test_cleanup_after_merge_removes_managed_worktree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    bare = tmp_path / "repos" / "litecli.git"
    worktree = tmp_path / "worktrees" / "litecli"
    source.mkdir()
    bare.parent.mkdir()
    worktree.parent.mkdir()
    _git(source, "init", "--initial-branch=main")
    (source / "README.md").write_text("start\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    subprocess.run(["git", "clone", "--bare", str(source), str(bare)], check=True, capture_output=True)
    subprocess.run(
        ["git", "--git-dir", str(bare), "worktree", "add", str(worktree), "main"],
        check=True,
        capture_output=True,
    )
    store = _store(tmp_path)
    attempt_id = _seed_issue_attempt(store, worktree)
    store.update_attempt_workspace(
        attempt_id,
        base_repo_path=str(bare),
        worktree_path=str(worktree),
        branch="symphony/dbcli-litecli-245-attempt-1",
        commit_sha=_git(worktree, "rev-parse", "HEAD"),
    )
    store.record_pr(
        attempt_id,
        repo="dbcli/litecli",
        number=12,
        url="https://github.com/dbcli/litecli/pull/12",
        title="Fix logging path support",
        state="closed",
        merged_at="2026-05-24T14:00:00Z",
    )
    config = replace(
        default_config(),
        workspace=WorkspaceConfig(root=str(worktree.parent), bare_repos_root=str(bare.parent)),
    )

    output = (
        PrimitiveExecutor(config, store, github=FakePrimitiveGitHub())
        .execute(
            _context(
                "workspace.cleanup_after_merge",
                attempt_id=attempt_id,
                worktree_path=str(worktree),
                input_data={"base_repo_path": str(bare)},
            )
        )
        .output
    )

    detail = store.attempt_detail(attempt_id)
    assert output == {"worktree_path": str(worktree), "removed": True, "reason": "removed"}
    assert not worktree.exists()
    assert detail is not None
    assert detail["pull_requests"][0]["worktree_cleaned_at"]


class FakePrimitiveGitHub:
    def __init__(self) -> None:
        self.pushed_branches: list[str] = []

    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]:
        return [self.issue(repo, 245)]

    def issue(self, repo: str, issue_number: int) -> GitHubIssue:
        return GitHubIssue(
            repo=repo,
            number=issue_number,
            title="Logging path support",
            body="The log_file option does not expand ~.",
            url=f"https://github.com/{repo}/issues/{issue_number}",
            state="open",
            labels=["symphony:todo", "symphony:type:code"],
            author="amjith",
            updated_at="2026-05-24T11:00:00Z",
        )

    def list_comments(self, repo: str, issue_number: int) -> list[GitHubComment]:
        return [
            GitHubComment(
                id=99,
                url=f"https://github.com/{repo}/issues/{issue_number}#issuecomment-99",
                body="I can reproduce this on 1.12.",
                author="amjith",
                created_at="2026-05-24T10:00:00Z",
                updated_at="2026-05-24T10:00:00Z",
            )
        ]

    def list_pull_request_review_comments(
        self,
        repo: str,
        pull_request_number: int,
    ) -> list[GitHubPullRequestReviewComment]:
        return [
            GitHubPullRequestReviewComment(
                id=501,
                url=f"https://github.com/{repo}/pull/{pull_request_number}#pullrequestreview-501",
                body="Overall this needs a regression test.",
                author="reviewer",
                created_at="2026-05-24T12:00:00Z",
                updated_at="2026-05-24T12:00:00Z",
                kind="review",
                state="CHANGES_REQUESTED",
            ),
            GitHubPullRequestReviewComment(
                id=502,
                url=f"https://github.com/{repo}/pull/{pull_request_number}#discussion_r502",
                body="Please cover tilde expansion here.",
                author="reviewer",
                created_at="2026-05-24T12:01:00Z",
                updated_at="2026-05-24T12:01:00Z",
                kind="inline",
                review_id=501,
                path="litecli/main.py",
                line=42,
                original_line=41,
                side="RIGHT",
                diff_hunk="@@ -40,6 +40,7 @@",
            ),
        ]

    def list_pull_requests(self, repo: str, *, state: str = "open") -> list[PullRequest]:
        return [
            PullRequest(
                number=12,
                url=f"https://github.com/{repo}/pull/12",
                title="Fix logging path support",
                state="open",
                head_sha="abc123",
                head_ref="symphony/existing-pr",
                head_repo=repo,
                body=issue_link_marker(repo, 245),
            ),
            PullRequest(
                number=13,
                url=f"https://github.com/{repo}/pull/13",
                title="Mention issue without marker",
                state="open",
                head_sha="def456",
                head_ref="symphony/unrelated",
                head_repo=repo,
                body="This mentions https://github.com/dbcli/litecli/issues/245 without the marker.",
            ),
        ]

    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> None:
        return

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        return

    def pull_request(self, repo: str, number: int) -> PullRequest:
        return PullRequest(
            number=number,
            url=f"https://github.com/{repo}/pull/{number}",
            title="Fix logging path support",
            state="open",
            head_sha="abc123",
            head_ref="symphony/existing-pr",
            head_repo=repo,
            body=issue_link_marker(repo, 245),
            mergeable=False,
            mergeable_state="dirty",
        )

    def merge_status(self, repo: str, pull_request_number: int) -> PullRequestMergeStatus:
        pull_request = self.pull_request(repo, pull_request_number)
        return PullRequestMergeStatus(
            number=pull_request.number,
            url=pull_request.url,
            title=pull_request.title,
            state=pull_request.state,
            merged_at=pull_request.merged_at,
            head_sha=pull_request.head_sha,
            mergeable=pull_request.mergeable,
            mergeable_state=pull_request.mergeable_state,
            has_conflicts=True,
        )

    def ci_status(self, repo: str, pull_request_number: int) -> GitHubCiStatus:
        failed_check = GitHubCheckRun(
            name="tests",
            status="completed",
            conclusion="failure",
            url=f"https://github.com/{repo}/actions/runs/1",
        )
        return GitHubCiStatus(
            sha="abc123",
            state="failure",
            conclusion="failure",
            failed_checks=[failed_check],
            checks=[failed_check],
        )

    def push_branch(self, *, repo: str, worktree_path: str, branch: str) -> None:
        self.pushed_branches.append(branch)


def _context(
    action: str,
    *,
    attempt_id: int | None = None,
    worktree_path: str = "",
    input_data: dict[str, Any] | None = None,
) -> PrimitiveContext:
    return PrimitiveContext(
        instance_id=1,
        transition_name=action.removeprefix("github."),
        transition=WorkflowTransitionConfig(
            from_state="from",
            to_state="to",
            action=action,
        ),
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        issue_title="Logging path support",
        attempt_id=attempt_id,
        worktree_path=worktree_path,
        input_data=input_data or {},
    )


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "symphony.db")
    store.init()
    return store


def _seed_attempt(store: Store, tmp_path: Path) -> tuple[int, Path]:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    attempt_id = _seed_issue_attempt(store, worktree)
    return attempt_id, worktree


def _seed_issue_attempt(store: Store, worktree: Path) -> int:
    github = FakePrimitiveGitHub()
    store.upsert_issue(github.issue("dbcli/litecli", 245).snapshot(default_config().labels, "research"))
    return store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        worktree_path=str(worktree),
        status="running",
    )


def _config_with_fake_codex(tmp_path: Path, prompt_path: Path, message: str) -> WorkflowConfig:
    command = tmp_path / "fake_codex.py"
    command.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "from pathlib import Path",
                "import sys",
                f"Path({str(prompt_path)!r}).write_text(sys.argv[-1], encoding='utf-8')",
                f"print({message!r})",
            ]
        ),
        encoding="utf-8",
    )
    command.chmod(0o755)
    return replace(
        default_config(),
        codex=CodexConfig(command=str(command), transport="exec"),
    )


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(path), *args], text=True, capture_output=True, check=True)
    return result.stdout.strip()
