from __future__ import annotations

import pytest

from symphony_dbcli.config import (
    WorkflowError,
    default_config,
    default_config_for_profile,
    parse_workflow,
    render_workflow,
    validate_config,
)


def clear_profile_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYMPHONY_PROFILE", raising=False)


def test_rendered_workflow_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_profile_env(monkeypatch)
    workflow = render_workflow(default_config())

    config = parse_workflow(workflow)

    assert config.tracker.kind == "github"
    assert config.github.repos == ["dbcli/pgcli", "dbcli/mycli", "dbcli/litecli"]
    assert config.profile.active == "local"
    assert config.database.path == ".symphony/symphony.db"
    assert config.workspace.strategy == "worktree"
    assert config.workspace.root == ".symphony/worktrees"
    assert config.workspace.bare_repos_root == ".symphony/repos"
    assert config.workspace.branch_prefix == "symphony"
    assert config.dashboard.host == "127.0.0.1"
    assert config.policy.dry_run is True
    assert config.workflow.initial_state == "todo"
    assert config.workflow.transitions["fix_issue"].action == "codex.fix_issue"
    assert config.workflow.transitions["create_draft_pr"].trigger == "human"
    assert config.preferences.preferred_test_strategy == "unit"
    assert config.preferences.run_review_after_code_change is True
    assert config.setup.enabled is True
    assert "post_research_answers" not in workflow
    assert "open_pull_requests" not in workflow
    assert "Workers should be direct" in config.instructions


def test_rendered_workflow_includes_local_and_prod_profiles() -> None:
    workflow = render_workflow(default_config())

    assert '[profile]\nactive = "local"' in workflow
    assert "[workflow]" in workflow
    assert "[workflow.transitions.fix_issue]" in workflow
    assert 'action = "codex.fix_issue"' in workflow
    assert "[preferences]" in workflow
    assert "[profiles.local.database]" in workflow
    assert 'path = ".symphony/symphony.db"' in workflow
    assert "[profiles.prod.database]" in workflow
    assert 'path = "/srv/symphony/symphony.db"' in workflow


def test_explicit_profile_overrides_workflow_defaults() -> None:
    workflow = render_workflow(default_config())

    config = parse_workflow(workflow, profile="prod")

    assert config.profile.active == "prod"
    assert config.database.path == "/srv/symphony/symphony.db"
    assert config.workspace.root == "/srv/symphony/worktrees"
    assert config.workspace.bare_repos_root == "/srv/symphony/repos"
    assert config.dashboard.host == "0.0.0.0"


def test_env_profile_overrides_workflow_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_profile_env(monkeypatch)
    workflow = render_workflow(default_config())
    monkeypatch.setenv("SYMPHONY_PROFILE", "prod")

    config = parse_workflow(workflow)

    assert config.profile.active == "prod"
    assert config.database.path == "/srv/symphony/symphony.db"


def test_cli_profile_beats_env_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_profile_env(monkeypatch)
    workflow = render_workflow(default_config())
    monkeypatch.setenv("SYMPHONY_PROFILE", "prod")

    config = parse_workflow(workflow, profile="local")

    assert config.profile.active == "local"
    assert config.database.path == ".symphony/symphony.db"


def test_default_config_for_profile_applies_profile_defaults() -> None:
    config = default_config_for_profile(profile="prod")

    assert config.profile.active == "prod"
    assert config.database.path == "/srv/symphony/symphony.db"
    assert config.workspace.root == "/srv/symphony/worktrees"


def test_workflow_active_profile_is_used_without_override(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_profile_env(monkeypatch)
    workflow = render_workflow(default_config()).replace('active = "local"', 'active = "prod"')

    config = parse_workflow(workflow)

    assert config.profile.active == "prod"
    assert config.database.path == "/srv/symphony/symphony.db"


def test_workflow_validation_rejects_unknown_profile() -> None:
    workflow = render_workflow(default_config())

    with pytest.raises(WorkflowError, match="Profile 'staging' is not defined"):
        parse_workflow(workflow, profile="staging")


def test_workflow_validation_rejects_missing_toml_block() -> None:
    with pytest.raises(WorkflowError, match="fenced toml"):
        parse_workflow("# no config here")


def test_workflow_validation_rejects_invalid_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_profile_env(monkeypatch)
    workflow = render_workflow(default_config()).replace('"dbcli/pgcli"', '"not a repo"')

    with pytest.raises(WorkflowError, match="invalid repository"):
        validate_config(parse_workflow(workflow))


def test_workflow_accepts_legacy_disabled_side_effect_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_profile_env(monkeypatch)
    workflow = render_workflow(default_config()).replace(
        "dry_run = true",
        "post_research_answers = false\nopen_pull_requests = false\ndry_run = true",
    )

    config = parse_workflow(workflow)

    assert config.policy.dry_run is True


def test_workflow_rejects_enabled_side_effect_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_profile_env(monkeypatch)
    workflow = render_workflow(default_config()).replace(
        "dry_run = true",
        "post_research_answers = true\nopen_pull_requests = true\ndry_run = true",
    )

    with pytest.raises(WorkflowError, match="no longer configurable"):
        parse_workflow(workflow)


def test_workflow_validation_rejects_unknown_action(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_profile_env(monkeypatch)
    workflow = render_workflow(default_config()).replace(
        'action = "codex.fix_issue"',
        'action = "codex.nope"',
    )

    with pytest.raises(WorkflowError, match="not a known primitive"):
        parse_workflow(workflow)


def test_workflow_validation_rejects_human_transition_without_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_profile_env(monkeypatch)
    workflow = render_workflow(default_config()).replace(
        'gate = "review_diff"',
        'gate = ""',
    )

    with pytest.raises(WorkflowError, match="gate is required"):
        parse_workflow(workflow)


def test_workflow_accepts_setup_steps_and_preferences(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_profile_env(monkeypatch)
    workflow = (
        render_workflow(default_config())
        .replace(
            "[workflow]\n",
            "\n".join(
                [
                    "[setup.steps.install_deps]",
                    'command = ["uv", "sync"]',
                    'description = "Install dependencies before running tests."',
                    'run = "per_repo"',
                    'cwd = "workspace"',
                    "timeout_seconds = 300",
                    "blocks_worker = true",
                    "",
                    "[workflow]",
                    "",
                ]
            ),
        )
        .replace('preferred_test_strategy = "unit"', 'preferred_test_strategy = "balanced"')
    )

    config = parse_workflow(workflow)

    assert config.preferences.preferred_test_strategy == "balanced"
    assert config.setup.steps["install_deps"].command == ["uv", "sync"]
    assert config.setup.steps["install_deps"].run == "per_repo"
    assert config.setup.steps["install_deps"].blocks_worker is True


def test_workflow_validation_rejects_invalid_setup_step(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_profile_env(monkeypatch)
    workflow = render_workflow(default_config()).replace(
        "[workflow]\n",
        "\n".join(
            [
                "[setup.steps.install_deps]",
                "command = []",
                "timeout_seconds = 0",
                "",
                "[workflow]",
                "",
            ]
        ),
    )

    with pytest.raises(WorkflowError, match="command must include at least one argument"):
        parse_workflow(workflow)
