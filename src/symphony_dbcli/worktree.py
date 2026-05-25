from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .clock import elapsed_ms, monotonic_ns
from .config import WorkspaceConfig
from .workflow_definition import SetupConfig


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


@dataclass(frozen=True)
class WorkspaceChangeSummary:
    worktree_path: str
    base_commit_sha: str
    head_commit_sha: str
    changed_files: list[str]
    uncommitted_files: list[str]
    commit_count: int
    has_changes: bool


@dataclass(frozen=True)
class SetupStepResult:
    name: str
    command: list[str]
    status: Literal["succeeded", "failed", "skipped"]
    exit_code: int | None
    stdout_excerpt: str
    stderr_excerpt: str
    duration_ms: int
    blocks_worker: bool


SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
OUTPUT_EXCERPT_LIMIT = 4000


class WorktreeManager:
    def __init__(self, config: WorkspaceConfig):
        self.config = config

    def allocate(
        self,
        repo: str,
        issue_number: int,
        attempt_id: int,
        *,
        branch_name: str = "",
        source_ref: str = "",
    ) -> WorktreeAllocation:
        base_repo_path = self.base_repo_path(repo)
        worktree_path = self.worktree_path(repo, issue_number, attempt_id)
        branch = branch_name or self.branch_name(repo, issue_number, attempt_id)
        clone_url = f"https://github.com/{repo}.git"

        self._ensure_base_repo(base_repo_path, clone_url)
        remote_ref = source_ref or self._default_remote_ref(base_repo_path)
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

    def record_changes(self, *, worktree_path: str, base_commit_sha: str) -> WorkspaceChangeSummary:
        path = Path(worktree_path)
        if not worktree_path or not path.exists():
            raise WorktreeError(f"Workspace does not exist: {worktree_path}")
        head_sha = self._run(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()
        committed_files = self._committed_changed_files(path, base_commit_sha)
        uncommitted_files = self._status_files(path)
        changed_files = sorted(set(committed_files) | set(uncommitted_files))
        return WorkspaceChangeSummary(
            worktree_path=worktree_path,
            base_commit_sha=base_commit_sha,
            head_commit_sha=head_sha,
            changed_files=changed_files,
            uncommitted_files=uncommitted_files,
            commit_count=self._commit_count(path, base_commit_sha),
            has_changes=bool(changed_files) or (bool(base_commit_sha) and head_sha != base_commit_sha),
        )

    def run_setup(self, worktree_path: str, setup: SetupConfig) -> list[SetupStepResult]:
        if not setup.enabled:
            return []
        cwd = Path(worktree_path)
        if not cwd.exists():
            raise WorktreeError(f"Workspace does not exist: {worktree_path}")
        results: list[SetupStepResult] = []
        for name, step in setup.steps.items():
            if step.run == "manual":
                results.append(
                    SetupStepResult(
                        name=name,
                        command=step.command,
                        status="skipped",
                        exit_code=None,
                        stdout_excerpt="",
                        stderr_excerpt="manual setup step",
                        duration_ms=0,
                        blocks_worker=step.blocks_worker,
                    )
                )
                continue
            started = monotonic_ns()
            try:
                result = subprocess.run(
                    step.command,
                    cwd=str(cwd),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=step.timeout_seconds,
                )
                ended = monotonic_ns()
                results.append(
                    SetupStepResult(
                        name=name,
                        command=step.command,
                        status="succeeded" if result.returncode == 0 else "failed",
                        exit_code=result.returncode,
                        stdout_excerpt=_excerpt(result.stdout),
                        stderr_excerpt=_excerpt(result.stderr),
                        duration_ms=elapsed_ms(started, ended),
                        blocks_worker=step.blocks_worker,
                    )
                )
            except subprocess.TimeoutExpired as exc:
                ended = monotonic_ns()
                results.append(
                    SetupStepResult(
                        name=name,
                        command=step.command,
                        status="failed",
                        exit_code=None,
                        stdout_excerpt=_excerpt(_timeout_output(exc.stdout)),
                        stderr_excerpt=_excerpt(_timeout_output(exc.stderr) or "setup step timed out"),
                        duration_ms=elapsed_ms(started, ended),
                        blocks_worker=step.blocks_worker,
                    )
                )
        return results

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

    def _committed_changed_files(self, path: Path, base_commit_sha: str) -> list[str]:
        if not base_commit_sha:
            return []
        result = self._run(
            ["git", "-C", str(path), "diff", "--name-only", f"{base_commit_sha}...HEAD"],
            check=False,
        )
        if result.returncode != 0:
            result = self._run(
                ["git", "-C", str(path), "diff", "--name-only", f"{base_commit_sha}..HEAD"],
                check=False,
            )
        return sorted(line for line in result.stdout.splitlines() if line)

    def _status_files(self, path: Path) -> list[str]:
        result = self._run(["git", "-C", str(path), "status", "--porcelain"])
        return sorted(_porcelain_path(line) for line in result.stdout.splitlines() if line)

    def _commit_count(self, path: Path, base_commit_sha: str) -> int:
        if not base_commit_sha:
            return 0
        result = self._run(["git", "-C", str(path), "rev-list", "--count", f"{base_commit_sha}..HEAD"])
        return int(result.stdout.strip() or "0")


def safe_key(value: str) -> str:
    cleaned = SAFE_RE.sub("_", value.strip()).strip("_")
    return cleaned or "repo"


def _excerpt(value: str) -> str:
    if len(value) <= OUTPUT_EXCERPT_LIMIT:
        return value
    return value[-OUTPUT_EXCERPT_LIMIT:]


def _timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _porcelain_path(line: str) -> str:
    path = line[3:]
    if " -> " in path:
        return path.rsplit(" -> ", 1)[1]
    return path
