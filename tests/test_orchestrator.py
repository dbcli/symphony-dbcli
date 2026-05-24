from __future__ import annotations

from symphony_dbcli.config import default_config
from symphony_dbcli.orchestrator import build_worker_prompt


def test_code_follow_up_prompt_includes_research_context() -> None:
    prompt = build_worker_prompt(
        default_config(),
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        title="Logging support question",
        follow_up_context="Research result:\nExpand log_file before checking its parent directory.",
    )

    assert "Task type: code" in prompt
    assert "Follow-up context:" in prompt
    assert "Expand log_file" in prompt
