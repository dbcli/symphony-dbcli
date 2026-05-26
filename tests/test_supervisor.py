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

    def factory(command: Sequence[str], log_path: Path) -> FakeProcess:
        commands.append(list(command))
        assert log_path.name.startswith(f"worker-{attempt_id}-")
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

    def factory(command: Sequence[str], log_path: Path) -> FakeProcess:
        log_path.write_text("Traceback (most recent call last):\nRuntimeError: worker exploded\n")
        return process

    supervisor = WorkerSupervisor(
        store,
        workflow_path="WORKFLOW.md",
        profile=None,
        process_factory=factory,
    )
    supervisor.start_queued(config)
    instance_id = store.create_workflow_instance(
        repo="dbcli/litecli",
        issue_number=7,
        task_type="research",
        workflow_version_id=version_id,
        initial_state="setup_complete",
        attempt_id=attempt_id,
    )
    action_run_id = store.start_workflow_action_run(
        instance_id=instance_id,
        workflow_version_id=version_id,
        attempt_id=attempt_id,
        transition_name="research_issue",
        action_name="codex.research_issue",
    )
    process.returncode = 1

    result = supervisor.reconcile(config, version_id)

    retried_attempt = store.attempt_by_id(attempt_id)
    instance = store.workflow_instance_by_id(instance_id)
    queued_attempts = store.queued_attempts()
    with store.connect() as conn:
        action_run = conn.execute(
            "SELECT * FROM workflow_action_runs WHERE id = ?",
            (action_run_id,),
        ).fetchone()
        error = conn.execute("SELECT * FROM worker_errors WHERE attempt_id = ?", (attempt_id,)).fetchone()
    assert result.crashed == 1
    assert result.retried == 1
    assert retried_attempt is not None
    assert retried_attempt["status"] == "queued"
    assert retried_attempt["outcome"] == "retry_queued:crashed"
    assert retried_attempt["retry_count"] == 1
    assert instance is not None
    assert instance["current_state"] == "setup_complete"
    assert instance["status"] == "active"
    assert action_run is not None
    assert action_run["status"] == "failed"
    assert error is not None
    assert "RuntimeError: worker exploded" in error["log_excerpt"]
    assert len(queued_attempts) == 1
    assert queued_attempts[0]["id"] == attempt_id


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
