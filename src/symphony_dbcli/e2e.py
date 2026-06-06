from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .config import (
    CodexConfig,
    DashboardConfig,
    DatabaseConfig,
    GitHubConfig,
    PolicyConfig,
    ProfileConfig,
    WorkerConfig,
    WorkflowConfig,
    WorkspaceConfig,
    render_toml,
)
from .github import GitHubClient, GitHubError
from .orchestrator import Orchestrator, load_and_record_workflow
from .primitive_executor import PrimitiveContext, PrimitiveExecutor
from .review_actions import issue_link_marker
from .store import Store
from .workflow_definition import WorkflowTransitionConfig
from .worktree import safe_key

DEFAULT_FIXTURE_REPO = "amjith/symphony-dbcli-e2e-fixture"
FIXTURE_TITLE_PREFIX = "Symphony e2e"


@dataclass(frozen=True)
class FixtureScenario:
    task_type: str
    create_pr: bool
    description: str
    create_code_follow_up: bool = False
    codex_follow_up_action: str = ""
    create_associated_pr_before_claim: bool = False


FIXTURE_SCENARIOS = {
    "code_happy_path": FixtureScenario("code", True, "Code issue through draft PR creation."),
    "research_answer_review": FixtureScenario("research", False, "Research answer through human review."),
    "research_to_code_follow_up": FixtureScenario(
        "research",
        True,
        "Research answer followed by a code task and draft PR.",
        create_code_follow_up=True,
    ),
    "pr_review_comments": FixtureScenario(
        "code",
        True,
        "Code issue followed by PR review comment handling.",
        codex_follow_up_action="codex.address_pr_comments",
    ),
    "ci_failure_fix": FixtureScenario(
        "code",
        True,
        "Code issue followed by CI failure repair.",
        codex_follow_up_action="codex.fix_ci_failures",
    ),
    "associated_pr_parallel_checks": FixtureScenario(
        "code",
        False,
        "Existing associated PR is discovered, checked in parallel, and fed into PR feedback repair.",
        create_associated_pr_before_claim=True,
    ),
}


@dataclass(frozen=True)
class E2EFixtureConfig:
    repo: str = DEFAULT_FIXTURE_REPO
    root: Path = Path(".symphony/e2e")
    task_type: str = ""
    create_pr: bool = True
    reset_open_todo: bool = True
    scenario: str = "code_happy_path"


@dataclass(frozen=True)
class E2EFixtureResult:
    issue_url: str
    attempt_id: int
    workflow_path: Path
    database_path: Path
    worktree_path: str
    pull_request_url: str = ""
    follow_up_attempt_id: int | None = None
    scenario: str = "code_happy_path"


class E2EFixtureError(RuntimeError):
    """Raised when the GitHub-backed e2e fixture cannot run."""


def fixture_scenarios() -> list[str]:
    return list(FIXTURE_SCENARIOS)


