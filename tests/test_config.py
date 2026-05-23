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
    assert config.dashboard.host == "127.0.0.1"
    assert "Workers should be direct" in config.instructions


def test_rendered_workflow_includes_local_and_prod_profiles() -> None:
    workflow = render_workflow(default_config())

    assert '[profile]\nactive = "local"' in workflow
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
