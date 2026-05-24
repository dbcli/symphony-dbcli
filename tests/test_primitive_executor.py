from __future__ import annotations

from pathlib import Path
from typing import Any

from symphony_dbcli.config import default_config
from symphony_dbcli.github import GitHubCheckRun, GitHubCiStatus, GitHubComment, GitHubIssue, PullRequest
from symphony_dbcli.primitive_executor import PrimitiveContext, PrimitiveExecutor
from symphony_dbcli.store import Store
from symphony_dbcli.workflow_definition import WorkflowTransitionConfig


def test_fetch_issue_persists_latest_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    executor = PrimitiveExecutor(default_config(), store, github=FakePrimitiveGitHub())

    output = executor.execute(_context("github.fetch_issue")).output

    detail = store.issue_detail("dbcli/litecli", 245)
    assert output["issue"]["title"] == "Logging path support"
    assert detail is not None
    assert detail["issue"]["title"] == "Logging path support"
    assert {row["label"] for row in detail["labels"]} == {"symphony:todo", "symphony:type:code"}


def test_fetch_comments_returns_comment_snapshots(tmp_path: Path) -> None:
    executor = PrimitiveExecutor(default_config(), _store(tmp_path), github=FakePrimitiveGitHub())

    output = executor.execute(_context("github.fetch_comments")).output

    assert output["comments"] == [
        {
            "id": 99,
            "url": "https://github.com/dbcli/litecli/issues/245#issuecomment-99",
            "body": "I can reproduce this on 1.12.",
            "author": "amjith",
            "created_at": "2026-05-24T10:00:00Z",
            "updated_at": "2026-05-24T10:00:00Z",
        }
    ]


def test_fetch_pull_request_records_attempt_pr(tmp_path: Path) -> None:
    store = _store(tmp_path)
    executor_github = FakePrimitiveGitHub()
    store.upsert_issue(
        executor_github.issue("dbcli/litecli", 245).snapshot(default_config().labels, "research")
    )
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="review",
    )
    executor = PrimitiveExecutor(default_config(), store, github=executor_github)

    output = executor.execute(
        _context(
            "github.fetch_pull_request",
            attempt_id=attempt_id,
            input_data={"pull_request_number": 12},
        )
    ).output

    detail = store.attempt_detail(attempt_id)
    assert output["pull_request_url"] == "https://github.com/dbcli/litecli/pull/12"
    assert output["is_merged"] is False
    assert detail is not None
    assert detail["pull_requests"][0]["number"] == 12


def test_fetch_ci_status_returns_failed_checks(tmp_path: Path) -> None:
    executor = PrimitiveExecutor(default_config(), _store(tmp_path), github=FakePrimitiveGitHub())

    output = executor.execute(
        _context("github.fetch_ci_status", input_data={"pull_request_number": 12})
    ).output

    assert output["sha"] == "abc123"
    assert output["state"] == "failure"
    assert output["failed_checks"] == [
        {
            "name": "tests",
            "status": "completed",
            "conclusion": "failure",
            "url": "https://github.com/dbcli/litecli/actions/runs/1",
        }
    ]


class FakePrimitiveGitHub:
    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]:
        return [self.issue(repo, 245)]

    def issue(self, repo: str, issue_number: int) -> GitHubIssue:
        return GitHubIssue(
            repo=repo,
            number=issue_number,
            title="Logging path support",
            body="The log_file option does not expand ~.",
            url=f"https://github.com/{repo}/issues/{issue_number}",
            state="open",
            labels=["symphony:todo", "symphony:type:code"],
            author="amjith",
            updated_at="2026-05-24T11:00:00Z",
        )

    def list_comments(self, repo: str, issue_number: int) -> list[GitHubComment]:
        return [
            GitHubComment(
                id=99,
                url=f"https://github.com/{repo}/issues/{issue_number}#issuecomment-99",
                body="I can reproduce this on 1.12.",
                author="amjith",
                created_at="2026-05-24T10:00:00Z",
                updated_at="2026-05-24T10:00:00Z",
            )
        ]

    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> None:
        return

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        return

    def pull_request(self, repo: str, number: int) -> PullRequest:
        return PullRequest(
            number=number,
            url=f"https://github.com/{repo}/pull/{number}",
            title="Fix logging path support",
            state="open",
            head_sha="abc123",
        )

    def ci_status(self, repo: str, pull_request_number: int) -> GitHubCiStatus:
        failed_check = GitHubCheckRun(
            name="tests",
            status="completed",
            conclusion="failure",
            url=f"https://github.com/{repo}/actions/runs/1",
        )
        return GitHubCiStatus(
            sha="abc123",
            state="failure",
            conclusion="failure",
            failed_checks=[failed_check],
            checks=[failed_check],
        )


def _context(
    action: str,
    *,
    attempt_id: int | None = None,
    input_data: dict[str, Any] | None = None,
) -> PrimitiveContext:
    return PrimitiveContext(
        instance_id=1,
        transition_name=action.removeprefix("github."),
        transition=WorkflowTransitionConfig(
            from_state="from",
            to_state="to",
            action=action,
        ),
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        issue_title="Logging path support",
        attempt_id=attempt_id,
        input_data=input_data or {},
    )


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "symphony.db")
    store.init()
    return store
