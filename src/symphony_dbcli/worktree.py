from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import WorkspaceConfig


class WorktreeError(RuntimeError):
    """Raised when git worktree allocation fails."""


@dataclass(frozen=True)
class WorktreeAllocation:
    repo: str
    attempt_id: int
    base_repo_path: str
    worktree_path: str
    branch: str
    commit_sha: str


@dataclass(frozen=True)
class WorktreeRemoval:
    worktree_path: str
    removed: bool
    reason: str


SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class WorktreeManager:
    def __init__(self, config: WorkspaceConfig):
        self.config = config

    def allocate(self, repo: str, issue_number: int, attempt_id: int) -> WorktreeAllocation:
        base_repo_path = self.base_repo_path(repo)
        worktree_path = self.worktree_path(repo, issue_number, attempt_id)
        branch = self.branch_name(repo, issue_number, attempt_id)
        clone_url = f"https://github.com/{repo}.git"

        self._ensure_base_repo(base_repo_path, clone_url)
        remote_ref = self._default_remote_ref(base_repo_path)
        Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                "git",
                "--git-dir",
                str(base_repo_path),
                "worktree",
                "add",
                "-B",
                branch,
                str(worktree_path),
                remote_ref,
            ]
        )
        commit_sha = self._run(["git", "-C", str(worktree_path), "rev-parse", "HEAD"]).stdout.strip()
        return WorktreeAllocation(
            repo=repo,
            attempt_id=attempt_id,
            base_repo_path=str(base_repo_path),
            worktree_path=str(worktree_path),
            branch=branch,
            commit_sha=commit_sha,
        )

    def base_repo_path(self, repo: str) -> Path:
        return Path(self.config.bare_repos_root) / f"{safe_key(repo)}.git"

    def worktree_path(self, repo: str, issue_number: int, attempt_id: int) -> Path:
        name = f"{safe_key(repo)}_{issue_number}_attempt_{attempt_id}"
        return Path(self.config.root) / name

    def branch_name(self, repo: str, issue_number: int, attempt_id: int) -> str:
        owner, name = repo.split("/", 1)
        prefix = self.config.branch_prefix.strip("/")
        return f"{prefix}/{safe_key(owner)}-{safe_key(name)}-{issue_number}-attempt-{attempt_id}"

    def cleanup_prunable(self) -> str:
        root = Path(self.config.bare_repos_root)
        if not root.exists():
            return "No shared repositories found."
        outputs: list[str] = []
        for base in sorted(root.glob("*.git")):
            result = self._run(["git", "--git-dir", str(base), "worktree", "prune"], check=False)
            outputs.append(result.stdout.strip() or f"Pruned {base}")
        return "\n".join(outputs)

    def remove_worktree(self, *, base_repo_path: str, worktree_path: str) -> WorktreeRemoval:
        path = Path(worktree_path)
        if not worktree_path:
            raise WorktreeError("Attempt does not have a worktree path.")
        self._ensure_managed_worktree(path)
        base = Path(base_repo_path)
        if not base_repo_path or not base.exists():
            raise WorktreeError("Attempt does not have an existing shared repository path.")
        if not path.exists():
            self._run(["git", "--git-dir", str(base), "worktree", "prune"], check=False)
            return WorktreeRemoval(worktree_path=worktree_path, removed=False, reason="already_missing")
        status = self._run(["git", "-C", str(path), "status", "--porcelain"]).stdout.strip()
        if status:
            raise WorktreeError("Worktree has uncommitted changes; skipping cleanup.")
        self._run(["git", "--git-dir", str(base), "worktree", "remove", str(path)])
        self._run(["git", "--git-dir", str(base), "worktree", "prune"], check=False)
        return WorktreeRemoval(worktree_path=worktree_path, removed=True, reason="removed")

    def _ensure_base_repo(self, path: Path, clone_url: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            self._run(["git", "--git-dir", str(path), "fetch", "--prune", "origin"])
            return
        self._run(["git", "clone", "--bare", clone_url, str(path)])
        self._run(["git", "--git-dir", str(path), "remote", "set-head", "origin", "--auto"], check=False)

    def _default_remote_ref(self, base_repo_path: Path) -> str:
        result = self._run(
            ["git", "--git-dir", str(base_repo_path), "symbolic-ref", "refs/remotes/origin/HEAD"],
            check=False,
        )
        ref = result.stdout.strip()
        if ref.startswith("refs/remotes/"):
            return ref.removeprefix("refs/remotes/")
        for candidate in ("origin/main", "origin/master"):
            exists = self._run(
                ["git", "--git-dir", str(base_repo_path), "rev-parse", "--verify", candidate],
                check=False,
            )
            if exists.returncode == 0:
                return candidate
        head = self._run(["git", "--git-dir", str(base_repo_path), "symbolic-ref", "HEAD"], check=False)
        if head.returncode == 0 and head.stdout.strip().startswith("refs/heads/"):
            return head.stdout.strip().removeprefix("refs/heads/")
        for candidate in ("main", "master"):
            exists = self._run(
                ["git", "--git-dir", str(base_repo_path), "rev-parse", "--verify", candidate],
                check=False,
            )
            if exists.returncode == 0:
                return candidate
        raise WorktreeError(f"Could not find a default branch for {base_repo_path}.")

    def _ensure_managed_worktree(self, path: Path) -> None:
        root = Path(self.config.root).resolve()
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise WorktreeError(f"Refusing to clean unmanaged worktree path: {path}") from exc

    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(args, text=True, capture_output=True, check=False)
        if check and result.returncode != 0:
            raise WorktreeError(result.stderr.strip() or "git command failed")
        return result


def safe_key(value: str) -> str:
    cleaned = SAFE_RE.sub("_", value.strip()).strip("_")
    return cleaned or "repo"
