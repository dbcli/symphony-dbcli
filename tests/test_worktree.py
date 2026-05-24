from __future__ import annotations

from pathlib import Path

from symphony_dbcli.config import WorkspaceConfig
from symphony_dbcli.worktree import WorktreeManager, safe_key


def test_safe_key_and_branch_names_are_deterministic() -> None:
    manager = WorktreeManager(WorkspaceConfig(root="/worktrees", bare_repos_root="/repos"))

    assert safe_key("dbcli/pgcli") == "dbcli_pgcli"
    assert manager.branch_name("dbcli/pgcli", 123, 2) == "symphony/dbcli-pgcli-123-attempt-2"
    assert str(manager.worktree_path("dbcli/pgcli", 123, 2)) == "/worktrees/dbcli_pgcli_123_attempt_2"


def test_default_remote_ref_accepts_bare_clone_head(tmp_path: Path) -> None:
    base_repo = tmp_path / "repo.git"
    manager = WorktreeManager(WorkspaceConfig(root="/worktrees", bare_repos_root="/repos"))
    manager._run(["git", "init", "--bare", "--initial-branch=main", str(base_repo)])

    assert manager._default_remote_ref(base_repo) == "main"
