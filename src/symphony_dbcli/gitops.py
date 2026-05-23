from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass


class GitError(RuntimeError):
    """Raised when local git operations fail."""


@dataclass(frozen=True)
class CommitResult:
    sha: str
    message: str


class GitWorktree:
    def __init__(self, path: str):
        self.path = path

    def has_changes(self) -> bool:
        return bool(self._run(["status", "--porcelain"]).stdout.strip())

    def head_sha(self) -> str:
        return self._run(["rev-parse", "HEAD"]).stdout.strip()

    def commits_since(self, base_sha: str) -> int:
        count = self._run(["rev-list", "--count", f"{base_sha}..HEAD"]).stdout.strip()
        return int(count)

    def commit_all(self, message: str) -> CommitResult:
        self._run(["add", "-A"])
        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", "symphony-dbcli")
        env.setdefault("GIT_AUTHOR_EMAIL", "symphony-dbcli@users.noreply.github.com")
        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
        self._run(["commit", "-m", message], env=env)
        sha = self.head_sha()
        return CommitResult(sha=sha, message=message)

    def _run(self, args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", self.path, *args],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result
