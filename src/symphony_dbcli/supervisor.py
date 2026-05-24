from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .clock import parse_utc
from .config import WorkflowConfig
from .store import Store


class WorkerProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


type ProcessFactory = Callable[[Sequence[str]], WorkerProcess]


@dataclass(frozen=True)
class DispatchResult:
    started: int = 0
    crashed: int = 0
    timed_out: int = 0
    retried: int = 0


class WorkerSupervisor:
    def __init__(
        self,
        store: Store,
        *,
        workflow_path: str | Path,
        profile: str | None,
        process_factory: ProcessFactory | None = None,
    ):
        self.store = store
        self.workflow_path = str(workflow_path)
        self.profile = profile
        self.process_factory = process_factory or _default_process_factory
        self.processes: dict[str, WorkerProcess] = {}

    def reconcile(self, config: WorkflowConfig, workflow_version_id: int | None) -> DispatchResult:
        crashed = 0
        timed_out = 0
        retried = 0
        now = datetime.now(UTC)
        for worker in self.store.running_workers():
            worker_id = str(worker["worker_id"])
            attempt_id = int(worker["attempt_id"])
            process = self.processes.get(worker_id)
            exit_code = process.poll() if process else None
            if exit_code is not None:
                crashed += 1
                retried += int(
                    self._fail_and_retry(
                        worker_id,
                        attempt_id,
                        "crashed",
                        f"Worker process exited with code {exit_code}.",
                        workflow_version_id,
                        config,
                        exit_code=exit_code,
                    )
                )
                self.processes.pop(worker_id, None)
                continue
            pid = _pid_from_value(worker["pid"])
            if process is None and pid > 0 and not _pid_exists(pid):
                crashed += 1
                retried += int(
                    self._fail_and_retry(
                        worker_id,
                        attempt_id,
                        "crashed",
                        "Worker process is no longer running.",
                        workflow_version_id,
                        config,
                    )
                )
                continue
            if self._is_stale(worker["heartbeat_at"], config, now) or self._is_past_deadline(
                worker["deadline_at"],
                now,
            ):
                timed_out += 1
                self._terminate(worker_id, worker["pid"], config.workers.shutdown_grace_seconds)
                retried += int(
                    self._fail_and_retry(
                        worker_id,
                        attempt_id,
                        "timed_out",
                        "Worker exceeded heartbeat or runtime deadline.",
                        workflow_version_id,
                        config,
                    )
                )
                self.processes.pop(worker_id, None)
        return DispatchResult(crashed=crashed, timed_out=timed_out, retried=retried)

    def start_queued(self, config: WorkflowConfig) -> DispatchResult:
        counts = self.store.running_attempt_counts()
        started = 0
        for attempt in self.store.queued_attempts(limit=config.workers.max_global):
            repo = str(attempt["repo"])
            if counts.get("*", 0) >= config.workers.max_global:
                break
            if counts.get(repo, 0) >= config.workers.max_per_repo:
                continue
            attempt_id = int(attempt["id"])
            worker_id = str(attempt["worker_id"] or f"worker-{attempt_id}-{time.time_ns():x}")
            self.store.start_attempt(
                attempt_id,
                worker_id,
                max_runtime_seconds=config.workers.max_runtime_seconds,
            )
            command = self._worker_command(attempt_id)
            try:
                process = self.process_factory(command)
            except Exception as exc:
                self.store.record_error(
                    attempt_id,
                    phase="supervisor",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=False,
                )
                self.store.finish_attempt(attempt_id, "failed", "spawn_failed")
                continue
            self.store.update_worker_pid(worker_id, process.pid)
            self.store.record_timeline_event(
                attempt_id,
                phase="supervisor",
                event_type="spawned",
                message=worker_id,
                data={"pid": process.pid, "command": list(command)},
            )
            self.processes[worker_id] = process
            counts[repo] = counts.get(repo, 0) + 1
            counts["*"] = counts.get("*", 0) + 1
            started += 1
        return DispatchResult(started=started)

    def _worker_command(self, attempt_id: int) -> list[str]:
        command = [
            sys.executable,
            "-m",
            "symphony_dbcli.cli",
            "--workflow",
            self.workflow_path,
        ]
        if self.profile:
            command.extend(["--profile", self.profile])
        command.extend(["worker", "run-attempt", "--attempt-id", str(attempt_id)])
        return command

    def _fail_and_retry(
        self,
        worker_id: str,
        attempt_id: int,
        reason: str,
        message: str,
        workflow_version_id: int | None,
        config: WorkflowConfig,
        *,
        exit_code: int | None = None,
    ) -> bool:
        should_retry = self._should_retry(attempt_id, config)
        self.store.mark_worker_exited(worker_id, exit_code, stop_reason=reason)
        self.store.record_error(
            attempt_id,
            phase="supervisor",
            error_type=reason,
            message=message,
            recoverable=should_retry,
        )
        self.store.finish_attempt(attempt_id, "failed", reason)
        if should_retry:
            retry_attempt_id = self.store.create_retry_attempt(attempt_id, workflow_version_id)
            if retry_attempt_id:
                self.store.record_timeline_event(
                    retry_attempt_id,
                    phase="queue",
                    event_type="retry_queued",
                    message=f"Retry after attempt {attempt_id} {reason}",
                    data={"parent_attempt_id": attempt_id},
                )
        return should_retry

    def _should_retry(self, attempt_id: int, config: WorkflowConfig) -> bool:
        attempt = self.store.attempt_by_id(attempt_id)
        if not attempt:
            return False
        return int(attempt["retry_count"] or 0) < config.workers.retry_limit

    def _is_stale(self, heartbeat_at: object, config: WorkflowConfig, now: datetime) -> bool:
        if not heartbeat_at:
            return False
        age = (now - parse_utc(str(heartbeat_at))).total_seconds()
        return age > config.workers.heartbeat_timeout_seconds

    def _is_past_deadline(self, deadline_at: object, now: datetime) -> bool:
        return bool(deadline_at) and now > parse_utc(str(deadline_at))

    def _terminate(self, worker_id: str, pid_value: object, grace_seconds: int) -> None:
        pid = _pid_from_value(pid_value)
        process = self.processes.get(worker_id)
        if process:
            process.terminate()
            deadline = time.monotonic() + grace_seconds
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            if process.poll() is None:
                process.kill()
            return
        if pid > 0:
            _terminate_pid(pid, grace_seconds)


def _default_process_factory(command: Sequence[str]) -> WorkerProcess:
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _terminate_pid(pid: int, grace_seconds: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_seconds
    while _pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _pid_exists(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return


def _pid_from_value(value: object) -> int:
    return int(value) if isinstance(value, int | str) and value else 0


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
