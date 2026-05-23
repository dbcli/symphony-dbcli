from __future__ import annotations

import subprocess
from pathlib import Path

from symphony_dbcli.gitops import GitWorktree


def test_git_worktree_detects_dirty_and_committed_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "README.md").write_text("start\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "initial")

    worktree = GitWorktree(str(repo))
    base_sha = worktree.head_sha()
    assert worktree.commits_since(base_sha) == 0

    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    assert worktree.has_changes()

    commit = worktree.commit_all("change")
    assert commit.sha == worktree.head_sha()
    assert not worktree.has_changes()
    assert worktree.commits_since(base_sha) == 1


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)
