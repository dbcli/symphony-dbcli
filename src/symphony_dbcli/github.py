from __future__ import annotations

import json
import os
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


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    title: str
    state: str = ""
    merged_at: str = ""
    head_sha: str = ""

    @property
    def is_merged(self) -> bool:
        return bool(self.merged_at)


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
class GitHubCheckRun:
    name: str
    status: str
    conclusion: str
    url: str


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


def _pull_request_from_json(data: dict[str, Any]) -> PullRequest:
    return PullRequest(
        number=int(data["number"]),
        url=str(data["html_url"]),
        title=str(data["title"]),
        state=str(data.get("state") or ""),
        merged_at=str(data.get("merged_at") or ""),
        head_sha=_head_sha(data),
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


def _json_object(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value or {})


def _json_objects(value: Any) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], value or [])


def _redact_token(value: str, token: str) -> str:
    return value.replace(token, "<redacted>")


FAILURE_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required", "cancelled", "startup_failure"})
