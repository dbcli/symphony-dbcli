from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from .config import WorkflowConfig
from .github import GitHubClient, PullRequest
from .gitops import GitWorktree
from .store import Store


class ReviewActionError(RuntimeError):
    """Raised when a manual review action cannot be completed."""


class GitHubReviewClient(Protocol):
    def create_comment(self, repo: str, issue_number: int, body: str) -> str: ...

    def create_pull_request(
        self,
        *,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str,
        draft: bool = True,
    ) -> PullRequest: ...

    def default_branch(self, repo: str) -> str: ...

    def push_branch(self, *, repo: str, worktree_path: str, branch: str) -> None: ...


ISSUE_LINK_MARKER_PREFIX = "symphony-dbcli:issue-link="


@dataclass(frozen=True)
class PostedComment:
    url: str
    attempt_id: int | None
    repo: str
    issue_number: int


@dataclass(frozen=True)
class DraftPullRequestContent:
    title: str
    body: str


@dataclass(frozen=True)
class PullRequestBranchUpdate:
    number: int
    url: str
    title: str
    branch: str
    commit_sha: str
    pushed: bool


class ReviewActions:
    def __init__(
        self,
        config: WorkflowConfig,
        store: Store,
        *,
        github: GitHubReviewClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.github = github or GitHubClient(config.github)

    def create_draft_pr(self, attempt_id: int, *, title: str = "", body: str = "") -> PullRequest:
        attempt = self.store.attempt_by_id(attempt_id)
        if not attempt:
            raise ReviewActionError(f"Attempt {attempt_id} does not exist.")
        if str(attempt["task_type"]) != "code":
            raise ReviewActionError("Only code attempts can create draft pull requests.")
        if str(attempt["status"]) != "review":
            raise ReviewActionError("Only attempts in review can create draft pull requests.")
        existing = self.store.pull_requests_for_attempt(attempt_id)
        if existing:
            row = existing[0]
            return PullRequest(number=int(row["number"]), url=str(row["url"]), title=str(row["title"]))

        repo = str(attempt["repo"])
        issue_number = int(attempt["issue_number"])
        worktree_path = str(attempt["worktree_path"])
        branch = str(attempt["branch"])
        base_commit = str(attempt["commit_sha"] or "")
        if not worktree_path or not branch or not base_commit:
            raise ReviewActionError("Attempt does not have a complete worktree, branch, and base commit.")

        worktree = GitWorktree(worktree_path)
        commit_sha = self._ensure_commit(
            attempt_id,
            repo,
            issue_number,
            worktree,
            base_commit,
            require_changes=True,
        )
        self.store.update_attempt_workspace(
            attempt_id,
            base_repo_path=str(attempt["base_repo_path"]),
            worktree_path=worktree_path,
            branch=branch,
            commit_sha=commit_sha,
        )

        self.github.push_branch(repo=repo, worktree_path=worktree_path, branch=branch)
        self.store.record_timeline_event(
            attempt_id,
            phase="github",
            event_type="branch_pushed",
            message=branch,
        )
        result = self.store.worker_result_for_attempt(attempt_id)
        result_body = str(result["body"]) if result else ""
        issue = self.store.issue_detail(repo, issue_number)
        issue_title = str(issue["issue"]["title"]) if issue else ""
        content = build_draft_pr_content(repo, issue_number, result_body, issue_title=issue_title)
        pr_title = title.strip() or content.title
        pr_body = ensure_issue_link_marker(body.strip() or content.body, repo, issue_number)
        pr = self.github.create_pull_request(
            repo=repo,
            title=pr_title,
            head=branch,
            base=self.github.default_branch(repo),
            body=pr_body,
            draft=True,
        )
        self.store.record_pr(
            attempt_id,
            repo,
            pr.number,
            pr.url,
            pr.title,
            state=pr.state,
            merged_at=pr.merged_at,
        )
        self.store.record_issue_pull_request_link(
            repo=repo,
            issue_number=issue_number,
            pull_request_number=pr.number,
            pull_request_url=pr.url,
            pull_request_title=pr.title,
            state=pr.state,
            link_source="created_by_symphony",
            marker=issue_link_marker(repo, issue_number),
        )
        self.store.update_attempt_outcome(attempt_id, "draft_pr_created")
        self.store.record_timeline_event(
            attempt_id,
            phase="github",
            event_type="pull_request_created",
            message=pr.url,
            data={"number": pr.number},
        )
        return pr

    def push_pr_update(self, attempt_id: int) -> PullRequestBranchUpdate:
        attempt = self.store.attempt_by_id(attempt_id)
        if not attempt:
            raise ReviewActionError(f"Attempt {attempt_id} does not exist.")
        if str(attempt["task_type"]) != "code":
            raise ReviewActionError("Only code attempts can update pull request branches.")
        existing = self.store.pull_requests_for_attempt(attempt_id)
        if not existing:
            raise ReviewActionError("Attempt does not have an existing pull request.")

        repo = str(attempt["repo"])
        issue_number = int(attempt["issue_number"])
        worktree_path = str(attempt["worktree_path"])
        branch = str(attempt["branch"])
        base_commit = str(attempt["commit_sha"] or "")
        if not worktree_path or not branch or not base_commit:
            raise ReviewActionError("Attempt does not have a complete worktree, branch, and base commit.")

        pull_request = existing[0]
        commit_sha = self._ensure_commit(
            attempt_id,
            repo,
            issue_number,
            GitWorktree(worktree_path),
            base_commit,
            require_changes=False,
        )
        if not commit_sha:
            return PullRequestBranchUpdate(
                number=int(pull_request["number"]),
                url=str(pull_request["url"]),
                title=str(pull_request["title"]),
                branch=branch,
                commit_sha=base_commit,
                pushed=False,
            )

        self.store.update_attempt_workspace(
            attempt_id,
            base_repo_path=str(attempt["base_repo_path"]),
            worktree_path=worktree_path,
            branch=branch,
            commit_sha=commit_sha,
        )
        self.github.push_branch(repo=repo, worktree_path=worktree_path, branch=branch)
        self.store.record_timeline_event(
            attempt_id,
            phase="github",
            event_type="pr_branch_updated",
            message=branch,
            data={"pull_request": int(pull_request["number"]), "commit_sha": commit_sha},
        )
        return PullRequestBranchUpdate(
            number=int(pull_request["number"]),
            url=str(pull_request["url"]),
            title=str(pull_request["title"]),
            branch=branch,
            commit_sha=commit_sha,
            pushed=True,
        )

    def post_comment(self, comment_id: int, body: str) -> PostedComment:
        cleaned_body = body.strip()
        if not cleaned_body:
            raise ReviewActionError("Comment body must not be empty.")
        comment = self.store.comment_by_id(comment_id)
        if not comment:
            raise ReviewActionError(f"Comment {comment_id} does not exist.")
        if str(comment["status"]) == "posted":
            return PostedComment(
                url=str(comment["url"]),
                attempt_id=_optional_int(comment["attempt_id"]),
                repo=str(comment["repo"]),
                issue_number=int(comment["issue_number"]),
            )
        repo = str(comment["repo"])
        issue_number = int(comment["issue_number"])
        url = self.github.create_comment(repo, issue_number, cleaned_body)
        self.store.mark_comment_posted(comment_id, body=cleaned_body, url=url)
        attempt_id = _optional_int(comment["attempt_id"])
        if attempt_id is not None:
            self.store.record_timeline_event(
                attempt_id,
                phase="github",
                event_type="comment_posted",
                message=url,
            )
        return PostedComment(url=url, attempt_id=attempt_id, repo=repo, issue_number=issue_number)

    def _ensure_commit(
        self,
        attempt_id: int,
        repo: str,
        issue_number: int,
        worktree: GitWorktree,
        base_commit: str,
        *,
        require_changes: bool,
    ) -> str:
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
            return commit.sha
        if worktree.commits_since(base_commit) > 0:
            commit_sha = worktree.head_sha()
            self.store.record_timeline_event(
                attempt_id,
                phase="git",
                event_type="existing_commits_detected",
                message=commit_sha,
            )
            return commit_sha
        if not require_changes:
            return ""
        raise ReviewActionError("No code changes were found in this attempt worktree.")


def build_draft_pr_content(
    repo: str,
    issue_number: int,
    worker_result: str,
    *,
    issue_title: str = "",
) -> DraftPullRequestContent:
    summary_lines = _summary_lines_from_worker_result(worker_result)
    verification_lines = _verification_lines_from_worker_result(worker_result)
    issue_url = f"https://github.com/{repo}/issues/{issue_number}"
    body_parts = [
        "## Changes",
        "",
        _format_lines(summary_lines or _fallback_summary_lines(issue_title, issue_number)),
    ]
    if verification_lines:
        body_parts.extend(
            [
                "",
                "## Tests",
                "",
                _format_lines(verification_lines),
            ]
        )
    body_parts.extend(
        [
            "",
            f"Fixes {issue_url}",
            "",
            issue_link_marker(repo, issue_number),
        ]
    )
    body_parts.append("")
    return DraftPullRequestContent(
        title=_title_from_issue(issue_number, issue_title, summary_lines),
        body="\n".join(body_parts),
    )


def build_draft_pr_body(
    repo: str,
    issue_number: int,
    worker_result: str,
    *,
    issue_title: str = "",
) -> str:
    return build_draft_pr_content(repo, issue_number, worker_result, issue_title=issue_title).body


def issue_link_marker(repo: str, issue_number: int) -> str:
    return f"<!-- {ISSUE_LINK_MARKER_PREFIX}https://github.com/{repo}/issues/{issue_number} -->"


def ensure_issue_link_marker(body: str, repo: str, issue_number: int) -> str:
    marker = issue_link_marker(repo, issue_number)
    if marker in body:
        return body
    return body.rstrip() + "\n\n" + marker + "\n"


def body_links_issue(body: str, repo: str, issue_number: int) -> bool:
    return issue_link_marker(repo, issue_number) in body


def _summary_lines_from_worker_result(worker_result: str) -> list[str]:
    summary = _section_lines(worker_result, {"summary"})
    if summary:
        return summary[:2]

    candidates: list[str] = []
    for line in worker_result.splitlines():
        cleaned = _clean_result_line(line)
        if not cleaned or _looks_like_progress(cleaned):
            continue
        lowered = cleaned.lower()
        if any(
            keyword in lowered
            for keyword in ("add", "adjust", "change", "create", "expand", "fix", "implement", "update")
        ):
            candidates.append(cleaned)
        if len(candidates) == 2:
            break
    return candidates


def _verification_lines_from_worker_result(worker_result: str) -> list[str]:
    return _section_lines(worker_result, {"checks run", "verification"})[:3]


def _section_lines(worker_result: str, headings: set[str]) -> list[str]:
    lines = worker_result.splitlines()
    for index, line in enumerate(lines):
        if _heading_name(line) not in headings:
            continue
        collected: list[str] = []
        for section_line in lines[index + 1 :]:
            if _heading_name(section_line):
                break
            cleaned = _clean_result_line(section_line)
            if cleaned and not _looks_like_pr_noise(cleaned):
                collected.append(cleaned)
        return collected
    return []


def _heading_name(line: str) -> str:
    normalized = line.strip().lstrip("#").strip().rstrip(":").lower()
    if normalized in {"summary", "checks run", "verification", "worker notes", "issue"}:
        return normalized
    return ""


def _clean_result_line(line: str) -> str:
    stripped = line.strip().removeprefix("-").strip()
    return _strip_markdown_links(stripped.rstrip())


def _looks_like_progress(line: str) -> bool:
    lowered = line.lower()
    return lowered.startswith(("i'll ", "i'm ", "the checkout ", "the worktree "))


def _looks_like_pr_noise(line: str) -> bool:
    lowered = line.lower()
    return _looks_like_progress(line) or lowered.startswith(
        (
            "i ",
            "i'll ",
            "i'm ",
            "i’ve ",
            "i've ",
            "checked ",
            "confirmed ",
            "inspected ",
            "looked ",
            "opened ",
            "read ",
            "reviewed ",
            "the checkout ",
            "the worktree ",
        )
    )


def _fallback_summary_lines(issue_title: str, issue_number: int) -> list[str]:
    issue_summary = _issue_title_summary(issue_title)
    if issue_summary:
        return [f"Addresses {issue_summary}."]
    return [f"Updates the code for issue #{issue_number}."]


def _format_lines(lines: list[str]) -> str:
    if len(lines) == 1:
        return lines[0]
    return "\n".join(f"- {line}" for line in lines)


def _title_from_issue(issue_number: int, issue_title: str, summary_lines: list[str]) -> str:
    issue_summary = _issue_title_summary(issue_title)
    if issue_summary:
        return _truncate(f"Fix #{issue_number}: {issue_summary}", 72)
    return _title_from_summary(issue_number, summary_lines)


def _title_from_summary(issue_number: int, summary_lines: list[str]) -> str:
    if not summary_lines:
        return f"Fix #{issue_number}"
    plain_summary = _compact_title(summary_lines[0].replace("`", "").rstrip("."))
    return _truncate(f"Fix #{issue_number}: {plain_summary}", 72)


def _issue_title_summary(issue_title: str) -> str:
    cleaned = _clean_issue_title(issue_title)
    if not cleaned:
        return ""
    quoted = _quoted_error_summary(issue_title)
    if quoted:
        return quoted
    support_match = re.match(r"(?P<subject>.+?)\s+should\s+support\s+(?P<feature>.+)", cleaned, re.IGNORECASE)
    if support_match:
        subject = support_match.group("subject").strip()
        feature = support_match.group("feature").strip()
        return _sentence_case(f"Support {feature} in {subject}")
    return _sentence_case(_compact_issue_title(cleaned))


def _clean_issue_title(issue_title: str) -> str:
    cleaned = _strip_markdown_links(issue_title).replace("`", "").strip()
    cleaned = re.sub(r"^\[(?:bug|fix|feature|fr|documentation|docs)\]\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"^(?:bug|fix|issue|error|feature request|fr)\s*[:\-]\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^(?:getting|gets|got)\s+error\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" .")


def _quoted_error_summary(issue_title: str) -> str:
    match = re.search(r"['\"`](?P<message>[^'\"`]{5,80})['\"`]", issue_title)
    if match is None:
        return ""
    summary = match.group("message").strip(" .")
    lowered = issue_title.lower()
    if "default" in lowered and ("director" in lowered or "folder" in lowered or "path" in lowered):
        summary = f"{summary} outside default directory"
    return _sentence_case(summary)


def _compact_issue_title(issue_title: str) -> str:
    compact = issue_title
    for separator in (" when ", " after ", " with "):
        if len(compact) > 64 and separator in compact.lower():
            parts = re.split(separator, compact, maxsplit=1, flags=re.IGNORECASE)
            if len(parts[0]) >= 20:
                compact = parts[0]
                break
    return compact


def _sentence_case(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return ""
    return stripped[0].upper() + stripped[1:]


def _strip_markdown_links(value: str) -> str:
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)


def _compact_title(value: str) -> str:
    compact = value
    if " before " in compact:
        compact = compact.split(" before ", 1)[0]
    if len(compact) > 80 and " in " in compact:
        compact = compact.split(" in ", 1)[0]
    return compact


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "..."


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
