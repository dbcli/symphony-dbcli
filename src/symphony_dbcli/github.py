from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .config import GitHubConfig, LabelConfig
from .github_auth import GitHubAuthenticator, GitHubAuthError
from .store import IssueSnapshot


class GitHubError(RuntimeError):
    """Raised when GitHub API calls fail."""


MAX_CI_FAILURE_CONTEXT_CHECKS = 5
MAX_CI_ANNOTATIONS_PER_CHECK = 10
MAX_CI_OUTPUT_CHARS = 4_000
MAX_CI_LOG_CHARS = 12_000
MAX_CI_LOG_BYTES = 240_000
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FAILURE_LINE_RE = re.compile(
    r"(::error|traceback|assertionerror|failed\b|failure\b|error:|failures|"
    r"exception|expected .* actual|expected .* got)",
    re.IGNORECASE,
)
LOW_SIGNAL_CI_ANNOTATION_RE = re.compile(
    r"(node\.js \d+ actions are deprecated|process completed with exit code \d+\.?)",
    re.IGNORECASE,
)
ACTIONABLE_CI_ANNOTATION_LEVELS = {"failure", "error"}


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    title: str
    state: str = ""
    author: str = ""
    labels: list[str] | None = None
    updated_at: str = ""
    merged_at: str = ""
    head_sha: str = ""
    head_ref: str = ""
    head_repo: str = ""
    body: str = ""
    mergeable: bool | None = None
    mergeable_state: str = ""

    @property
    def is_merged(self) -> bool:
        return bool(self.merged_at)


@dataclass(frozen=True)
class PullRequestMergeStatus:
    number: int
    url: str
    title: str
    state: str
    merged_at: str
    head_sha: str
    mergeable: bool | None
    mergeable_state: str
    has_conflicts: bool


@dataclass(frozen=True)
class GitHubAppConversion:
    app_id: int
    slug: str
    html_url: str
    pem: str
    webhook_secret: str


@dataclass(frozen=True)
class GitHubInstallation:
    id: int
    account_login: str
    account_type: str


@dataclass(frozen=True)
class GitHubIssue:
    repo: str
    number: int
    title: str
    body: str
    url: str
    state: str
    labels: list[str]
    author: str
    updated_at: str

    def snapshot(self, labels: LabelConfig, default_task_type: str) -> IssueSnapshot:
        task_type = default_task_type
        if labels.type_code in self.labels:
            task_type = "code"
        if labels.type_research in self.labels:
            task_type = "research"
        return IssueSnapshot(
            repo=self.repo,
            number=self.number,
            title=self.title,
            url=self.url,
            state=self.state,
            labels=self.labels,
            task_type=task_type,
            body=self.body,
            author=self.author,
            updated_at=self.updated_at,
        )


@dataclass(frozen=True)
class GitHubComment:
    id: int
    url: str
    body: str
    author: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class GitHubPullRequestReviewComment:
    id: int
    url: str
    body: str
    author: str
    created_at: str
    updated_at: str
    kind: str
    review_id: int | None = None
    path: str = ""
    line: int | None = None
    original_line: int | None = None
    side: str = ""
    diff_hunk: str = ""
    state: str = ""


@dataclass(frozen=True)
class GitHubCheckRun:
    name: str
    status: str
    conclusion: str
    url: str


@dataclass(frozen=True)
class GitHubCheckAnnotation:
    path: str
    start_line: int | None
    end_line: int | None
    annotation_level: str
    title: str
    message: str
    raw_details: str
    url: str


@dataclass(frozen=True)
class GitHubCiFailureCheckContext:
    name: str
    status: str
    conclusion: str
    url: str
    details_url: str
    summary: str
    text: str
    annotations: list[GitHubCheckAnnotation]
    log_excerpt: str
    unavailable_reason: str = ""


@dataclass(frozen=True)
class GitHubCiFailureContext:
    sha: str
    failed_checks: list[GitHubCiFailureCheckContext]
    unavailable_reason: str = ""


@dataclass(frozen=True)
class GitHubCiStatus:
    sha: str
    state: str
    conclusion: str
    failed_checks: list[GitHubCheckRun]
    checks: list[GitHubCheckRun]


