from __future__ import annotations

from symphony_dbcli.actions import DEFAULT_ACTION_REGISTRY
from symphony_dbcli.config import default_config


def test_default_action_registry_covers_default_workflow() -> None:
    config = default_config()

    missing = [
        transition.action
        for transition in config.workflow.transitions.values()
        if not DEFAULT_ACTION_REGISTRY.contains(transition.action)
    ]

    assert missing == []


def test_action_registry_records_execution_boundaries() -> None:
    draft_pr = DEFAULT_ACTION_REGISTRY.get("github.create_draft_pr")
    fetch_issues = DEFAULT_ACTION_REGISTRY.get("github.fetch_issues")

    assert draft_pr is not None
    assert draft_pr.side_effect == "github_write"
    assert draft_pr.input_type == "DraftPullRequestRequest"
    assert draft_pr.output_type == "PullRequestSnapshot"
    assert draft_pr.idempotency_strategy == "pull_request"
    assert draft_pr.automatic_allowed is True
    assert draft_pr.human_gate_allowed is True

    assert fetch_issues is not None
    assert fetch_issues.side_effect == "github_read"
    assert fetch_issues.automatic_allowed is True
    assert fetch_issues.human_gate_allowed is False
