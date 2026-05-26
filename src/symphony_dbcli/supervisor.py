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
from .db import create_db_engine, create_session_factory
from .models import create_model_tables
from .store import Store
from .work_items import WorkItemRepository

MAX_LOG_EXCERPT_BYTES = 12_000


class WorkerProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


type ProcessFactory = Callable[[Sequence[str], Path], WorkerProcess]


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
        self.worker_log_dir = Path(store.path).parent / "worker-logs"
        engine = create_db_engine(store.path)
        create_model_tables(engine)
        self.work_items = WorkItemRepository(create_session_factory(engine))

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
            self.work_items.start_attempt_run(attempt_id)
            command = self._worker_command(attempt_id)
            log_path = self._worker_log_path(worker_id)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                process = self.process_factory(command, log_path)
            except Exception as exc:
                self.store.record_error(
                    attempt_id,
                    phase="supervisor",
                    error_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=False,
                )
                self.store.finish_attempt(attempt_id, "failed", "spawn_failed")
                self.work_items.finish_attempt_run(attempt_id, status="failed", outcome="spawn_failed")
                continue
            self.store.update_worker_pid(worker_id, process.pid)
            self.store.record_timeline_event(
                attempt_id,
                phase="supervisor",
                event_type="spawned",
                message=worker_id,
                data={"pid": process.pid, "command": list(command), "log_path": str(log_path)},
            )
            self.processes[worker_id] = process
            counts[repo] = counts.get(repo, 0) + 1
            counts["*"] = counts.get("*", 0) + 1
            started += 1
        return DispatchResult(started=started)

    def terminate_tracked(self, config: WorkflowConfig) -> int:
        stopped = 0
        for worker_id, process in list(self.processes.items()):
            process.terminate()
            deadline = time.monotonic() + config.workers.shutdown_grace_seconds
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            if process.poll() is None:
                process.kill()
            self.processes.pop(worker_id, None)
            stopped += 1
        return stopped

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

    def _worker_log_path(self, worker_id: str) -> Path:
        return self.worker_log_dir / f"{_safe_log_name(worker_id)}.log"

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
        log_excerpt = _read_log_excerpt(self._worker_log_path(worker_id))
        self.store.mark_worker_exited(worker_id, exit_code, stop_reason=reason)
        self.store.fail_running_workflow_action_runs(attempt_id, error=message)
        self.store.record_error(
            attempt_id,
            phase="supervisor",
            error_type=reason,
            message=message,
            recoverable=should_retry,
            log_excerpt=log_excerpt,
        )
        if should_retry:
            self.store.requeue_attempt_for_retry(attempt_id, reason=reason)
            self.work_items.requeue_attempt_run(attempt_id, reason=reason)
            self.store.record_timeline_event(
                attempt_id,
                phase="queue",
                event_type="retry_queued",
                message=f"Retry after worker {worker_id} {reason}",
                data={"worker_id": worker_id, "reason": reason},
            )
        else:
            self.store.finish_attempt(attempt_id, "failed", reason)
            self.work_items.finish_attempt_run(attempt_id, status="failed", outcome=reason)
            instance = self.store.workflow_instance_for_attempt(attempt_id)
            if instance and str(instance["status"]) == "active":
                self.store.fail_workflow_instance(
                    int(instance["id"]),
                    workflow_version_id=workflow_version_id,
                    message=message,
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


def _default_process_factory(command: Sequence[str], log_path: Path) -> WorkerProcess:
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    with log_path.open("wb") as log_file:
        return subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT, env=env)


def _read_log_excerpt(path: Path) -> str:
    try:
        with path.open("rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            size = log_file.tell()
            start = max(size - MAX_LOG_EXCERPT_BYTES, 0)
            log_file.seek(start)
            content = log_file.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not content:
        return ""
    if start > 0:
        return f"[last {MAX_LOG_EXCERPT_BYTES} bytes of {path}]\n{content}"
    return content


def _safe_log_name(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
    return safe or "worker"


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
