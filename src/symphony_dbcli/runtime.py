from __future__ import annotations

import os
import socket
import sqlite3
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .actions import PrimitiveSideEffect
from .config import WorkflowConfig
from .orchestrator import Orchestrator, WorkflowWatcher, WorktreeCleanupSummary
from .store import Store
from .supervisor import DispatchResult, WorkerSupervisor


@dataclass(frozen=True)
class RuntimeCycleResult:
    trigger: str
    status: str
    started_at: str
    completed_at: str | None = None
    workflow_changed: bool = False
    synced: int = 0
    advanced: int = 0
    claimed: int = 0
    workers_started: int = 0
    workers_crashed: int = 0
    workers_timed_out: int = 0
    workers_retried: int = 0
    cleanup_scanned: int = 0
    cleanup_merged: int = 0
    cleanup_cleaned: int = 0
    cleanup_skipped: int = 0
    cleanup_errors: int = 0
    error: str = ""
    skipped_reason: str = ""

    @classmethod
    def busy(cls, trigger: str) -> RuntimeCycleResult:
        now = _utc_now()
        return cls(
            trigger=trigger,
            status="busy",
            started_at=now,
            completed_at=now,
            error="Another orchestration cycle is already running.",
        )

    @classmethod
    def failed(cls, trigger: str, started_at: str, error: str) -> RuntimeCycleResult:
        return cls(
            trigger=trigger,
            status="failed",
            started_at=started_at,
            completed_at=_utc_now(),
            error=error,
        )

    @classmethod
    def skipped_cycle(cls, trigger: str, reason: str) -> RuntimeCycleResult:
        now = _utc_now()
        return cls(
            trigger=trigger,
            status="skipped",
            started_at=now,
            completed_at=now,
            skipped_reason=reason,
        )

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"

    @property
    def started(self) -> int:
        return self.workers_started

    @property
    def retried(self) -> int:
        return self.workers_retried

    @property
    def cleaned_worktrees(self) -> int:
        return self.cleanup_cleaned

    @property
    def cleanup_error(self) -> str:
        if self.cleanup_errors == 0:
            return ""
        return f"{self.cleanup_errors} worktree cleanup error(s)"


@dataclass(frozen=True)
class RuntimeStatus:
    enabled: bool
    running: bool
    polling_enabled: bool
    cycle_running: bool
    profile: str
    poll_interval_seconds: int
    next_cycle_at: str
    last_cycle: RuntimeCycleResult | None
    queued_attempts: int
    running_attempts: int
    workers: list[RuntimeWorkerView]
    leader: bool = True
    lock_owner: str = ""


@dataclass(frozen=True)
class RuntimeWorkerView:
    worker_id: str
    attempt_id: int
    repo: str
    issue_number: int
    task_type: str
    pid: int
    heartbeat_at: str
    deadline_at: str
    started_at: str
    retry_count: int


class RuntimeOrchestrator(Protocol):
    def poll_once(self) -> int: ...

    def cleanup_merged_pull_request_worktrees(self) -> WorktreeCleanupSummary: ...

    def advance_ready_workflow_instances(
        self,
        *,
        allowed_side_effects: set[PrimitiveSideEffect] | None = None,
    ) -> int: ...

    def claim_available(self) -> int: ...


class RuntimeWorkflowWatcher(Protocol):
    def reload_if_changed(self) -> tuple[WorkflowConfig, int, bool]: ...


class RuntimeSupervisor(Protocol):
    def reconcile(self, config: WorkflowConfig, workflow_version_id: int | None) -> DispatchResult: ...

    def start_queued(self, config: WorkflowConfig) -> DispatchResult: ...

    def terminate_tracked(self, config: WorkflowConfig) -> int: ...


type OrchestratorFactory = Callable[[WorkflowConfig, Store, int], RuntimeOrchestrator]


