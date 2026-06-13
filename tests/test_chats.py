from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from symphony_dbcli.chats import (
    ChatDecision,
    ChatError,
    ChatMessageView,
    ChatThreadView,
    CodexChatAssistant,
)
from symphony_dbcli.config import CodexConfig, default_config


def test_codex_chat_assistant_uses_exec_and_parses_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        *,
        text: bool,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"action":"start_work","task_type":"code","message":"I will start the gruvbox theme."}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = replace(
        default_config(),
        codex=CodexConfig(command="codex", approval_policy="never", sandbox="workspace-write"),
    )
    assistant = CodexChatAssistant(config, tmp_path)

    decision = assistant.decide(_thread(), "Add a new syntax theme for litecli called gruvbox")

    assert decision == ChatDecision(
        action="start_work",
        task_type="code",
        message="I will start the gruvbox theme.",
    )
    command = commands[0]
    assert command[:6] == ["codex", "exec", "--cd", str(tmp_path), "--sandbox", "read-only"]
    assert 'approval_policy="never"' in command
    assert 'model_reasoning_effort="low"' in command
    assert command[command.index("--model") + 1] == "gpt-5.4-mini"
    assert "Add a new syntax theme for litecli called gruvbox" in command[-1]


def test_codex_chat_assistant_reports_invalid_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        command: list[str],
        *,
        text: bool,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="not json", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ChatError, match="chat decision JSON"):
        CodexChatAssistant(default_config(), tmp_path).decide(_thread(), "Make it better")


def _thread() -> ChatThreadView:
    return ChatThreadView(
        id=1,
        work_item_id=2,
        source_id=3,
        title="Add gruvbox",
        status="active",
        task_type="code",
        messages=[
            ChatMessageView(
                id=1,
                role="user",
                body="Add a new syntax theme for litecli called gruvbox",
                created_at="2026-06-13T12:00:00+00:00",
            )
        ],
        latest_run=None,
        created_at="2026-06-13T12:00:00+00:00",
        updated_at="2026-06-13T12:00:00+00:00",
    )
