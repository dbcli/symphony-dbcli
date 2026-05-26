from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .clock import elapsed_ms, monotonic_ns
from .config import CodexConfig
from .store import Store


class CodexRunnerError(RuntimeError):
    """Raised when a Codex worker session fails."""


@dataclass(frozen=True)
class CodexResult:
    thread_id: str
    turn_count: int
    final_message: str
    duration_ms: int


class CodexRunner:
    def __init__(self, config: CodexConfig):
        self.config = config

    def run(self, *, prompt: str, cwd: str, attempt_id: int, store: Store) -> CodexResult:
        if self.config.transport == "exec":
            return self._run_exec(prompt=prompt, cwd=cwd, attempt_id=attempt_id, store=store)
        return self._run_app_server(prompt=prompt, cwd=cwd, attempt_id=attempt_id, store=store)

    def _run_exec(self, *, prompt: str, cwd: str, attempt_id: int, store: Store) -> CodexResult:
        started = monotonic_ns()
        thread_id = f"exec-{uuid.uuid4()}"
        store.record_timeline_event(
            attempt_id, phase="codex", event_type="started", message="Started codex exec"
        )
        command = [
            self.config.command,
            "exec",
            "--cd",
            cwd,
            "--sandbox",
            self.config.sandbox,
            "-c",
            f'approval_policy="{self.config.approval_policy}"',
        ]
        if self.config.model:
            command.extend(["--model", self.config.model])
        command.append(prompt)
        store.record_codex_event(
            attempt_id,
            thread_id=thread_id,
            event_type="exec/request",
            payload=_prompt_payload(
                self.config, thread_id=thread_id, cwd=cwd, prompt=prompt, include_sandbox=True
            ),
        )
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        ended = monotonic_ns()
        output = result.stdout.strip()
        if result.returncode != 0:
            store.record_error(
                attempt_id,
                phase="codex",
                error_type="codex_exec_failed",
                message=result.stderr.strip() or "codex exec failed",
                recoverable=False,
                log_excerpt=result.stdout[-2000:],
            )
            store.record_codex_turn(
                attempt_id,
                thread_id=thread_id,
                turn_index=1,
                status="failed",
                model=self.config.model,
                started_monotonic_ns=started,
                ended_monotonic_ns=ended,
            )
            raise CodexRunnerError(result.stderr.strip() or "codex exec failed")
        store.record_codex_turn(
            attempt_id,
            thread_id=thread_id,
            turn_index=1,
            status="completed",
            model=self.config.model,
            started_monotonic_ns=started,
            ended_monotonic_ns=ended,
        )
        store.record_timeline_event(
            attempt_id,
            phase="codex",
            event_type="completed",
            message="Finished codex exec",
            started_monotonic_ns=started,
            ended_monotonic_ns=ended,
        )
        return CodexResult(
            thread_id=thread_id, turn_count=1, final_message=output, duration_ms=elapsed_ms(started, ended)
        )

    def _run_app_server(self, *, prompt: str, cwd: str, attempt_id: int, store: Store) -> CodexResult:
        started = monotonic_ns()
        store.record_timeline_event(
            attempt_id, phase="codex", event_type="started", message="Started codex app-server"
        )
        client = _AppServerClient(self.config, store=store, attempt_id=attempt_id)
        try:
            client.start()
            client.initialize()
            thread_id = client.thread_start(cwd)
            final_message = client.turn_start(thread_id=thread_id, cwd=cwd, prompt=prompt)
            ended = monotonic_ns()
            store.record_codex_turn(
                attempt_id,
                thread_id=thread_id,
                turn_index=1,
                status="completed",
                model=self.config.model,
                started_monotonic_ns=started,
                ended_monotonic_ns=ended,
            )
            store.record_timeline_event(
                attempt_id,
                phase="codex",
                event_type="completed",
                message="Finished codex app-server turn",
                started_monotonic_ns=started,
                ended_monotonic_ns=ended,
            )
            return CodexResult(
                thread_id=thread_id,
                turn_count=1,
                final_message=final_message,
                duration_ms=elapsed_ms(started, ended),
            )
        except Exception as exc:
            ended = monotonic_ns()
            store.record_error(
                attempt_id,
                phase="codex",
                error_type=type(exc).__name__,
                message=str(exc),
                recoverable=False,
            )
            store.record_timeline_event(
                attempt_id,
                phase="codex",
                event_type="failed",
                message=str(exc),
                started_monotonic_ns=started,
                ended_monotonic_ns=ended,
            )
            raise
        finally:
            client.close()


