from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from symphony_dbcli.config import default_config
from symphony_dbcli.github import PullRequest
from symphony_dbcli.review_actions import (
    ReviewActions,
    build_commit_message,
    build_draft_pr_content,
    issue_link_marker,
)
from symphony_dbcli.store import IssueSnapshot, Store


class FakeGitHub:
    def __init__(self) -> None:
        self.pushed_branches: list[tuple[str, str, str]] = []
        self.pull_request_body = ""
        self.pull_request_title = ""
        self.comments: list[tuple[str, int, str]] = []

    def create_comment(self, repo: str, issue_number: int, body: str) -> str:
        self.comments.append((repo, issue_number, body))
        return f"https://github.com/{repo}/issues/{issue_number}#issuecomment-1"

    def create_pull_request(
        self,
        *,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str,
        draft: bool = True,
    ) -> PullRequest:
        self.pull_request_title = title
        self.pull_request_body = body
        assert draft is True
        assert base == "main"
        assert head == "symphony/dbcli-litecli-245-attempt-1"
        return PullRequest(number=7, url=f"https://github.com/{repo}/pull/7", title=title)

    def default_branch(self, repo: str) -> str:
        assert repo == "dbcli/litecli"
        return "main"

    def push_branch(self, *, repo: str, worktree_path: str, branch: str) -> None:
        self.pushed_branches.append((repo, worktree_path, branch))


class FailingPushGitHub(FakeGitHub):
    def push_branch(self, *, repo: str, worktree_path: str, branch: str) -> None:
        raise RuntimeError("push failed")