def run_fixture(config: E2EFixtureConfig) -> E2EFixtureResult:
    config = _resolve_scenario(config)
    scenario = FIXTURE_SCENARIOS[config.scenario]
    _ensure_github_token()
    paths = _fixture_paths(config)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.workflow.parent.mkdir(parents=True, exist_ok=True)
    _write_fake_codex(paths.fake_codex)
    workflow_config = _workflow_config(config, paths)
    _write_fixture_workflow(paths.workflow, workflow_config)
    store = Store(workflow_config.database.path)
    store.init()
    workflow_config, workflow_version_id = load_and_record_workflow(store, paths.workflow)

    _ensure_labels(config.repo)
    if config.reset_open_todo:
        _clear_open_todo_issues(config.repo)
    issue_url = _create_issue(config.repo, config.task_type)
    issue_number = _issue_number_from_url(issue_url)
    associated_pull_request_url = ""
    if scenario.create_associated_pr_before_claim:
        associated_pull_request_url = _create_associated_pull_request(
            config.repo,
            issue_number,
            paths,
        )

    orchestrator = Orchestrator(workflow_config, store, workflow_version_id)
    _poll_until_issue_visible(orchestrator, store, config.repo, issue_number)
    attempt_id = orchestrator.claim_next()
    if attempt_id is None:
        raise E2EFixtureError(f"No eligible issue was claimed for {config.repo}#{issue_number}.")
    attempt = store.attempt_by_id(attempt_id)
    if not attempt or int(attempt["issue_number"]) != issue_number:
        raise E2EFixtureError("The fixture claimed an unexpected issue; rerun with a clean fixture repo.")
    orchestrator.run_attempt(attempt_id)

    target_attempt_id = attempt_id
    follow_up_attempt_id = None
    if scenario.create_code_follow_up:
        follow_up_attempt_id = store.create_code_follow_up_attempt(attempt_id, workflow_version_id)
        orchestrator.run_attempt(follow_up_attempt_id)
        target_attempt_id = follow_up_attempt_id

    pull_request_url = associated_pull_request_url
    target_attempt = store.attempt_by_id(target_attempt_id)
    if config.create_pr and target_attempt and str(target_attempt["task_type"]) == "code":
        gate = store.pending_workflow_gate_for_attempt(target_attempt_id, "create_draft_pr")
        if not gate:
            raise E2EFixtureError(f"Attempt {target_attempt_id} does not have a draft PR review gate.")
        orchestrator.run_human_gate(int(gate["id"]))
        pull_requests = store.pull_requests_for_attempt(target_attempt_id)
        if not pull_requests:
            raise E2EFixtureError(f"Attempt {target_attempt_id} did not record a pull request.")
        pull_request_url = str(pull_requests[0]["url"])
    if scenario.codex_follow_up_action and pull_request_url:
        _run_codex_follow_up_action(
            workflow_config, store, target_attempt_id, scenario.codex_follow_up_action
        )
    completed_attempt = store.attempt_by_id(target_attempt_id)
    worktree_path = str(completed_attempt["worktree_path"]) if completed_attempt else ""
    return E2EFixtureResult(
        issue_url=issue_url,
        attempt_id=attempt_id,
        workflow_path=paths.workflow,
        database_path=paths.database,
        worktree_path=worktree_path,
        pull_request_url=pull_request_url,
        follow_up_attempt_id=follow_up_attempt_id,
        scenario=config.scenario,
    )


def _poll_until_issue_visible(
    orchestrator: Orchestrator,
    store: Store,
    repo: str,
    issue_number: int,
) -> None:
    for _ in range(6):
        orchestrator.poll_once()
        if store.issue_detail(repo, issue_number):
            return
        time.sleep(2)
    raise E2EFixtureError(f"GitHub issue {repo}#{issue_number} was not visible to the poller.")


@dataclass(frozen=True)
class _FixturePaths:
    root: Path
    database: Path
    workflow: Path
    fake_codex: Path
    worktrees: Path
    repos: Path


def _fixture_paths(config: E2EFixtureConfig) -> _FixturePaths:
    run_key = str(time.time_ns())
    root = config.root / safe_key(config.repo)
    run_root = root / "runs" / run_key
    return _FixturePaths(
        root=root,
        database=run_root / "symphony.db",
        workflow=run_root / "WORKFLOW.md",
        fake_codex=root / "bin" / "fake-codex",
        worktrees=run_root / "worktrees",
        repos=root / "repos",
    )


def _resolve_scenario(config: E2EFixtureConfig) -> E2EFixtureConfig:
    if config.scenario not in FIXTURE_SCENARIOS:
        names = ", ".join(fixture_scenarios())
        raise E2EFixtureError(f"Unknown e2e fixture scenario {config.scenario!r}; choose one of {names}.")
    scenario = FIXTURE_SCENARIOS[config.scenario]
    return E2EFixtureConfig(
        repo=config.repo,
        root=config.root,
        task_type=config.task_type or scenario.task_type,
        create_pr=config.create_pr and scenario.create_pr,
        reset_open_todo=config.reset_open_todo,
        scenario=config.scenario,
    )


