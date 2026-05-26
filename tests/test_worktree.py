from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from symphony_dbcli.config import WorkspaceConfig
from symphony_dbcli.workflow_definition import SetupConfig, SetupStepConfig
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


def test_source_ref_resolves_origin_branch_in_bare_clone(tmp_path: Path) -> None:
    source = tmp_path / "source"
    bare = tmp_path / "repo.git"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    (source / "README.md").write_text("start\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    _git(source, "checkout", "-b", "symphony/existing-pr")
    (source / "README.md").write_text("branch\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "branch")
    subprocess.run(["git", "clone", "--bare", str(source), str(bare)], check=True, capture_output=True)
    manager = WorktreeManager(WorkspaceConfig(root="/worktrees", bare_repos_root="/repos"))

    assert manager._resolve_source_ref(bare, "origin/symphony/existing-pr") == "symphony/existing-pr"


def test_allocate_reuses_managed_worktree_for_existing_pr_branch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    bare = tmp_path / "repos" / "dbcli_litecli.git"
    existing_worktree = tmp_path / "worktrees" / "dbcli_litecli_245_attempt_10"
    source.mkdir()
    bare.parent.mkdir()
    existing_worktree.parent.mkdir()
    _git(source, "init", "--initial-branch=main")
    (source / "README.md").write_text("start\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    _git(source, "checkout", "-b", "symphony/existing-pr")
    (source / "README.md").write_text("branch\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "branch")
    subprocess.run(["git", "clone", "--bare", str(source), str(bare)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(bare),
            "worktree",
            "add",
            str(existing_worktree),
            "symphony/existing-pr",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    manager = WorktreeManager(
        WorkspaceConfig(root=str(existing_worktree.parent), bare_repos_root=str(bare.parent))
    )

    allocation = manager.allocate(
        "dbcli/litecli",
        245,
        12,
        branch_name="symphony/existing-pr",
        source_ref="origin/symphony/existing-pr",
    )

    assert allocation.worktree_path == str(existing_worktree)
    assert allocation.branch == "symphony/existing-pr"
    assert allocation.reused_existing is True
    assert not manager.worktree_path("dbcli/litecli", 245, 12).exists()


def test_remove_worktree_removes_clean_managed_worktree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    bare = tmp_path / "repos" / "repo.git"
    worktree = tmp_path / "worktrees" / "repo"
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
    manager = WorktreeManager(WorkspaceConfig(root=str(worktree.parent), bare_repos_root=str(bare.parent)))

    removal = manager.remove_worktree(base_repo_path=str(bare), worktree_path=str(worktree))

    assert removal.removed is True
    assert removal.reason == "removed"
    assert not worktree.exists()


def test_run_setup_records_command_results(tmp_path: Path) -> None:
    manager = WorktreeManager(WorkspaceConfig(root="/worktrees", bare_repos_root="/repos"))
    setup = SetupConfig(
        steps={
            "install": SetupStepConfig(
                command=[sys.executable, "-c", "print('ready')"],
                timeout_seconds=10,
            ),
            "manual_seed": SetupStepConfig(
                command=[sys.executable, "-c", "raise SystemExit(1)"],
                run="manual",
            ),
        }
    )

    results = manager.run_setup(str(tmp_path), setup)

    assert len(results) == 2
    assert results[0].name == "install"
    assert results[0].status == "succeeded"
    assert results[0].exit_code == 0
    assert results[0].stdout_excerpt.strip() == "ready"
    assert results[1].name == "manual_seed"
    assert results[1].status == "skipped"
    assert results[1].stderr_excerpt == "manual setup step"


def test_run_setup_records_blocking_failures(tmp_path: Path) -> None:
    manager = WorktreeManager(WorkspaceConfig(root="/worktrees", bare_repos_root="/repos"))
    setup = SetupConfig(
        steps={
            "fail": SetupStepConfig(
                command=[
                    sys.executable,
                    "-c",
                    "import sys; print('bad', file=sys.stderr); raise SystemExit(3)",
                ],
                timeout_seconds=10,
                blocks_worker=True,
            )
        }
    )

    results = manager.run_setup(str(tmp_path), setup)

    assert results[0].status == "failed"
    assert results[0].exit_code == 3
    assert results[0].stderr_excerpt.strip() == "bad"
    assert results[0].blocks_worker is True


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)
