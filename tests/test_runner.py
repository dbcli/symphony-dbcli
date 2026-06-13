from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from symphony_dbcli.config import CodexConfig
from symphony_dbcli.runner import _AppServerClient
from symphony_dbcli.store import IssueSnapshot, Store


def test_app_server_turn_start_logs_prompt_before_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/litecli",
            number=245,
            title="Logging support question",
            url="https://github.com/dbcli/litecli/issues/245",
            state="open",
            labels=["symphony:todo"],
            task_type="code",
        )
    )
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
    )
    config = CodexConfig(model="gpt-5.4-mini", approval_policy="never")
    client = _AppServerClient(config, store=store, attempt_id=attempt_id)
    prompt = "Fix issue #245 and run the relevant tests."
    requests: list[tuple[str, dict[str, Any]]] = []

    def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        detail = store.attempt_detail(attempt_id)
        assert detail is not None
        assert detail["prompts"][0]["prompt"] == prompt
        requests.append((method, params))
        return {}

    def fake_read_until_turn_completed(thread_id: str) -> None:
        assert thread_id == "thread-1"

    monkeypatch.setattr(client, "request", fake_request)
    monkeypatch.setattr(client, "_read_until_turn_completed", fake_read_until_turn_completed)

    client.turn_start(thread_id="thread-1", cwd=str(tmp_path), prompt=prompt)

    assert requests == [
        (
            "turn/start",
            {
                "threadId": "thread-1",
                "cwd": str(tmp_path.resolve()),
                "model": "gpt-5.4-mini",
                "approvalPolicy": "never",
                "input": [{"type": "text", "text": prompt}],
            },
        )
    ]


def test_app_server_thread_start_can_create_persistent_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _store, _attempt_id = _app_server_client(
        tmp_path,
        CodexConfig(model="gpt-5.4-mini", approval_policy="never", sandbox="workspace-write"),
    )
    requests: list[tuple[str, dict[str, Any]]] = []

    def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        requests.append((method, params))
        return {"thread": {"id": "thread-persistent"}}

    monkeypatch.setattr(client, "request", fake_request)

    thread_id = client.thread_start(str(tmp_path), ephemeral=False)

    assert thread_id == "thread-persistent"
    assert requests == [
        (
            "thread/start",
            {
                "cwd": str(tmp_path.resolve()),
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
                "model": "gpt-5.4-mini",
                "serviceName": "symphony-dbcli",
                "ephemeral": False,
            },
        )
    ]


def test_app_server_thread_resume_requests_existing_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _store, _attempt_id = _app_server_client(
        tmp_path,
        CodexConfig(model="gpt-5.4-mini", approval_policy="never", sandbox="workspace-write"),
    )
    requests: list[tuple[str, dict[str, Any]]] = []

    def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        requests.append((method, params))
        return {"thread": {"id": "thread-existing"}}

    monkeypatch.setattr(client, "request", fake_request)

    thread_id = client.thread_resume("thread-existing", str(tmp_path))

    assert thread_id == "thread-existing"
    assert requests == [
        (
            "thread/resume",
            {
                "threadId": "thread-existing",
                "cwd": str(tmp_path.resolve()),
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
                "model": "gpt-5.4-mini",
                "excludeTurns": True,
            },
        )
    ]


def test_app_server_client_tracks_latest_token_usage(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/litecli",
            number=245,
            title="Logging support question",
            url="https://github.com/dbcli/litecli/issues/245",
            state="open",
            labels=["symphony:todo"],
            task_type="code",
        )
    )
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
    )
    config = CodexConfig(model="gpt-5.4-mini", approval_policy="never")
    client = _AppServerClient(config, store=store, attempt_id=attempt_id)

    client._handle_notification(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "tokenUsage": {
                    "total": {
                        "cachedInputTokens": 100,
                        "inputTokens": 2000,
                        "outputTokens": 500,
                        "reasoningOutputTokens": 125,
                        "totalTokens": 2500,
                    }
                },
            },
        }
    )

    assert client.latest_token_usage is not None
    assert client.latest_token_usage.input_tokens == 2000
    assert client.latest_token_usage.output_tokens == 500
    assert client.latest_token_usage.total_tokens == 2500
    assert client.latest_token_usage.cached_input_tokens == 100
    assert client.latest_token_usage.reasoning_output_tokens == 125


def test_app_server_client_separates_agent_message_items(tmp_path: Path) -> None:
    store = Store(tmp_path / "symphony.db")
    store.init()
    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/litecli",
            number=245,
            title="Logging support question",
            url="https://github.com/dbcli/litecli/issues/245",
            state="open",
            labels=["symphony:todo"],
            task_type="code",
        )
    )
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
    )
    client = _AppServerClient(CodexConfig(), store=store, attempt_id=attempt_id)

    client._handle_notification(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread-1",
                "itemId": "message-1",
                "delta": "I found the source.",
            },
        }
    )
    client._handle_notification(
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread-1",
                "itemId": "message-2",
                "delta": "**Summary**\n- Fixed the rendering.",
            },
        }
    )

    assert "".join(client.final_message_parts) == (
        "I found the source.\n\n**Summary**\n- Fixed the rendering."
    )


def _app_server_client(
    tmp_path: Path,
    config: CodexConfig | None = None,
) -> tuple[_AppServerClient, Store, int]:
    store = Store(tmp_path / "symphony.db")
    store.init()
    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/litecli",
            number=245,
            title="Logging support question",
            url="https://github.com/dbcli/litecli/issues/245",
            state="open",
            labels=["symphony:todo"],
            task_type="code",
        )
    )
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
    )
    client = _AppServerClient(config or CodexConfig(), store=store, attempt_id=attempt_id)
    return client, store, attempt_id