def _workflow_config(config: E2EFixtureConfig, paths: _FixturePaths) -> WorkflowConfig:
    config = _resolve_scenario(config)
    return WorkflowConfig(
        profile=ProfileConfig(active="e2e"),
        github=GitHubConfig(
            repos=[config.repo],
            auth_strategy="token",
            token_env="SYMPHONY_GITHUB_TOKEN",
            fallback_token_env="GH_TOKEN",
        ),
        workspace=WorkspaceConfig(
            root=str(paths.worktrees),
            bare_repos_root=str(paths.repos),
            retention_days=1,
        ),
        workers=WorkerConfig(
            max_global=1,
            max_per_repo=1,
            default_task_type=config.task_type,
            poll_interval_seconds=5,
            retry_limit=0,
        ),
        dashboard=DashboardConfig(host="127.0.0.1", port=8766),
        database=DatabaseConfig(path=str(paths.database)),
        codex=CodexConfig(
            command=str(paths.fake_codex),
            transport="exec",
            approval_policy="never",
        ),
        policy=PolicyConfig(dry_run=False),
        instructions=(
            "This is an e2e fixture run. Keep changes limited to the fixture task, "
            "run the local unittest suite, and summarize the result succinctly."
        ),
    )


def _run_codex_follow_up_action(
    config: WorkflowConfig,
    store: Store,
    attempt_id: int,
    action: str,
) -> None:
    attempt = store.attempt_by_id(attempt_id)
    if not attempt:
        raise E2EFixtureError(f"Attempt {attempt_id} does not exist.")
    issue = store.issue_detail(str(attempt["repo"]), int(attempt["issue_number"]))
    pull_requests = store.pull_requests_for_attempt(attempt_id)
    if not pull_requests:
        raise E2EFixtureError(f"Attempt {attempt_id} does not have a pull request for {action}.")
    transition_name = action.removeprefix("codex.")
    pull_request_number = int(pull_requests[0]["number"])
    PrimitiveExecutor(config, store).execute(
        PrimitiveContext(
            instance_id=0,
            transition_name=transition_name,
            transition=WorkflowTransitionConfig(
                from_state="fixture",
                to_state="fixture",
                action=action,
                guidance=["Keep the fixture follow-up focused and fast."],
            ),
            repo=str(attempt["repo"]),
            issue_number=int(attempt["issue_number"]),
            task_type=str(attempt["task_type"]),
            issue_title="" if not issue else str(issue["issue"]["title"]),
            attempt_id=attempt_id,
            base_repo_path=str(attempt["base_repo_path"] or ""),
            worktree_path=str(attempt["worktree_path"] or ""),
            branch=str(attempt["branch"] or ""),
            commit_sha=str(attempt["commit_sha"] or ""),
            input_data=_codex_follow_up_input(action, pull_request_number),
        )
    )


def _codex_follow_up_input(action: str, pull_request_number: int) -> dict[str, Any]:
    if action == "codex.address_pr_comments":
        return {
            "pull_request_number": pull_request_number,
            "comments": [
                {
                    "author": "symphony-fixture",
                    "body": "Please confirm the fixture test suite still passes.",
                    "url": "",
                }
            ],
        }
    if action == "codex.fix_ci_failures":
        return {
            "pull_request_number": pull_request_number,
            "failed_checks": [
                {
                    "name": "fixture-tests",
                    "status": "completed",
                    "conclusion": "failure",
                    "url": "",
                }
            ],
            "failure_context": [
                {
                    "name": "fixture-tests",
                    "conclusion": "failure",
                    "log_excerpt": "FAILED fixture test suite",
                }
            ],
            "checks": [],
        }
    return {"pull_request_number": pull_request_number}


