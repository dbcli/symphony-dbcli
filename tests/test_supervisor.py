from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from symphony_dbcli.config import default_config, render_workflow
from symphony_dbcli.store import IssueSnapshot, Store
from symphony_dbcli.supervisor import WorkerSupervisor


class FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_supervisor_starts_queued_attempt(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    config = default_config()
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=7,
        task_type="research",
        workflow_version_id=None,
    )
    commands: list[list[str]] = []

    def factory(command: Sequence[str]) -> FakeProcess:
        commands.append(list(command))
        return FakeProcess(9001)

    result = WorkerSupervisor(
        store,
        workflow_path="WORKFLOW.md",
        profile="local",
        process_factory=factory,
    ).start_queued(config)

    attempt = store.attempt_by_id(attempt_id)
    workers = store.running_workers()
    assert result.started == 1
    assert attempt is not None
    assert attempt["status"] == "running"
    assert workers[0]["pid"] == 9001
    assert commands[0][-4:] == ["worker", "run-attempt", "--attempt-id", str(attempt_id)]


def test_supervisor_retries_crashed_worker(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    config = default_config()
    version_id = store.record_workflow_version("WORKFLOW.md", render_workflow(config), config)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=7,
        task_type="research",
        workflow_version_id=version_id,
    )
    process = FakeProcess(9002)

    def factory(command: Sequence[str]) -> FakeProcess:
        return process

    supervisor = WorkerSupervisor(
        store,
        workflow_path="WORKFLOW.md",
        profile=None,
        process_factory=factory,
    )
    supervisor.start_queued(config)
    process.returncode = 1

    result = supervisor.reconcile(config, version_id)

    failed_attempt = store.attempt_by_id(attempt_id)
    queued_attempts = store.queued_attempts()
    assert result.crashed == 1
    assert result.retried == 1
    assert failed_attempt is not None
    assert failed_attempt["status"] == "failed"
    assert failed_attempt["outcome"] == "crashed"
    assert len(queued_attempts) == 1
    assert queued_attempts[0]["parent_attempt_id"] == attempt_id
    assert queued_attempts[0]["retry_count"] == 1


def _seed_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "symphony.db")
    store.init()
    store.upsert_issue(
        IssueSnapshot(
            repo="dbcli/litecli",
            number=7,
            title="Support question",
            url="https://github.com/dbcli/litecli/issues/7",
            state="open",
            labels=["symphony:todo"],
            task_type="research",
        )
    )
    return store
