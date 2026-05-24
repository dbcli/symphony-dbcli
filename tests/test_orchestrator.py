from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

from symphony_dbcli.config import WorkspaceConfig, default_config
from symphony_dbcli.github import GitHubIssue, PullRequest
from symphony_dbcli.orchestrator import Orchestrator, build_worker_prompt
from symphony_dbcli.store import IssueSnapshot, Store


def test_code_follow_up_prompt_includes_research_context() -> None:
    prompt = build_worker_prompt(
        default_config(),
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        title="Logging support question",
        follow_up_context="Research result:\nExpand log_file before checking its parent directory.",
    )

    assert "Task type: code" in prompt
    assert "Follow-up context:" in prompt
    assert "Expand log_file" in prompt


def test_orchestrator_cleans_worktree_after_pr_merge(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
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
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="review",
    )
    store.update_attempt_workspace(
        attempt_id,
        base_repo_path=str(bare),
        worktree_path=str(worktree),
        branch="symphony/dbcli-litecli-245-attempt-1",
    )
    store.record_pr(
        attempt_id,
        repo="dbcli/litecli",
        number=254,
        url="https://github.com/dbcli/litecli/pull/254",
        title="Fix #245",
    )
    config = replace(
        default_config(),
        workspace=WorkspaceConfig(root=str(worktree.parent), bare_repos_root=str(bare.parent)),
    )

    summary = Orchestrator(config, store, github=FakeCleanupGitHub()).cleanup_merged_pull_request_worktrees()

    detail = store.attempt_detail(attempt_id)
    assert summary.scanned == 1
    assert summary.merged == 1
    assert summary.cleaned == 1
    assert not worktree.exists()
    assert detail is not None
    pull_request = detail["pull_requests"][0]
    assert pull_request["state"] == "closed"
    assert pull_request["merged_at"] == "2026-05-24T14:00:00Z"
    assert pull_request["worktree_cleaned_at"]
    assert detail["timeline"][-1]["event_type"] == "cleaned_after_pr_merge"


def test_orchestrator_skips_open_pr_worktree_cleanup(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        worktree_path=str(tmp_path / "worktrees" / "litecli"),
        base_repo_path=str(tmp_path / "repos" / "litecli.git"),
        status="review",
    )
    store.record_pr(
        attempt_id,
        repo="dbcli/litecli",
        number=254,
        url="https://github.com/dbcli/litecli/pull/254",
        title="Fix #245",
    )
    summary = Orchestrator(
        default_config(), store, github=FakeCleanupGitHub(merged=False)
    ).cleanup_merged_pull_request_worktrees()

    detail = store.attempt_detail(attempt_id)
    assert summary.scanned == 1
    assert summary.skipped == 1
    assert detail is not None
    assert detail["pull_requests"][0]["state"] == "open"
    assert detail["pull_requests"][0]["worktree_cleaned_at"] is None


class FakeCleanupGitHub:
    def __init__(self, *, merged: bool = True) -> None:
        self.merged = merged

    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]:
        return []

    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> None:
        return

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        return

    def pull_request(self, repo: str, number: int) -> PullRequest:
        if not self.merged:
            return PullRequest(
                number=number,
                url=f"https://github.com/{repo}/pull/{number}",
                title="Fix #245",
                state="open",
            )
        return PullRequest(
            number=number,
            url=f"https://github.com/{repo}/pull/{number}",
            title="Fix #245",
            state="closed",
            merged_at="2026-05-24T14:00:00Z",
        )


def _seed_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "symphony.db")
    store.init()
    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/litecli",
            number=245,
            title="Logging support question",
            url="https://github.com/dbcli/litecli/issues/245",
            state="open",
            labels=["symphony:todo"],
            task_type="code",
        )
    )
    return store


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)
