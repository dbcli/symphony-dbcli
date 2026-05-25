from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

from symphony_dbcli.actions import PrimitiveSideEffect
from symphony_dbcli.config import DatabaseConfig, WorkflowConfig, default_config
from symphony_dbcli.orchestrator import WorktreeCleanupSummary
from symphony_dbcli.runtime import OrchestrationRuntime
from symphony_dbcli.store import Store
from symphony_dbcli.supervisor import DispatchResult


def test_runtime_cycle_runs_existing_orchestration_steps_in_order(tmp_path: Path) -> None:
    config, store = _config_and_store(tmp_path)
    calls: list[str] = []
    watcher = FakeWatcher(config, calls)
    supervisor = FakeSupervisor(calls)

    runtime = OrchestrationRuntime(
        config=config,
        store=store,
        workflow_path="WORKFLOW.md",
        watcher=watcher,
        supervisor=supervisor,
        orchestrator_factory=lambda cfg, st, version_id: FakeOrchestrator(calls),
    )

    result = runtime.run_cycle(trigger="manual")

    assert result.status == "succeeded"
    assert result.workflow_changed is True
    assert result.synced == 8
    assert result.advanced == 6
    assert result.claimed == 7
    assert result.workers_started == 4
    assert result.workers_crashed == 1
    assert result.workers_timed_out == 2
    assert result.workers_retried == 3
    assert result.cleanup_scanned == 5
    assert result.cleanup_cleaned == 2
    assert calls == [
        "watcher.reload",
        "supervisor.reconcile",
        "orchestrator.poll_once",
        "orchestrator.cleanup",
        "orchestrator.advance",
        "orchestrator.claim_available",
        "supervisor.start_queued",
    ]


def test_runtime_manual_cycle_does_not_overlap_running_timer_cycle(tmp_path: Path) -> None:
    config, store = _config_and_store(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    results: list[str] = []

    runtime = OrchestrationRuntime(
        config=config,
        store=store,
        workflow_path="WORKFLOW.md",
        watcher=FakeWatcher(config, []),
        supervisor=FakeSupervisor([]),
        orchestrator_factory=lambda cfg, st, version_id: BlockingOrchestrator(entered, release),
    )

    thread = threading.Thread(target=lambda: results.append(runtime.run_cycle(trigger="timer").status))
    thread.start()
    assert entered.wait(timeout=1)

    busy = runtime.run_cycle(trigger="manual")
    release.set()
    thread.join(timeout=1)

    assert busy.status == "busy"
    assert busy.error == "Another orchestration cycle is already running."
    assert results == ["succeeded"]
    assert runtime.last_cycle_result is not None
    assert runtime.last_cycle_result.status == "succeeded"


def test_runtime_stop_terminates_tracked_workers_after_cycle(tmp_path: Path) -> None:
    config, store = _config_and_store(tmp_path)
    supervisor = FakeSupervisor([])
    runtime = OrchestrationRuntime(
        config=config,
        store=store,
        workflow_path="WORKFLOW.md",
        watcher=FakeWatcher(config, []),
        supervisor=supervisor,
        orchestrator_factory=lambda cfg, st, version_id: FakeOrchestrator([]),
    )

    runtime.run_cycle(trigger="manual")
    runtime.stop()

    assert supervisor.terminated_configs == [config]
    assert runtime.status().running is False


def test_runtime_start_stays_standby_when_leader_lock_is_held(tmp_path: Path) -> None:
    config, store = _config_and_store(tmp_path)
    assert store.acquire_runtime_lock("orchestration", "other-process", ttl_seconds=60)
    runtime = OrchestrationRuntime(
        config=config,
        store=store,
        workflow_path="WORKFLOW.md",
        watcher=FakeWatcher(config, []),
        supervisor=FakeSupervisor([]),
        orchestrator_factory=lambda cfg, st, version_id: FakeOrchestrator([]),
        owner_id="this-process",
    )

    runtime.start()
    skipped = runtime.run_cycle(trigger="manual")
    status = runtime.status()

    assert skipped.status == "skipped"
    assert skipped.skipped_reason == "runtime lock is held by another process"
    assert status.running is False
    assert status.leader is False
    assert status.lock_owner == "other-process"


def _config_and_store(tmp_path: Path) -> tuple[WorkflowConfig, Store]:
    config = replace(default_config(), database=DatabaseConfig(path=str(tmp_path / "symphony.db")))
    store = Store(config.database.path)
    store.init()
    return config, store


class FakeWatcher:
    def __init__(self, config: WorkflowConfig, calls: list[str]) -> None:
        self.config = config
        self.calls = calls

    def reload_if_changed(self) -> tuple[WorkflowConfig, int, bool]:
        self.calls.append("watcher.reload")
        return self.config, 42, True


class FakeSupervisor:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.terminated_configs: list[WorkflowConfig] = []

    def reconcile(self, config: WorkflowConfig, workflow_version_id: int | None) -> DispatchResult:
        self.calls.append("supervisor.reconcile")
        return DispatchResult(crashed=1, timed_out=2, retried=3)

    def start_queued(self, config: WorkflowConfig) -> DispatchResult:
        self.calls.append("supervisor.start_queued")
        return DispatchResult(started=4)

    def terminate_tracked(self, config: WorkflowConfig) -> int:
        self.terminated_configs.append(config)
        return 1


class FakeOrchestrator:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def poll_once(self) -> int:
        self.calls.append("orchestrator.poll_once")
        return 8

    def cleanup_merged_pull_request_worktrees(self) -> WorktreeCleanupSummary:
        self.calls.append("orchestrator.cleanup")
        return WorktreeCleanupSummary(scanned=5, merged=1, cleaned=2, skipped=3)

    def advance_ready_workflow_instances(
        self,
        *,
        allowed_side_effects: set[PrimitiveSideEffect] | None = None,
    ) -> int:
        self.calls.append("orchestrator.advance")
        assert allowed_side_effects == {"github_read", "github_write", "workspace_write"}
        return 6

    def claim_available(self) -> int:
        self.calls.append("orchestrator.claim_available")
        return 7


class BlockingOrchestrator(FakeOrchestrator):
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        super().__init__([])
        self.entered = entered
        self.release = release

    def poll_once(self) -> int:
        self.entered.set()
        assert self.release.wait(timeout=2)
        return 1