class GitHubClient:
    def __init__(self, config: GitHubConfig):
        self.config = config
        self.auth = GitHubAuthenticator(config)

    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]:
        params = {"state": "open", "per_page": "100"}
        if labels:
            params["labels"] = ",".join(labels)
        data = self._request_json("GET", f"/repos/{repo}/issues?{urllib.parse.urlencode(params)}")
        issues: list[GitHubIssue] = []
        for item in data:
            if "pull_request" in item:
                continue
            issues.append(_issue_from_json(repo, item))
        return issues

    def issue(self, repo: str, issue_number: int) -> GitHubIssue:
        data = self._request_json("GET", f"/repos/{repo}/issues/{issue_number}")
        return _issue_from_json(repo, data)

    def list_comments(self, repo: str, issue_number: int) -> list[GitHubComment]:
        data = self._request_json(
            "GET",
            f"/repos/{repo}/issues/{issue_number}/comments?per_page=100",
        )
        return [_comment_from_json(item) for item in data]

    def list_pull_request_review_comments(
        self,
        repo: str,
        pull_request_number: int,
    ) -> list[GitHubPullRequestReviewComment]:
        reviews = self._request_json(
            "GET",
            f"/repos/{repo}/pulls/{pull_request_number}/reviews?per_page=100",
        )
        inline_comments = self._request_json(
            "GET",
            f"/repos/{repo}/pulls/{pull_request_number}/comments?per_page=100",
        )
        return [
            *[_review_comment_from_json(item) for item in _json_objects(reviews) if item.get("body")],
            *[_inline_review_comment_from_json(item) for item in _json_objects(inline_comments)],
        ]

    def list_pull_requests(self, repo: str, *, state: str = "open") -> list[PullRequest]:
        data = self._request_json("GET", f"/repos/{repo}/pulls?state={state}&per_page=100")
        return [_pull_request_from_json(item) for item in _json_objects(data)]

    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> None:
        self._require_token()
        self._request_json("POST", f"/repos/{repo}/issues/{issue_number}/labels", {"labels": labels})

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        self._require_token()
        encoded = urllib.parse.quote(label, safe="")
        self._request_json(
            "DELETE", f"/repos/{repo}/issues/{issue_number}/labels/{encoded}", expect_empty=True
        )

    def create_comment(self, repo: str, issue_number: int, body: str) -> str:
        self._require_token()
        data = self._request_json("POST", f"/repos/{repo}/issues/{issue_number}/comments", {"body": body})
        return str(data.get("html_url") or "")

    def default_branch(self, repo: str) -> str:
        data = self._request_json("GET", f"/repos/{repo}")
        return str(data.get("default_branch") or "main")

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
        self._require_token()
        data = self._request_json(
            "POST",
            f"/repos/{repo}/pulls",
            {
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "draft": draft,
                "maintainer_can_modify": True,
            },
        )
        return _pull_request_from_json(data)

    def pull_request(self, repo: str, number: int) -> PullRequest:
        data = self._request_json("GET", f"/repos/{repo}/pulls/{number}")
        return _pull_request_from_json(data)

    def merge_status(self, repo: str, pull_request_number: int) -> PullRequestMergeStatus:
        data = self._request_json("GET", f"/repos/{repo}/pulls/{pull_request_number}")
        return _merge_status_from_json(data)

    def ci_status(self, repo: str, pull_request_number: int) -> GitHubCiStatus:
        pull_request = self._request_json("GET", f"/repos/{repo}/pulls/{pull_request_number}")
        sha = _head_sha(pull_request)
        if not sha:
            raise GitHubError(f"Pull request {repo}#{pull_request_number} does not have a head SHA.")
        check_runs = self._request_json(
            "GET",
            f"/repos/{repo}/commits/{sha}/check-runs?per_page=100",
        )
        combined_status = self._request_json("GET", f"/repos/{repo}/commits/{sha}/status")
        return _ci_status_from_json(sha, check_runs, combined_status)

    def ci_failure_context(
        self,
        repo: str,
        pull_request_number: int,
        failed_checks: list[GitHubCheckRun],
    ) -> GitHubCiFailureContext:
        pull_request = self._request_json("GET", f"/repos/{repo}/pulls/{pull_request_number}")
        sha = _head_sha(pull_request)
        if not sha:
            raise GitHubError(f"Pull request {repo}#{pull_request_number} does not have a head SHA.")
        check_runs = self._request_json(
            "GET",
            f"/repos/{repo}/commits/{sha}/check-runs?per_page=100",
        )
        failed_check_runs = _matching_failed_check_runs(check_runs, failed_checks)
        contexts = [
            self._ci_failure_check_context(repo, item)
            for item in failed_check_runs[:MAX_CI_FAILURE_CONTEXT_CHECKS]
        ]
        missing_status_contexts = _missing_status_contexts(failed_checks, failed_check_runs)
        contexts.extend(missing_status_contexts[: MAX_CI_FAILURE_CONTEXT_CHECKS - len(contexts)])
        unavailable_reason = ""
        if failed_checks and not contexts:
            unavailable_reason = "No failed check-run log data was available from GitHub."
        return GitHubCiFailureContext(
            sha=sha,
            failed_checks=contexts,
            unavailable_reason=unavailable_reason,
        )

    def _ci_failure_check_context(
        self,
        repo: str,
        data: dict[str, Any],
    ) -> GitHubCiFailureCheckContext:
        check_run_id = _optional_int(data.get("id"))
        output = _json_object(data.get("output"))
        annotations: list[GitHubCheckAnnotation] = []
        annotation_error = ""
        if check_run_id is not None:
            try:
                annotations = self._check_run_annotations(repo, check_run_id)
            except GitHubError as exc:
                annotation_error = str(exc)
        annotations = _actionable_ci_annotations(annotations)
        log_excerpt = ""
        log_error = ""
        job_id = _actions_job_id(data)
        if job_id is not None:
            try:
                log_excerpt = _failure_log_excerpt(
                    self._request_text("GET", f"/repos/{repo}/actions/jobs/{job_id}/logs")
                )
            except GitHubError as exc:
                log_error = str(exc)
        unavailable_reason = _unavailable_reason(
            has_context=bool(
                _str_text(output.get("summary"))
                or _str_text(output.get("text"))
                or annotations
                or log_excerpt
            ),
            annotation_error=annotation_error,
            log_error=log_error,
            job_id=job_id,
        )
        return GitHubCiFailureCheckContext(
            name=str(data.get("name") or ""),
            status=str(data.get("status") or ""),
            conclusion=str(data.get("conclusion") or ""),
            url=str(data.get("html_url") or ""),
            details_url=str(data.get("details_url") or ""),
            summary=_truncate_text(_str_text(output.get("summary")), MAX_CI_OUTPUT_CHARS),
            text=_truncate_text(_str_text(output.get("text")), MAX_CI_OUTPUT_CHARS),
            annotations=annotations,
            log_excerpt=log_excerpt,
            unavailable_reason=unavailable_reason,
        )

    def _check_run_annotations(self, repo: str, check_run_id: int) -> list[GitHubCheckAnnotation]:
        data = self._request_json(
            "GET",
            f"/repos/{repo}/check-runs/{check_run_id}/annotations?per_page={MAX_CI_ANNOTATIONS_PER_CHECK}",
        )
        return [_annotation_from_json(item) for item in _json_objects(data)]

    def push_branch(self, *, repo: str, worktree_path: str, branch: str) -> None:
        token = self._require_token()
        remote_url = self.auth.authenticated_git_url(repo)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as script:
            script.write(
                "#!/bin/sh\n"
                'case "$1" in\n'
                "*Username*) printf '%s\\n' x-access-token ;;\n"
                "*) printf '%s\\n' \"$SYMPHONY_GIT_TOKEN\" ;;\n"
                "esac\n"
            )
        script_path = Path(script.name)
        script_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        env = os.environ.copy()
        env["GIT_ASKPASS"] = str(script_path)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["SYMPHONY_GIT_TOKEN"] = token
        try:
            result = subprocess.run(
                ["git", "-C", worktree_path, "push", remote_url, f"{branch}:{branch}"],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
        finally:
            script_path.unlink(missing_ok=True)
        if result.returncode != 0:
            raise GitHubError(_redact_token(result.stderr.strip(), token) or "git push failed")

    def convert_manifest_code(self, code: str) -> GitHubAppConversion:
        data = self._request_json("POST", f"/app-manifests/{urllib.parse.quote(code, safe='')}/conversions")
        return GitHubAppConversion(
            app_id=int(data["id"]),
            slug=str(data.get("slug") or ""),
            html_url=str(data.get("html_url") or ""),
            pem=str(data["pem"]),
            webhook_secret=str(data.get("webhook_secret") or ""),
        )

    def list_app_installations(self) -> list[GitHubInstallation]:
        token = self.auth.app_jwt()
        data = request_json(self.config.api_base_url, "GET", "/app/installations", token=token)
        return [
            GitHubInstallation(
                id=int(item["id"]),
                account_login=str(item.get("account", {}).get("login") or ""),
                account_type=str(item.get("account", {}).get("type") or ""),
            )
            for item in data
        ]

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expect_empty: bool = False,
    ) -> Any:
        token = self.auth.api_token()
        return request_json(
            self.config.api_base_url,
            method,
            path,
            payload,
            token=token,
            expect_empty=expect_empty,
        )

    def _request_text(self, method: str, path: str) -> str:
        token = self.auth.api_token()
        return request_text(self.config.api_base_url, method, path, token=token)

    def _require_token(self) -> str:
        try:
            return self.auth.require_api_token()
        except GitHubAuthError as exc:
            raise GitHubError(str(exc)) from exc


