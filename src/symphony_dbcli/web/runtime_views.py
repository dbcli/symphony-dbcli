from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from symphony_dbcli.config import WorkflowConfig
from symphony_dbcli.runtime import RuntimeCycleResult, RuntimeStatus, RuntimeWorkerView
from symphony_dbcli.store import Store


@dataclass(frozen=True)
class RuntimeConfigView:
    profile: str
    dry_run: bool
    database_path: str
    workspace_strategy: str
    workspace_root: str
    bare_repos_root: str
    branch_prefix: str
    base_branch: str
    retention_days: int

    @classmethod
    def from_config(cls, config: WorkflowConfig) -> RuntimeConfigView:
        return cls(
            profile=config.profile.active,
            dry_run=config.policy.dry_run,
            database_path=config.database.path,
            workspace_strategy=config.workspace.strategy,
            workspace_root=config.workspace.root,
            bare_repos_root=config.workspace.bare_repos_root,
            branch_prefix=config.workspace.branch_prefix,
            base_branch=config.workspace.base_branch or "default branch",
            retention_days=config.workspace.retention_days,
        )


@dataclass(frozen=True)
class WorkerStatusView:
    worker_id: str
    attempt_id: int
    repo: str
    issue_number: int
    task_type: str
    worker_status: str
    attempt_status: str
    pid: int
    hostname: str
    heartbeat_at: str
    deadline_at: str
    started_at: str
    updated_at: str
    retry_count: int


@dataclass(frozen=True)
class QueuedAttemptView:
    attempt_id: int
    repo: str
    issue_number: int
    task_type: str
    status: str
    current_phase: str
    queued_at: str
    updated_at: str
    retry_count: int


@dataclass(frozen=True)
class WorkerEventCounts:
    crashed: int
    timed_out: int
    retried: int


@dataclass(frozen=True)
class WorkersRuntimeStatusView:
    runtime_attached: bool
    leader: bool
    lock_owner: str
    loop_running: bool
    polling_enabled: bool
    cycle_running: bool
    poll_interval_seconds: int
    next_poll_at: str
    last_cycle: RuntimeCycleResult | None
    queued_attempt_count: int
    running_attempt_count: int
    running_workers: list[WorkerStatusView]
    queued_attempts: list[QueuedAttemptView]
    crashed_count: int
    timed_out_count: int
    retried_count: int
    error_count: int
    message: str = ""

    @property
    def runtime_label(self) -> str:
        if not self.runtime_attached:
            return "Not attached"
        return "Leader" if self.leader else "Standby"

    @property
    def loop_label(self) -> str:
        return "Running" if self.loop_running else "Stopped"

    @property
    def polling_label(self) -> str:
        return "Enabled" if self.polling_enabled else "Paused"

    @property
    def cycle_label(self) -> str:
        return "Running" if self.cycle_running else "Idle"

    @classmethod
    def from_runtime(
        cls,
        config: WorkflowConfig,
        store: Store,
        runtime_status: RuntimeStatus | None,
    ) -> WorkersRuntimeStatusView:
        counts = _worker_event_counts(store)
        summary = store.dashboard_summary()
        queued_attempts = [_queued_attempt(row) for row in store.queued_attempts(limit=50)]

        if runtime_status is None:
            return cls(
                runtime_attached=False,
                leader=False,
                lock_owner="",
                loop_running=False,
                polling_enabled=False,
                cycle_running=False,
                poll_interval_seconds=config.workers.poll_interval_seconds,
                next_poll_at="",
                last_cycle=None,
                queued_attempt_count=int(summary["queued_attempts"]),
                running_attempt_count=int(summary["running_attempts"]),
                running_workers=[_worker_status(row) for row in store.running_workers()],
                queued_attempts=queued_attempts,
                crashed_count=counts.crashed,
                timed_out_count=counts.timed_out,
                retried_count=counts.retried,
                error_count=int(summary["error_count"]),
                message="Runtime service is not attached to FastAPI yet.",
            )

        return cls(
            runtime_attached=True,
            leader=runtime_status.leader,
            lock_owner=runtime_status.lock_owner,
            loop_running=runtime_status.running,
            polling_enabled=runtime_status.polling_enabled,
            cycle_running=runtime_status.cycle_running,
            poll_interval_seconds=runtime_status.poll_interval_seconds,
            next_poll_at=runtime_status.next_cycle_at,
            last_cycle=runtime_status.last_cycle,
            queued_attempt_count=runtime_status.queued_attempts,
            running_attempt_count=runtime_status.running_attempts,
            running_workers=[_runtime_worker_status(worker) for worker in runtime_status.workers],
            queued_attempts=queued_attempts,
            crashed_count=counts.crashed,
            timed_out_count=counts.timed_out,
            retried_count=counts.retried,
            error_count=int(summary["error_count"]),
        )


def _runtime_worker_status(worker: RuntimeWorkerView) -> WorkerStatusView:
    return WorkerStatusView(
        worker_id=worker.worker_id,
        attempt_id=worker.attempt_id,
        repo=worker.repo,
        issue_number=worker.issue_number,
        task_type=worker.task_type,
        worker_status="running",
        attempt_status="running",
        pid=worker.pid,
        hostname="",
        heartbeat_at=worker.heartbeat_at,
        deadline_at=worker.deadline_at,
        started_at=worker.started_at,
        updated_at="",
        retry_count=worker.retry_count,
    )


def _worker_status(row: sqlite3.Row) -> WorkerStatusView:
    return WorkerStatusView(
        worker_id=str(row["worker_id"]),
        attempt_id=int(row["attempt_id"]),
        repo=str(row["repo"]),
        issue_number=int(row["issue_number"]),
        task_type=str(row["task_type"]),
        worker_status=str(row["worker_status"]),
        attempt_status=str(row["attempt_status"]),
        pid=_int(row["pid"]),
        hostname=str(row["hostname"]),
        heartbeat_at=_text(row["heartbeat_at"]),
        deadline_at=_text(row["deadline_at"]),
        started_at=_text(row["worker_started_at"]),
        updated_at=_text(row["worker_updated_at"]),
        retry_count=int(row["retry_count"]),
    )


def _queued_attempt(row: sqlite3.Row) -> QueuedAttemptView:
    return QueuedAttemptView(
        attempt_id=int(row["id"]),
        repo=str(row["repo"]),
        issue_number=int(row["issue_number"]),
        task_type=str(row["task_type"]),
        status=str(row["status"]),
        current_phase=_text(row["current_phase"]),
        queued_at=_text(row["queued_at"]),
        updated_at=_text(row["updated_at"]),
        retry_count=int(row["retry_count"]),
    )


def _worker_event_counts(store: Store) -> WorkerEventCounts:
    with store.connect() as conn:
        crashed = conn.execute(
            "SELECT COUNT(*) AS count FROM workers WHERE stop_reason = 'crashed'"
        ).fetchone()["count"]
        timed_out = conn.execute(
            "SELECT COUNT(*) AS count FROM workers WHERE stop_reason = 'timed_out'"
        ).fetchone()["count"]
        retried = conn.execute("SELECT COALESCE(SUM(retry_count), 0) AS count FROM attempts").fetchone()[
            "count"
        ]
    return WorkerEventCounts(crashed=int(crashed), timed_out=int(timed_out), retried=int(retried))


def _int(value: Any) -> int:
    return 0 if value is None else int(value)


def _text(value: object) -> str:
    return "" if value is None else str(value)
