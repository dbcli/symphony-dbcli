from __future__ import annotations

from symphony_dbcli.config import default_config
from symphony_dbcli.review_actions import PullRequestSourceContext, source_item_link_marker
from symphony_dbcli.worker_prompt import build_pull_request_prompt, build_worker_prompt


def test_worker_prompt_requests_reviewable_pr_title_and_body_for_issue() -> None:
    prompt = build_worker_prompt(
        default_config(),
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        title="Logging path support",
    )

    assert "GitHub issue: https://github.com/dbcli/litecli/issues/245" in prompt
    assert "a succinct work summary, no more than 5 bullets total" in prompt
    assert "a `PR title:` line that names the actual code change" in prompt
    assert "a `PR body:` section with `## Changes`" in prompt
    assert "do not make it only a `Fixes` line or issue URL" in prompt


def test_worker_prompt_describes_local_ticket_without_fake_issue_url() -> None:
    source_context = PullRequestSourceContext(
        kind="local_ticket",
        source_item_id=36,
        source_item_number=1,
        title="Remove codex review action",
    )

    prompt = build_worker_prompt(
        default_config(),
        repo="dbcli/litecli",
        issue_number=1_000_000_036,
        task_type="code",
        title="Remove codex review action",
        source_context=source_context,
    )

    assert "Local ticket: Ticket #1" in prompt
    assert "Ticket title: Remove codex review action" in prompt
    assert source_item_link_marker(36) in prompt
    assert "a `PR title:` line that names the actual code change" in prompt
    assert "a `PR body:` section with `## Changes`" in prompt
    assert "at least one concrete change detail" in prompt
    assert "https://github.com/dbcli/litecli/issues/1000000036" not in prompt
    assert "GitHub issue:" not in prompt


def test_worker_prompt_describes_conversation_without_fake_issue_url() -> None:
    source_context = PullRequestSourceContext(
        kind="conversation",
        source_item_id=42,
        source_item_number=3,
        title="Explore interactive chat",
    )

    prompt = build_worker_prompt(
        default_config(),
        repo="dbcli/litecli",
        issue_number=2_000_000_042,
        task_type="code",
        title="Explore interactive chat",
        source_context=source_context,
    )

    assert "Conversation: Conversation #3" in prompt
    assert "Conversation title: Explore interactive chat" in prompt
    assert source_item_link_marker(42) in prompt
    assert "https://github.com/dbcli/litecli/issues/2000000042" not in prompt
    assert "GitHub issue:" not in prompt


def test_research_worker_prompt_requires_reply_text_not_filesystem_only() -> None:
    prompt = build_worker_prompt(
        default_config(),
        repo="dbcli/litecli",
        issue_number=245,
        task_type="research",
        title="Logging path support",
    )

    assert "Research response requirements:" in prompt
    assert "complete user-facing draft reply in your final response" in prompt
    assert "Do not save the draft only to a filesystem path" in prompt
    assert "dashboard users cannot access VM-local files" in prompt


def test_pull_request_prompt_requires_ticket_marker_without_closing_issue() -> None:
    source_context = PullRequestSourceContext(
        kind="local_ticket",
        source_item_id=36,
        source_item_number=1,
        title="Remove codex review action",
    )

    prompt = build_pull_request_prompt(
        default_config(),
        repo="dbcli/litecli",
        issue_number=1_000_000_036,
        title="Remove codex review action",
        worktree_path="/tmp/worktree",
        branch="symphony/test",
        commit_sha="abc123",
        worker_result="Summary:\n- Removed the Codex review workflow.",
        issue_link_marker="<!-- symphony-dbcli:issue-link=https://github.com/dbcli/litecli/issues/1000000036 -->",
        source_context=source_context,
    )

    assert "Symphony ticket marker:" in prompt
    assert source_item_link_marker(36) in prompt
    assert "Write a specific pull request title that names the actual code change" in prompt
    assert "Do not make the pull request description only a closing issue line" in prompt
    assert "Do not add a GitHub issue URL or closing keyword" in prompt
    assert "https://github.com/dbcli/litecli/issues/1000000036" not in prompt
    assert "Issue link marker:" not in prompt