def test_review_actions_create_draft_pr_from_code_attempt(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    repo = tmp_path / "litecli"
    repo.mkdir()
    _git(repo, "init")
    (repo / "README.md").write_text("start\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    _git(repo, "checkout", "-b", "symphony/dbcli-litecli-245-attempt-1")
    base_sha = _git(repo, "rev-parse", "HEAD")

    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="review",
    )
    store.update_attempt_workspace(
        attempt_id,
        base_repo_path=str(repo),
        worktree_path=str(repo),
        branch="symphony/dbcli-litecli-245-attempt-1",
        commit_sha=base_sha,
    )
    store.record_worker_result(
        attempt_id=attempt_id,
        repo="dbcli/litecli",
        issue_number=245,
        result_type="code_changes",
        title="Code Changes",
        body=(
            "I'll verify the issue context first.\n"
            "\n"
            "Summary:\n"
            "- Expanded configured `log_file` paths before directory checks in [litecli/main.py](/Users/amjith/litecli/main.py:252).\n"
            "- Added a regression test for `~/.cache/litecli/log`.\n"
            "\n"
            "Checks run:\n"
            "- `python -m py_compile litecli/main.py tests/test_main.py` passed.\n"
            "- `ruff check litecli/main.py tests/test_main.py` passed.\n"
        ),
    )
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    github = FakeGitHub()

    pr = ReviewActions(default_config(), store, github=github).create_draft_pr(attempt_id)

    detail = store.attempt_detail(attempt_id)
    assert pr.number == 7
    assert detail is not None
    assert detail["attempt"]["outcome"] == "draft_pr_created"
    assert detail["pull_requests"][0]["url"] == "https://github.com/dbcli/litecli/pull/7"
    assert _git(repo, "log", "-1", "--pretty=%s") == "Fix #245: Support tilde paths in log_file"
    assert github.pushed_branches == [("dbcli/litecli", str(repo), "symphony/dbcli-litecli-245-attempt-1")]
    assert github.pull_request_title == "Fix #245: Support tilde paths in log_file"
    assert "Fixes https://github.com/dbcli/litecli/issues/245" in github.pull_request_body
    assert issue_link_marker("dbcli/litecli", 245) in github.pull_request_body
    assert "## Changes" in github.pull_request_body
    assert "## Tests" in github.pull_request_body
    assert (
        "Expanded configured `log_file` paths before directory checks in litecli/main.py"
        in github.pull_request_body
    )
    assert "## Issue" not in github.pull_request_body
    assert "/Users/amjith" not in github.pull_request_body
    assert "`ruff check litecli/main.py tests/test_main.py` passed." in github.pull_request_body
    assert "I'll verify the issue context first" not in github.pull_request_body
    assert "Worker Notes" not in github.pull_request_body


def test_review_actions_push_pr_update_retries_existing_local_commit(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    repo = tmp_path / "litecli"
    repo.mkdir()
    _git(repo, "init")
    (repo / "README.md").write_text("start\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    _git(repo, "checkout", "-b", "symphony/dbcli-litecli-245-attempt-1")
    (repo / "README.md").write_text("fixed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "fix")
    head_sha = _git(repo, "rev-parse", "HEAD")
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="review",
    )
    store.update_attempt_workspace(
        attempt_id,
        base_repo_path=str(repo),
        worktree_path=str(repo),
        branch="symphony/dbcli-litecli-245-attempt-1",
        commit_sha=head_sha,
    )
    store.record_pr(
        attempt_id,
        repo="dbcli/litecli",
        number=7,
        url="https://github.com/dbcli/litecli/pull/7",
        title="Fix logging path support",
    )
    github = FakeGitHub()

    update = ReviewActions(default_config(), store, github=github).push_pr_update(attempt_id)

    assert update.pushed is True
    assert update.commit_sha == head_sha
    assert github.pushed_branches == [("dbcli/litecli", str(repo), "symphony/dbcli-litecli-245-attempt-1")]


def test_review_actions_push_pr_update_does_not_advance_attempt_when_push_fails(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    repo = tmp_path / "litecli"
    repo.mkdir()
    _git(repo, "init")
    (repo / "README.md").write_text("start\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    _git(repo, "checkout", "-b", "symphony/dbcli-litecli-245-attempt-1")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("fixed\n", encoding="utf-8")
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="review",
    )
    store.update_attempt_workspace(
        attempt_id,
        base_repo_path=str(repo),
        worktree_path=str(repo),
        branch="symphony/dbcli-litecli-245-attempt-1",
        commit_sha=base_sha,
    )
    store.record_pr(
        attempt_id,
        repo="dbcli/litecli",
        number=7,
        url="https://github.com/dbcli/litecli/pull/7",
        title="Fix logging path support",
    )

    with pytest.raises(RuntimeError, match="push failed"):
        ReviewActions(default_config(), store, github=FailingPushGitHub()).push_pr_update(attempt_id)

    attempt = store.attempt_by_id(attempt_id)
    assert attempt is not None
    assert attempt["commit_sha"] == base_sha


def test_build_draft_pr_content_uses_issue_title_for_human_title() -> None:
    content = build_draft_pr_content(
        "dbcli/litecli",
        245,
        "Summary:\n- Expanded configured `log_file` paths before directory checks.",
        issue_title="Getting error 'Unable to open log file' when log file move to other directory other than default",
    )

    assert content.title == "Fix #245: Unable to open log file outside default directory"
    assert content.body.startswith("## Changes\n\nExpanded configured `log_file` paths")
    assert "## Issue" not in content.body


def test_build_draft_pr_content_uses_worker_pr_title_and_body() -> None:
    content = build_draft_pr_content(
        "dbcli/litecli",
        245,
        """\
Summary:
- Expanded configured log paths.

PR title: Expand log_file paths before validation

PR body:
## Changes

- Expands configured log file paths before parent directory checks.
- Adds regression coverage for the moved log file case.

## Tests

- `pytest tests/test_main.py` passed.
""",
        issue_title="Logging path support",
    )

    assert content.title == "Expand log_file paths before validation"
    assert content.body.startswith("## Changes\n\n- Expands configured log file paths")
    assert "Fixes https://github.com/dbcli/litecli/issues/245" in content.body
    assert issue_link_marker("dbcli/litecli", 245) in content.body


def test_build_draft_pr_content_strips_outer_markdown_fence() -> None:
    content = build_draft_pr_content(
        "dbcli/litecli",
        236,
        """\
PR title: Add readonly database open support

PR body:
```md
Fixes https://github.com/dbcli/litecli/issues/236

<!-- symphony-dbcli:issue-link=https://github.com/dbcli/litecli/issues/236 -->
```
""",
    )

    assert content.title == "Add readonly database open support"
    assert content.body.startswith("Fixes https://github.com/dbcli/litecli/issues/236")
    assert "```" not in content.body
    assert issue_link_marker("dbcli/litecli", 236) in content.body


def test_build_commit_message_uses_issue_or_worker_summary() -> None:
    issue_message = build_commit_message(
        245,
        "Summary:\n- Adjusted logfile handling.",
        issue_title="Getting error 'Unable to open log file' when log file move to other directory other than default",
    )
    summary_message = build_commit_message(
        245,
        "Summary:\n- Added regression coverage for expanded log_file paths.",
    )

    assert issue_message == "Fix #245: Unable to open log file outside default directory"
    assert summary_message == "Fix #245: Added regression coverage for expanded log_file paths"


def test_review_actions_create_draft_pr_uses_edited_content(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    repo = tmp_path / "litecli"
    repo.mkdir()
    _git(repo, "init")
    (repo / "README.md").write_text("start\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    _git(repo, "checkout", "-b", "symphony/dbcli-litecli-245-attempt-1")
    base_sha = _git(repo, "rev-parse", "HEAD")
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="review",
    )
    store.update_attempt_workspace(
        attempt_id,
        base_repo_path=str(repo),
        worktree_path=str(repo),
        branch="symphony/dbcli-litecli-245-attempt-1",
        commit_sha=base_sha,
    )
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    github = FakeGitHub()

    ReviewActions(default_config(), store, github=github).create_draft_pr(
        attempt_id,
        title="Fix logging path expansion",
        body="Fixes https://github.com/dbcli/litecli/issues/245\n\nEdited description.",
    )

    assert github.pull_request_title == "Fix logging path expansion"
    assert github.pull_request_body.startswith(
        "Fixes https://github.com/dbcli/litecli/issues/245\n\nEdited description."
    )
    assert issue_link_marker("dbcli/litecli", 245) in github.pull_request_body


def test_review_actions_post_edited_github_comment(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="research",
        workflow_version_id=None,
        status="review",
    )
    store.record_comment(
        attempt_id,
        repo="dbcli/litecli",
        issue_number=245,
        url="",
        body="original draft",
        status="drafted",
    )
    draft_detail = store.attempt_detail(attempt_id)
    assert draft_detail is not None
    comment_id = int(draft_detail["comments"][0]["id"])
    github = FakeGitHub()

    posted = ReviewActions(default_config(), store, github=github).post_comment(comment_id, "edited body")

    comment = store.comment_by_id(comment_id)
    detail = store.attempt_detail(attempt_id)
    assert posted.url == "https://github.com/dbcli/litecli/issues/245#issuecomment-1"
    assert github.comments == [("dbcli/litecli", 245, "edited body")]
    assert comment is not None
    assert comment["status"] == "posted"
    assert comment["body"] == "edited body"
    assert comment["url"] == posted.url
    assert detail is not None
    assert detail["timeline"][-1]["event_type"] == "comment_posted"


def _seed_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "symphony.db")
    store.init()
    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/litecli",
            number=245,
            title="log_file should support tilde paths",
            url="https://github.com/dbcli/litecli/issues/245",
            state="open",
            labels=["symphony:todo", "symphony:type:code"],
            task_type="code",
        )
    )
    return store


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
