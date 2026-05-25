from __future__ import annotations

import os
from pathlib import Path

import pytest

from symphony_dbcli.e2e import (
    DEFAULT_FIXTURE_REPO,
    E2EFixtureConfig,
    E2EFixtureError,
    _fixture_paths,
    _issue_number_from_url,
    _pull_request_number_from_url,
    _resolve_scenario,
    _workflow_config,
    run_fixture,
)


def test_fixture_workflow_uses_fast_local_paths(tmp_path: Path) -> None:
    config = E2EFixtureConfig(repo="amjith/symphony-dbcli-e2e-fixture", root=tmp_path)
    paths = _fixture_paths(config)

    workflow = _workflow_config(config, paths)

    assert workflow.github.repos == ["amjith/symphony-dbcli-e2e-fixture"]
    assert workflow.github.auth_strategy == "token"
    assert workflow.policy.dry_run is False
    assert workflow.codex.transport == "exec"
    assert workflow.codex.command == str(paths.fake_codex)
    assert workflow.workers.poll_interval_seconds == 5
    assert workflow.database.path == str(paths.database)
    assert str(paths.worktrees).startswith(str(tmp_path))


def test_fixture_scenario_resolves_task_behavior(tmp_path: Path) -> None:
    research = _resolve_scenario(
        E2EFixtureConfig(
            repo="amjith/symphony-dbcli-e2e-fixture",
            root=tmp_path,
            scenario="research_answer_review",
        )
    )

    assert research.task_type == "research"
    assert research.create_pr is False

    associated_pr = _resolve_scenario(
        E2EFixtureConfig(
            repo="amjith/symphony-dbcli-e2e-fixture",
            root=tmp_path,
            scenario="associated_pr_parallel_checks",
        )
    )

    assert associated_pr.task_type == "code"
    assert associated_pr.create_pr is False

    with pytest.raises(E2EFixtureError, match="Unknown e2e fixture scenario"):
        _resolve_scenario(E2EFixtureConfig(root=tmp_path, scenario="unknown"))


def test_issue_number_from_url() -> None:
    assert _issue_number_from_url("https://github.com/amjith/symphony-dbcli-e2e-fixture/issues/42") == 42


def test_issue_number_from_url_rejects_invalid_url() -> None:
    with pytest.raises(E2EFixtureError, match="Could not parse"):
        _issue_number_from_url("https://github.com/amjith/symphony-dbcli-e2e-fixture")


def test_pull_request_number_from_url() -> None:
    assert _pull_request_number_from_url("https://github.com/amjith/symphony-dbcli-e2e-fixture/pull/17") == 17


def test_run_fixture_against_github_fixture_repo(tmp_path: Path) -> None:
    if not os.environ.get("SYMPHONY_RUN_GITHUB_E2E"):
        pytest.skip("set SYMPHONY_RUN_GITHUB_E2E=1 to run the GitHub-backed fixture")

    result = run_fixture(
        E2EFixtureConfig(
            repo=DEFAULT_FIXTURE_REPO,
            root=tmp_path,
            scenario="code_happy_path",
        )
    )

    assert result.issue_url.startswith(f"https://github.com/{DEFAULT_FIXTURE_REPO}/issues/")
    assert result.attempt_id > 0
    assert result.workflow_path.exists()
    assert result.database_path.exists()
    assert result.worktree_path


def test_run_fixture_associated_pr_review_against_github_fixture_repo(tmp_path: Path) -> None:
    if not os.environ.get("SYMPHONY_RUN_GITHUB_E2E"):
        pytest.skip("set SYMPHONY_RUN_GITHUB_E2E=1 to run the GitHub-backed fixture")

    result = run_fixture(
        E2EFixtureConfig(
            repo=DEFAULT_FIXTURE_REPO,
            root=tmp_path,
            scenario="associated_pr_parallel_checks",
        )
    )

    assert result.issue_url.startswith(f"https://github.com/{DEFAULT_FIXTURE_REPO}/issues/")
    assert result.pull_request_url.startswith(f"https://github.com/{DEFAULT_FIXTURE_REPO}/pull/")
    assert result.worktree_path