class _AppServerClient:
    def __init__(self, config: CodexConfig, *, store: Store, attempt_id: int):
        self.config = config
        self.store = store
        self.attempt_id = attempt_id
        self.process: subprocess.Popen[str] | None = None
        self.next_id = 1
        self.final_message_parts: list[str] = []

    def start(self) -> None:
        self.process = subprocess.Popen(
            [self.config.command, "app-server", "--listen", self.config.app_server_listen],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def initialize(self) -> None:
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "symphony-dbcli",
                    "title": "Symphony DBCLI",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )

    def thread_start(self, cwd: str) -> str:
        result = self.request(
            "thread/start",
            {
                "cwd": str(Path(cwd).resolve()),
                "approvalPolicy": self.config.approval_policy,
                "sandbox": self.config.sandbox,
                "model": self.config.model or None,
                "serviceName": "symphony-dbcli",
                "ephemeral": True,
            },
        )
        thread = result.get("thread") or {}
        thread_id = thread.get("id") or thread.get("threadId")
        if not thread_id:
            raise CodexRunnerError(f"thread/start response did not include a thread id: {result!r}")
        self.store.record_codex_event(
            self.attempt_id, thread_id=thread_id, event_type="thread/start", payload=result
        )
        return str(thread_id)

    def turn_start(self, *, thread_id: str, cwd: str, prompt: str) -> str:
        payload = _prompt_payload(self.config, thread_id=thread_id, cwd=cwd, prompt=prompt)
        self.store.record_codex_event(
            self.attempt_id,
            thread_id=thread_id,
            event_type="turn/start/request",
            payload=payload,
        )
        self.request("turn/start", payload)
        self._read_until_turn_completed(thread_id)
        return "".join(self.final_message_parts).strip()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.process or not self.process.stdin:
            raise CodexRunnerError("app-server process is not running")
        request_id = self.next_id
        self.next_id += 1
        self.process.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        while True:
            message = self._read_message()
            if "id" in message and message["id"] == request_id:
                if "error" in message:
                    raise CodexRunnerError(message["error"].get("message", "app-server request failed"))
                return dict(message.get("result") or {})
            self._handle_notification(message)

    def close(self) -> None:
        if not self.process:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def _read_until_turn_completed(self, thread_id: str) -> None:
        while True:
            message = self._read_message()
            self._handle_notification(message)
            if message.get("method") == "turn/completed":
                self.store.record_codex_event(
                    self.attempt_id,
                    thread_id=thread_id,
                    event_type="turn/completed",
                    payload=dict(message.get("params") or {}),
                )
                return

    def _read_message(self) -> dict[str, Any]:
        if not self.process or not self.process.stdout:
            raise CodexRunnerError("app-server process is not running")
        line = self.process.stdout.readline()
        if not line:
            stderr = ""
            if self.process.stderr:
                stderr = self.process.stderr.read().strip()
            raise CodexRunnerError(stderr or "app-server closed stdout")
        return cast(dict[str, Any], json.loads(line))

    def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = dict(message.get("params") or {})
        if not method:
            return
        thread_id = str(params.get("threadId") or params.get("thread_id") or "")
        if thread_id:
            self.store.record_codex_event(
                self.attempt_id, thread_id=thread_id, event_type=method, payload=params
            )
        if method in {
            "agent/message/delta",
            "agent_message/delta",
            "item/agentMessage/delta",
            "item/agent_message/delta",
        }:
            delta = params.get("delta") or params.get("text") or ""
            self.final_message_parts.append(str(delta))


def _prompt_payload(
    config: CodexConfig, *, thread_id: str, cwd: str, prompt: str, include_sandbox: bool = False
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "threadId": thread_id,
        "cwd": str(Path(cwd).resolve()),
        "model": config.model or None,
        "approvalPolicy": config.approval_policy,
        "input": [{"type": "text", "text": prompt}],
    }
    if include_sandbox:
        payload["sandbox"] = config.sandbox
    return payload