def _write_fixture_workflow(path: Path, config: WorkflowConfig) -> None:
    data = config.to_dict()
    data["profiles"] = {
        "e2e": {
            "database": {"path": config.database.path},
            "workspace": {
                "root": config.workspace.root,
                "bare_repos_root": config.workspace.bare_repos_root,
            },
            "dashboard": {"host": config.dashboard.host, "port": config.dashboard.port},
        }
    }
    path.write_text(
        "\n".join(
            [
                "# Symphony DBCLI E2E Workflow",
                "",
                "Generated by `symphony-dbcli e2e run-fixture`.",
                "",
                "```toml",
                render_toml(data).rstrip(),
                "```",
                "",
                "## Worker Instructions",
                "",
                config.instructions,
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_fake_codex(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_CODEX_SCRIPT, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _ensure_github_token() -> None:
    if os.environ.get("SYMPHONY_GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
        return
    token = _gh(["auth", "token"]).strip()
    if not token:
        raise E2EFixtureError("Could not read a token from `gh auth token`.")
    os.environ["SYMPHONY_GITHUB_TOKEN"] = token


def _ensure_labels(repo: str) -> None:
    labels = {
        "symphony:todo": ("1f7a5a", "Dispatchable Symphony work"),
        "symphony:working": ("d99a2b", "Symphony worker claimed this issue"),
        "symphony:review": ("135f7a", "Symphony output needs review"),
        "symphony:blocked": ("9f3a38", "Blocked from Symphony dispatch"),
        "symphony:done": ("6a737d", "Symphony terminal state"),
        "symphony:type:code": ("5319e7", "Symphony coding task"),
        "symphony:type:research": ("c5def5", "Symphony research task"),
    }
    for name, (color, description) in labels.items():
        _gh(
            [
                "label",
                "create",
                name,
                "--repo",
                repo,
                "--color",
                color,
                "--description",
                description,
                "--force",
            ]
        )


def _clear_open_todo_issues(repo: str) -> None:
    raw = _gh(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--label",
            "symphony:todo",
            "--json",
            "number",
            "--limit",
            "100",
        ]
    )
    for issue in cast(list[dict[str, Any]], json.loads(raw)):
        _gh(
            [
                "issue",
                "edit",
                str(issue["number"]),
                "--repo",
                repo,
                "--remove-label",
                "symphony:todo",
            ]
        )


def _create_issue(repo: str, task_type: str) -> str:
    task_label = "symphony:type:code" if task_type == "code" else "symphony:type:research"
    title = f"{FIXTURE_TITLE_PREFIX} {task_type} task {int(time.time())}"
    body = "\n".join(
        [
            "This issue is generated by the symphony-dbcli e2e harness.",
            "",
            "For code tasks, fix `fixture_calc.add()` so the unittest suite passes.",
            "For research tasks, explain the expected fix without editing files.",
        ]
    )
    return _gh(
        [
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            title,
            "--body",
            body,
            "--label",
            "symphony:todo",
            "--label",
            task_label,
        ]
    ).strip()


def _create_associated_pull_request(repo: str, issue_number: int, paths: _FixturePaths) -> str:
    branch = f"symphony/e2e-associated-{issue_number}-{int(time.time())}"
    checkout = paths.root / "associated-pr-checkouts" / safe_key(branch)
    checkout.parent.mkdir(parents=True, exist_ok=True)
    _run(["gh", "repo", "clone", repo, str(checkout)])
    _run(["git", "-C", str(checkout), "checkout", "-b", branch])
    fixture_note = checkout / "ASSOCIATED_PR_FIXTURE.md"
    fixture_note.write_text(
        f"Associated PR fixture for {repo}#{issue_number}.\n",
        encoding="utf-8",
    )
    _run(["git", "-C", str(checkout), "add", str(fixture_note)])
    _run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=symphony-dbcli",
            "-c",
            "user.email=symphony-dbcli@users.noreply.github.com",
            "commit",
            "-m",
            f"Fixture associated PR for issue {issue_number}",
        ]
    )
    _run(["git", "-C", str(checkout), "push", "origin", branch])
    body = "\n".join(
        [
            f"Fixture associated pull request for https://github.com/{repo}/issues/{issue_number}.",
            "",
            issue_link_marker(repo, issue_number),
        ]
    )
    pr_url = _gh(
        [
            "pr",
            "create",
            "--repo",
            repo,
            "--title",
            f"Associated PR fixture for issue {issue_number}",
            "--body",
            body,
            "--head",
            branch,
            "--draft",
        ]
    ).strip()
    pr_number = _pull_request_number_from_url(pr_url)
    _gh(
        [
            "api",
            f"repos/{repo}/pulls/{pr_number}/reviews",
            "-f",
            "event=COMMENT",
            "-f",
            "body=Please confirm the fixture unittest suite still passes.",
        ]
    )
    return pr_url


def _issue_number_from_url(issue_url: str) -> int:
    match = re.search(r"/issues/(\d+)$", issue_url.strip())
    if not match:
        raise E2EFixtureError(f"Could not parse issue number from {issue_url!r}.")
    return int(match.group(1))


def _pull_request_number_from_url(pull_request_url: str) -> int:
    match = re.search(r"/pull/(\d+)$", pull_request_url.strip())
    if not match:
        raise E2EFixtureError(f"Could not parse pull request number from {pull_request_url!r}.")
    return int(match.group(1))


def _run(args: list[str]) -> str:
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise E2EFixtureError(result.stderr.strip() or f"{' '.join(args)} failed")
    return result.stdout


def _gh(args: list[str]) -> str:
    result = subprocess.run(["gh", *args], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise E2EFixtureError(result.stderr.strip() or f"gh {' '.join(args)} failed")
    return result.stdout


class _E2EGitHubClient(GitHubClient):
    def push_branch(self, *, repo: str, worktree_path: str, branch: str) -> None:
        result = subprocess.run(
            ["git", "-C", worktree_path, "push", f"git@github.com:{repo}.git", f"{branch}:{branch}"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise GitHubError(result.stderr.strip() or "git push failed")


FAKE_CODEX_SCRIPT = """#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(prog="fake-codex")
    subcommands = parser.add_subparsers(dest="command", required=True)
    exec_parser = subcommands.add_parser("exec")
    exec_parser.add_argument("--cd", required=True)
    exec_parser.add_argument("--sandbox", default="workspace-write")
    exec_parser.add_argument("-c", "--config", action="append", default=[])
    exec_parser.add_argument("--model", default="")
    exec_parser.add_argument("prompt")
    args = parser.parse_args()
    cwd = Path(args.cd)
    if "Create a draft pull request" in args.prompt:
        repo = _line_value(args.prompt, "Repository:")
        issue_url = _line_value(args.prompt, "GitHub issue:")
        branch = _line_value(args.prompt, "Branch:")
        marker = _line_value(args.prompt, "Issue link marker:")
        issue_number = _issue_number(issue_url)
        title = f"Fix fixture add behavior for issue {issue_number}"
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        if status.stdout.strip():
            _run(["git", "add", "-A"], cwd)
            _run(
                [
                    "git",
                    "-c",
                    "user.name=symphony-dbcli",
                    "-c",
                    "user.email=symphony-dbcli@users.noreply.github.com",
                    "commit",
                    "-m",
                    title,
                ],
                cwd,
            )
        _run(["git", "push", "origin", f"HEAD:{branch}"], cwd)
        body = "\\n".join(
            [
                "## Changes",
                "",
                "- Updated `fixture_calc.add()` to return the sum of both arguments.",
                "",
                "## Tests",
                "",
                "- `python -m unittest discover -v` passed.",
                "",
                f"Fixes {issue_url}",
                "",
                marker,
                "",
            ]
        )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as body_file:
            body_file.write(body)
            body_path = body_file.name
        pr = _run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--body-file",
                body_path,
                "--head",
                branch,
                "--draft",
            ],
            cwd,
        ).strip()
        print(f"Pull request: {pr}")
        return 0
    if "Task type: code" in args.prompt:
        source = cwd / "fixture_calc.py"
        source.write_text(
            source.read_text(encoding="utf-8").replace("left - right", "left + right"),
            encoding="utf-8",
        )
        result = subprocess.run(
            ["python", "-m", "unittest", "discover", "-v"],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr)
            return result.returncode
        print("Summary:")
        print("- Updated `fixture_calc.add()` to return the sum of both arguments.")
        print("- Verified the fixture unittest suite.")
        print()
        print("Checks run:")
        print("- `python -m unittest discover -v` passed.")
        return 0
    print("Summary:")
    print("- Researched the fixture issue and identified that `fixture_calc.add()` should return `left + right`.")
    print()
    print("Checks run:")
    print("- No code changes were made for this research task.")
    return 0


def _line_value(prompt: str, label: str) -> str:
    for line in prompt.splitlines():
        if line.startswith(label):
            return line.removeprefix(label).strip()
    return ""


def _issue_number(issue_url: str) -> str:
    match = re.search(r"/issues/(\\d+)$", issue_url)
    return match.group(1) if match else "unknown"


def _run(args: list[str], cwd: Path) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise SystemExit(result.returncode)
    return result.stdout


if __name__ == "__main__":
    raise SystemExit(main())
"""