class OrchestrationRuntime:
    def __init__(
        self,
        *,
        config: WorkflowConfig | None = None,
        store: Store,
        workflow_path: str,
        profile: str | None = None,
        watcher: RuntimeWorkflowWatcher | None = None,
        supervisor: RuntimeSupervisor | None = None,
        orchestrator_factory: OrchestratorFactory | None = None,
        lock_name: str = "orchestration",
        owner_id: str | None = None,
    ):
        self.store = store
        self.workflow_path = workflow_path
        self.profile = profile
        self.watcher = watcher or WorkflowWatcher(store, workflow_path, profile=profile)
        self.supervisor = supervisor or WorkerSupervisor(
            store,
            workflow_path=workflow_path,
            profile=profile,
        )
        self.orchestrator_factory = orchestrator_factory or _default_orchestrator_factory
        self.lock_name = lock_name
        self.owner_id = owner_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._running = False
        self._next_poll_at: str | None = None
        self._last_cycle: RuntimeCycleResult | None = None
        self._current_config: WorkflowConfig | None = config
        self._current_version_id: int | None = None
        self._leader = False
        self._lock_owner = ""

    @property
    def current_config(self) -> WorkflowConfig | None:
        with self._state_lock:
            return self._current_config

    @property
    def last_cycle_result(self) -> RuntimeCycleResult | None:
        with self._state_lock:
            return self._last_cycle

    def start(self) -> None:
        with self._state_lock:
            if self._running:
                return
        if not self._ensure_runtime_lock():
            return
        with self._state_lock:
            self._running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="symphony-runtime", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        config, _version_id = self._runtime_context()
        if config is not None:
            self.supervisor.terminate_tracked(config)
        self.store.release_runtime_lock(self.lock_name, self.owner_id)
        with self._state_lock:
            self._running = False
            self._next_poll_at = None
            self._leader = False

    def run_cycle(self, *, trigger: str = "manual") -> RuntimeCycleResult:
        if not self._ensure_runtime_lock():
            result = RuntimeCycleResult.skipped_cycle(trigger, "runtime lock is held by another process")
            self._set_last_cycle(result)
            return result
        if not self._cycle_lock.acquire(blocking=False):
            result = RuntimeCycleResult.busy(trigger)
            self._set_last_cycle(result)
            return result
        started_at = _utc_now()
        release_after_cycle = not self.is_running()
        try:
            config, version_id, workflow_changed = self.watcher.reload_if_changed()
            self._set_runtime_context(config, version_id)
            orchestrator = self.orchestrator_factory(config, self.store, version_id)
            reconciled = self.supervisor.reconcile(config, version_id)
            synced = orchestrator.poll_once()
            cleanup = self._cleanup_worktrees(orchestrator)
            advanced = orchestrator.advance_ready_workflow_instances(
                allowed_side_effects={"github_read", "github_write", "workspace_write"}
            )
            claimed = orchestrator.claim_available()
            started = self.supervisor.start_queued(config)
            result = RuntimeCycleResult(
                trigger=trigger,
                status="succeeded",
                started_at=started_at,
                completed_at=_utc_now(),
                workflow_changed=workflow_changed,
                synced=synced,
                advanced=advanced,
                claimed=claimed,
                workers_started=started.started,
                workers_crashed=reconciled.crashed,
                workers_timed_out=reconciled.timed_out,
                workers_retried=reconciled.retried,
                cleanup_scanned=cleanup.scanned,
                cleanup_merged=cleanup.merged,
                cleanup_cleaned=cleanup.cleaned,
                cleanup_skipped=cleanup.skipped,
                cleanup_errors=cleanup.errors,
            )
        except Exception as exc:
            result = RuntimeCycleResult.failed(trigger, started_at, str(exc))
            print(f"runtime cycle error: {exc}", file=sys.stderr)
        finally:
            self._cycle_lock.release()
            if release_after_cycle:
                self.store.release_runtime_lock(self.lock_name, self.owner_id)
                self._set_leader(False, "")
        self._set_last_cycle(result)
        return result

    def status(self) -> RuntimeStatus:
        with self._state_lock:
            config = self._current_config
            running = self._running
            next_poll_at = self._next_poll_at
            last_cycle = self._last_cycle
            leader = self._leader
            lock_owner = self._lock_owner
        poll_interval_seconds = config.workers.poll_interval_seconds if config else 0
        profile = config.profile.active if config else self.profile or ""
        summary = self.store.dashboard_summary()
        return RuntimeStatus(
            enabled=True,
            running=running,
            polling_enabled=running,
            cycle_running=self._cycle_lock.locked(),
            profile=profile,
            poll_interval_seconds=poll_interval_seconds,
            next_cycle_at=next_poll_at or "",
            last_cycle=last_cycle,
            queued_attempts=int(summary["queued_attempts"]),
            running_attempts=int(summary["running_attempts"]),
            workers=[_worker_view(row) for row in self.store.running_workers()],
            leader=leader,
            lock_owner=lock_owner,
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            result = self.run_cycle(trigger="timer")
            config = self.current_config
            interval = config.workers.poll_interval_seconds if config else 60
            self._set_next_poll(interval)
            if result.status == "failed":
                interval = max(interval, 5)
            if not self._refresh_runtime_lock():
                with self._state_lock:
                    self._running = False
                    self._leader = False
                break
            self._stop.wait(interval)

    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    def _cleanup_worktrees(self, orchestrator: RuntimeOrchestrator) -> WorktreeCleanupSummary:
        try:
            return orchestrator.cleanup_merged_pull_request_worktrees()
        except Exception as exc:
            print(f"worktree cleanup error: {exc}", file=sys.stderr)
            return WorktreeCleanupSummary(errors=1)

    def _set_last_cycle(self, result: RuntimeCycleResult) -> None:
        with self._state_lock:
            self._last_cycle = result

    def _set_next_poll(self, interval_seconds: int) -> None:
        next_poll = datetime.now(UTC) + timedelta(seconds=interval_seconds)
        with self._state_lock:
            self._next_poll_at = next_poll.isoformat()

    def _set_runtime_context(self, config: WorkflowConfig, version_id: int) -> None:
        with self._state_lock:
            self._current_config = config
            self._current_version_id = version_id

    def _runtime_context(self) -> tuple[WorkflowConfig | None, int | None]:
        with self._state_lock:
            return self._current_config, self._current_version_id

    def _ensure_runtime_lock(self) -> bool:
        config = self.current_config
        ttl_seconds = _lock_ttl_seconds(config)
        acquired = self.store.acquire_runtime_lock(self.lock_name, self.owner_id, ttl_seconds=ttl_seconds)
        if acquired:
            self._set_leader(True, self.owner_id)
            return True
        row = self.store.runtime_lock(self.lock_name)
        self._set_leader(False, "" if row is None else str(row["owner"]))
        return False

    def _refresh_runtime_lock(self) -> bool:
        config = self.current_config
        refreshed = self.store.refresh_runtime_lock(
            self.lock_name,
            self.owner_id,
            ttl_seconds=_lock_ttl_seconds(config),
        )
        if refreshed:
            self._set_leader(True, self.owner_id)
        return refreshed

    def _set_leader(self, leader: bool, lock_owner: str) -> None:
        with self._state_lock:
            self._leader = leader
            self._lock_owner = lock_owner


def disabled_runtime_status(config: WorkflowConfig) -> RuntimeStatus:
    return RuntimeStatus(
        enabled=False,
        running=False,
        polling_enabled=False,
        cycle_running=False,
        profile=config.profile.active,
        poll_interval_seconds=config.workers.poll_interval_seconds,
        next_cycle_at="",
        last_cycle=None,
        queued_attempts=0,
        running_attempts=0,
        workers=[],
        leader=False,
    )


def _default_orchestrator_factory(
    config: WorkflowConfig,
    store: Store,
    version_id: int,
) -> RuntimeOrchestrator:
    return Orchestrator(config, store, version_id)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _lock_ttl_seconds(config: WorkflowConfig | None) -> int:
    if config is None:
        return 60
    return max(config.workers.poll_interval_seconds * 2, 60)


def _worker_view(row: sqlite3.Row) -> RuntimeWorkerView:
    return RuntimeWorkerView(
        worker_id=str(row["worker_id"]),
        attempt_id=int(row["attempt_id"]),
        repo=str(row["repo"]),
        issue_number=int(row["issue_number"]),
        task_type=str(row["task_type"]),
        pid=_int(row["pid"]),
        heartbeat_at=_text(row["heartbeat_at"]),
        deadline_at=_text(row["deadline_at"]),
        started_at=_text(row["worker_started_at"]),
        retry_count=int(row["retry_count"]),
    )


def _int(value: Any) -> int:
    return 0 if value is None else int(value)


def _text(value: object) -> str:
    return "" if value is None else str(value)