def request_json(
    api_base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    token: str | None = None,
    expect_empty: bool = False,
) -> Any:
    url = api_base_url.rstrip("/") + path
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubError(f"GitHub API {method} {path} failed: {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise GitHubError(f"GitHub API {method} {path} failed: {exc.reason}") from exc
    if expect_empty or not data:
        return None
    return cast(Any, json.loads(data.decode("utf-8")))


def request_text(
    api_base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
) -> str:
    url = api_base_url.rstrip("/") + path
    request = urllib.request.Request(url, method=method)
    request.add_header("Accept", "text/plain")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(MAX_CI_LOG_BYTES)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubError(f"GitHub API {method} {path} failed: {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise GitHubError(f"GitHub API {method} {path} failed: {exc.reason}") from exc
    return cast(bytes, data).decode("utf-8", errors="replace")


def _pull_request_from_json(data: dict[str, Any]) -> PullRequest:
    return PullRequest(
        number=int(data["number"]),
        url=str(data["html_url"]),
        title=str(data["title"]),
        state=str(data.get("state") or ""),
        author=str(_json_object(data.get("user")).get("login") or ""),
        labels=[str(label.get("name") or "") for label in _json_objects(data.get("labels"))],
        updated_at=str(data.get("updated_at") or ""),
        merged_at=str(data.get("merged_at") or ""),
        head_sha=_head_sha(data),
        head_ref=_head_ref(data),
        head_repo=_head_repo(data),
        body=str(data.get("body") or ""),
        mergeable=_optional_bool(data.get("mergeable")),
        mergeable_state=str(data.get("mergeable_state") or ""),
    )


def _merge_status_from_json(data: dict[str, Any]) -> PullRequestMergeStatus:
    mergeable = _optional_bool(data.get("mergeable"))
    mergeable_state = str(data.get("mergeable_state") or "")
    return PullRequestMergeStatus(
        number=int(data["number"]),
        url=str(data["html_url"]),
        title=str(data["title"]),
        state=str(data.get("state") or ""),
        merged_at=str(data.get("merged_at") or ""),
        head_sha=_head_sha(data),
        mergeable=mergeable,
        mergeable_state=mergeable_state,
        has_conflicts=mergeable is False or mergeable_state == "dirty",
    )


def _issue_from_json(repo: str, data: dict[str, Any]) -> GitHubIssue:
    return GitHubIssue(
        repo=repo,
        number=int(data["number"]),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        url=str(data.get("html_url") or ""),
        state=str(data.get("state") or "open"),
        labels=[str(label.get("name") or "") for label in _json_objects(data.get("labels"))],
        author=str(_json_object(data.get("user")).get("login") or ""),
        updated_at=str(data.get("updated_at") or ""),
    )


def _comment_from_json(data: dict[str, Any]) -> GitHubComment:
    return GitHubComment(
        id=int(data["id"]),
        url=str(data.get("html_url") or ""),
        body=str(data.get("body") or ""),
        author=str(_json_object(data.get("user")).get("login") or ""),
        created_at=str(data.get("created_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
    )


def _review_comment_from_json(data: dict[str, Any]) -> GitHubPullRequestReviewComment:
    return GitHubPullRequestReviewComment(
        id=int(data["id"]),
        url=str(data.get("html_url") or ""),
        body=str(data.get("body") or ""),
        author=str(_json_object(data.get("user")).get("login") or ""),
        created_at=str(data.get("submitted_at") or ""),
        updated_at=str(data.get("submitted_at") or ""),
        kind="review",
        state=str(data.get("state") or ""),
    )


def _inline_review_comment_from_json(data: dict[str, Any]) -> GitHubPullRequestReviewComment:
    return GitHubPullRequestReviewComment(
        id=int(data["id"]),
        url=str(data.get("html_url") or ""),
        body=str(data.get("body") or ""),
        author=str(_json_object(data.get("user")).get("login") or ""),
        created_at=str(data.get("created_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
        kind="inline",
        review_id=_optional_int(data.get("pull_request_review_id")),
        path=str(data.get("path") or ""),
        line=_optional_int(data.get("line")),
        original_line=_optional_int(data.get("original_line")),
        side=str(data.get("side") or ""),
        diff_hunk=str(data.get("diff_hunk") or ""),
    )


def _ci_status_from_json(
    sha: str,
    check_runs_data: dict[str, Any],
    combined_status_data: dict[str, Any],
) -> GitHubCiStatus:
    checks = [
        GitHubCheckRun(
            name=str(item.get("name") or ""),
            status=str(item.get("status") or ""),
            conclusion=str(item.get("conclusion") or ""),
            url=str(item.get("html_url") or ""),
        )
        for item in _json_objects(check_runs_data.get("check_runs"))
    ]
    checks.extend(_status_checks_from_json(combined_status_data))
    failed_checks = [check for check in checks if check.conclusion in FAILURE_CONCLUSIONS]
    pending_checks = [check for check in checks if check.status != "completed"]
    if failed_checks:
        state = "failure"
    elif pending_checks:
        state = "pending"
    else:
        state = "success" if checks else str(combined_status_data.get("state") or "unknown")
    return GitHubCiStatus(
        sha=sha,
        state=state,
        conclusion=state,
        failed_checks=failed_checks,
        checks=checks,
    )


def _matching_failed_check_runs(
    check_runs_data: dict[str, Any],
    failed_checks: list[GitHubCheckRun],
) -> list[dict[str, Any]]:
    failed_names = {check.name for check in failed_checks if check.name}
    failed_urls = {check.url for check in failed_checks if check.url}
    check_runs = _json_objects(check_runs_data.get("check_runs"))
    return [
        item
        for item in check_runs
        if str(item.get("conclusion") or "") in FAILURE_CONCLUSIONS
        and _check_matches_failed_status(item, failed_names, failed_urls)
    ]


def _check_matches_failed_status(
    check_run: dict[str, Any],
    failed_names: set[str],
    failed_urls: set[str],
) -> bool:
    if not failed_names and not failed_urls:
        return True
    name = str(check_run.get("name") or "")
    html_url = str(check_run.get("html_url") or "")
    details_url = str(check_run.get("details_url") or "")
    return name in failed_names or html_url in failed_urls or details_url in failed_urls


def _missing_status_contexts(
    failed_checks: list[GitHubCheckRun],
    failed_check_runs: list[dict[str, Any]],
) -> list[GitHubCiFailureCheckContext]:
    matched_names = {str(item.get("name") or "") for item in failed_check_runs}
    matched_urls = {
        url
        for item in failed_check_runs
        for url in (str(item.get("html_url") or ""), str(item.get("details_url") or ""))
        if url
    }
    contexts: list[GitHubCiFailureCheckContext] = []
    for check in failed_checks:
        if check.name in matched_names or check.url in matched_urls:
            continue
        contexts.append(
            GitHubCiFailureCheckContext(
                name=check.name,
                status=check.status,
                conclusion=check.conclusion,
                url=check.url,
                details_url="",
                summary="",
                text="",
                annotations=[],
                log_excerpt="",
                unavailable_reason="No GitHub check-run annotations or Actions job logs were available for this status check.",
            )
        )
    return contexts


def _annotation_from_json(data: dict[str, Any]) -> GitHubCheckAnnotation:
    return GitHubCheckAnnotation(
        path=str(data.get("path") or ""),
        start_line=_optional_int(data.get("start_line")),
        end_line=_optional_int(data.get("end_line")),
        annotation_level=str(data.get("annotation_level") or ""),
        title=str(data.get("title") or ""),
        message=_truncate_text(_str_text(data.get("message")), MAX_CI_OUTPUT_CHARS),
        raw_details=_truncate_text(_str_text(data.get("raw_details")), MAX_CI_OUTPUT_CHARS),
        url=str(data.get("blob_href") or ""),
    )


def _actionable_ci_annotations(
    annotations: list[GitHubCheckAnnotation],
) -> list[GitHubCheckAnnotation]:
    return [
        annotation
        for annotation in annotations
        if is_actionable_ci_annotation(
            annotation_level=annotation.annotation_level,
            title=annotation.title,
            message=annotation.message,
            raw_details=annotation.raw_details,
        )
    ]


def is_actionable_ci_annotation(
    *, annotation_level: str, title: str, message: str, raw_details: str = ""
) -> bool:
    text = " ".join(part for part in (title.strip(), message.strip(), raw_details.strip()) if part)
    if not text:
        return False
    level = annotation_level.strip().lower()
    if level and level not in ACTIONABLE_CI_ANNOTATION_LEVELS:
        return False
    return LOW_SIGNAL_CI_ANNOTATION_RE.search(text) is None


def _actions_job_id(data: dict[str, Any]) -> int | None:
    for field in ("details_url", "html_url"):
        match = re.search(r"/job/(?P<job_id>\d+)", str(data.get(field) or ""))
        if match:
            return int(match.group("job_id"))
    return None


def _failure_log_excerpt(log_text: str) -> str:
    cleaned = ANSI_ESCAPE_RE.sub("", log_text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [line.rstrip() for line in cleaned.splitlines()]
    if not lines:
        return ""
    marker_indexes = [index for index, line in enumerate(lines) if FAILURE_LINE_RE.search(line)]
    if not marker_indexes:
        return _truncate_text("\n".join(lines[-80:]), MAX_CI_LOG_CHARS)
    ranges = _merged_line_windows(marker_indexes, before=20, after=40, limit=len(lines))
    excerpt_lines: list[str] = []
    for index, (start, end) in enumerate(ranges):
        if index:
            excerpt_lines.append("...")
        excerpt_lines.extend(lines[start:end])
    return _truncate_text("\n".join(excerpt_lines), MAX_CI_LOG_CHARS)


def _merged_line_windows(
    indexes: list[int],
    *,
    before: int,
    after: int,
    limit: int,
) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for index in indexes:
        start = max(0, index - before)
        end = min(limit, index + after + 1)
        if ranges and start <= ranges[-1][1]:
            previous_start, previous_end = ranges[-1]
            ranges[-1] = (previous_start, max(previous_end, end))
        else:
            ranges.append((start, end))
    return ranges[:3]


def _unavailable_reason(
    *,
    has_context: bool,
    annotation_error: str,
    log_error: str,
    job_id: int | None,
) -> str:
    if has_context:
        return ""
    reasons: list[str] = []
    if annotation_error:
        reasons.append(f"annotations unavailable: {annotation_error}")
    if log_error:
        reasons.append(f"job log unavailable: {log_error}")
    elif job_id is None:
        reasons.append("check did not include a GitHub Actions job URL")
    return "; ".join(reasons) or "No failure output was available for this check."


def _truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 14].rstrip() + "\n...[truncated]"


def _str_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _status_checks_from_json(data: dict[str, Any]) -> list[GitHubCheckRun]:
    return [
        GitHubCheckRun(
            name=str(item.get("context") or ""),
            status=_legacy_status_state(str(item.get("state") or "")),
            conclusion=_legacy_status_conclusion(str(item.get("state") or "")),
            url=str(item.get("target_url") or ""),
        )
        for item in _json_objects(data.get("statuses"))
    ]


def _legacy_status_state(state: str) -> str:
    if state in {"success", "failure", "error"}:
        return "completed"
    return "pending"


def _legacy_status_conclusion(state: str) -> str:
    if state == "success":
        return "success"
    if state in {"failure", "error"}:
        return "failure"
    return ""


def _head_sha(data: dict[str, Any]) -> str:
    return str(_json_object(data.get("head")).get("sha") or "")


def _head_ref(data: dict[str, Any]) -> str:
    return str(_json_object(data.get("head")).get("ref") or "")


def _head_repo(data: dict[str, Any]) -> str:
    repo = _json_object(_json_object(data.get("head")).get("repo"))
    return str(repo.get("full_name") or "")


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) else None


def _json_object(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value or {})


def _json_objects(value: Any) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], value or [])


def _redact_token(value: str, token: str) -> str:
    return value.replace(token, "<redacted>")


FAILURE_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required", "cancelled", "startup_failure"})
