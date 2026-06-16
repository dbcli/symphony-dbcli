from __future__ import annotations

import difflib
import json
import socket
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from .clock import elapsed_ms, monotonic_ns, utc_after, utc_now
from .config import WorkflowConfig, workflow_hash
from .types import AttemptSummary

SCHEMA_VERSION = 10
FOLLOW_UP_CODE_RELATIONSHIP = "follow_up_code"
ATTEMPT_ADJUSTMENT_RELATIONSHIP = "attempt_adjustment"
START_QUEUED_WORK_AUTOMATICALLY_KEY = "start_queued_work_automatically"
CODEX_PROMPT_EVENT_TYPES = ("turn/start/request", "exec/request")
CODEX_AGENT_MESSAGE_DELTA_EVENT_TYPES = frozenset(
    {
        "agent/message/delta",
        "agent_message/delta",
        "item/agentMessage/delta",
        "item/agent_message/delta",
    }
)

type AttemptLiveEventSource = Literal["codex", "timeline", "error"]


@dataclass(frozen=True)
class IssueSnapshot:
    repo: str
    number: int
    title: str
    url: str
    state: str
    labels: list[str]
    task_type: str
    body: str = ""
    author: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class CodexTokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int = 0
    reasoning_output_tokens: int = 0


@dataclass(frozen=True)
class AttemptLiveEvent:
    source: AttemptLiveEventSource
    id: int
    event_type: str
    created_at: str
    title: str
    message: str
    payload: dict[str, Any]
    output_delta: str = ""


@dataclass(frozen=True)
class AttemptLiveSnapshot:
    attempt_id: int
    status: str
    current_phase: str
    updated_at: str
    events: list[AttemptLiveEvent]


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            for statement in SCHEMA:
                conn.execute(statement)
            _migrate(conn)
            conn.execute(
                """
                INSERT INTO settings(key, value, updated_at)
                VALUES('schema_version', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (str(SCHEMA_VERSION), utc_now()),
            )

    def acquire_runtime_lock(self, name: str, owner: str, *, ttl_seconds: int) -> bool:
        now = utc_now()
        expires_at = utc_after(ttl_seconds)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO runtime_locks(name, owner, acquired_at, heartbeat_at, expires_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    owner = excluded.owner,
                    acquired_at = CASE
                        WHEN runtime_locks.owner = excluded.owner THEN runtime_locks.acquired_at
                        ELSE excluded.acquired_at
                    END,
                    heartbeat_at = excluded.heartbeat_at,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                WHERE runtime_locks.owner = excluded.owner OR runtime_locks.expires_at <= ?
                """,
                (name, owner, now, now, expires_at, now, now),
            )
            return cursor.rowcount == 1

    def refresh_runtime_lock(self, name: str, owner: str, *, ttl_seconds: int) -> bool:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE runtime_locks
                SET heartbeat_at = ?, expires_at = ?, updated_at = ?
                WHERE name = ? AND owner = ?
                """,
                (now, utc_after(ttl_seconds), now, name, owner),
            )
            return cursor.rowcount == 1

    def release_runtime_lock(self, name: str, owner: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM runtime_locks WHERE name = ? AND owner = ?", (name, owner))

    def runtime_lock(self, name: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return cast(
                sqlite3.Row | None,
                conn.execute("SELECT * FROM runtime_locks WHERE name = ?", (name,)).fetchone(),
            )

    def record_workflow_version(
        self,
        path: str | Path,
        content: str,
        config: WorkflowConfig | None,
        *,
        status: str = "accepted",
        error: str | None = None,
    ) -> int:
        config_json = json.dumps(config.to_dict() if config else {}, sort_keys=True)
        content_hash = workflow_hash(content)
        with self.connect() as conn:
            previous = conn.execute(
                """
                SELECT id, content FROM workflow_versions
                WHERE status = 'accepted'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            if status == "accepted":
                existing = conn.execute(
                    "SELECT id FROM workflow_versions WHERE status = 'accepted' AND content_hash = ?",
                    (content_hash,),
                ).fetchone()
                if existing:
                    return int(existing["id"])
            diff = ""
            if previous:
                diff = "\n".join(
                    difflib.unified_diff(
                        str(previous["content"]).splitlines(),
                        content.splitlines(),
                        fromfile=f"workflow:{previous['id']}",
                        tofile="workflow:new",
                        lineterm="",
                    )
                )
            created_at = utc_now()
            cursor = conn.execute(
                """
                INSERT INTO workflow_versions(
                    path, content_hash, content, parsed_config_json, status, error, diff, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(path), content_hash, content, config_json, status, error, diff, created_at),
            )
            version_id = _lastrowid(cursor)
            conn.execute(
                """
                INSERT INTO workflow_reload_events(
                    workflow_version_id, status, error, created_at
                )
                VALUES(?, ?, ?, ?)
                """,
                (version_id, status, error, created_at),
            )
            return version_id

    def latest_workflow_version(self) -> sqlite3.Row | None:
        with self.connect() as conn:
            return cast(
                sqlite3.Row | None,
                conn.execute(
                    """
                    SELECT * FROM workflow_versions
                    WHERE status = 'accepted'
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone(),
            )

    def workflow_history(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT id, path, content_hash, status, error, created_at
                    FROM workflow_versions
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def create_workflow_instance(
        self,
        *,
        repo: str,
        issue_number: int,
        task_type: str,
        workflow_version_id: int | None,
        initial_state: str,
        attempt_id: int | None = None,
        work_item_id: int | None = None,
        work_item_run_id: int | None = None,
    ) -> int:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO workflow_instances(
                    workflow_version_id, repo, issue_number, attempt_id, work_item_id,
                    work_item_run_id, task_type,
                    current_state, status, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    workflow_version_id,
                    repo,
                    issue_number,
                    attempt_id,
                    work_item_id,
                    work_item_run_id,
                    task_type,
                    initial_state,
                    now,
                    now,
                ),
            )
            return _lastrowid(cursor)

    def workflow_instance_by_id(self, instance_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return cast(
                sqlite3.Row | None,
                conn.execute("SELECT * FROM workflow_instances WHERE id = ?", (instance_id,)).fetchone(),
            )

    def active_workflow_instance_for_issue(self, repo: str, issue_number: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return cast(
                sqlite3.Row | None,
                conn.execute(
                    """
                    SELECT *
                    FROM workflow_instances
                    WHERE repo = ? AND issue_number = ? AND status = 'active'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (repo, issue_number),
                ).fetchone(),
            )

    def workflow_instance_for_attempt(self, attempt_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return cast(
                sqlite3.Row | None,
                conn.execute(
                    """
                    SELECT *
                    FROM workflow_instances
                    WHERE attempt_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (attempt_id,),
                ).fetchone(),
            )

    def workflow_action_run_by_id(self, action_run_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return cast(
                sqlite3.Row | None,
                conn.execute(
                    """
                    SELECT *
                    FROM workflow_action_runs
                    WHERE id = ?
                    """,
                    (action_run_id,),
                ).fetchone(),
            )

    def workflow_action_runs_for_attempt(self, attempt_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT *
                    FROM workflow_action_runs
                    WHERE attempt_id = ?
                    ORDER BY id ASC
                    """,
                    (attempt_id,),
                )
            )

    def prepare_workflow_action_retry(
        self,
        *,
        instance_id: int,
        attempt_id: int,
        state: str,
        transition_name: str,
    ) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE workflow_instances
                SET current_state = ?,
                    status = 'active',
                    completed_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (state, now, instance_id),
            )
            conn.execute(
                """
                UPDATE attempts
                SET status = 'running',
                    outcome = ?,
                    completed_at = NULL,
                    duration_ms = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (f"manual_retry:{transition_name}", now, attempt_id),
            )

    def workflow_instance_for_work_item(self, work_item_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return cast(
                sqlite3.Row | None,
                conn.execute(
                    """
                    SELECT *
                    FROM workflow_instances
                    WHERE work_item_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (work_item_id,),
                ).fetchone(),
            )

    def workflow_instances_ready_for_automation(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT i.*
                    FROM workflow_instances i
                    LEFT JOIN attempts a ON a.id = i.attempt_id
                    WHERE i.status = 'active'
                      AND (i.attempt_id IS NULL OR a.status NOT IN ('queued', 'running'))
                    ORDER BY i.updated_at ASC, i.id ASC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def start_workflow_action_run(
        self,
        *,
        instance_id: int,
        workflow_version_id: int | None,
        transition_name: str,
        action_name: str,
        input_data: dict[str, Any] | None = None,
        idempotency_key: str = "",
        attempt_id: int | None = None,
        retry_count: int = 0,
    ) -> int:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO workflow_action_runs(
                    workflow_instance_id, workflow_version_id, attempt_id, transition_name,
                    action_name, status, input_json, output_json, error, idempotency_key,
                    retry_count, started_at, completed_at, started_monotonic_ns,
                    ended_monotonic_ns, duration_ms
                )
                VALUES(?, ?, ?, ?, ?, 'running', ?, '{}', '', ?, ?, ?, NULL, ?, NULL, NULL)
                """,
                (
                    instance_id,
                    workflow_version_id,
                    attempt_id,
                    transition_name,
                    action_name,
                    json.dumps(input_data or {}, sort_keys=True),
                    idempotency_key,
                    retry_count,
                    now,
                    monotonic_ns(),
                ),
            )
            return _lastrowid(cursor)

    def finish_workflow_action_run(
        self,
        action_run_id: int,
        *,
        status: str,
        output_data: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        ended_ns = monotonic_ns()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT started_monotonic_ns FROM workflow_action_runs WHERE id = ?",
                (action_run_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Workflow action run {action_run_id} does not exist.")
            duration = elapsed_ms(int(row["started_monotonic_ns"]), ended_ns)
            conn.execute(
                """
                UPDATE workflow_action_runs
                SET status = ?, output_json = ?, error = ?, completed_at = ?,
                    ended_monotonic_ns = ?, duration_ms = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(output_data or {}, sort_keys=True),
                    error,
                    utc_now(),
                    ended_ns,
                    duration,
                    action_run_id,
                ),
            )

    def record_workflow_artifacts(
        self,
        instance_id: int,
        artifacts: dict[str, Any],
        *,
        workflow_version_id: int | None,
        action_run_id: int | None = None,
    ) -> None:
        if not artifacts:
            return
        now = utc_now()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO workflow_artifacts(
                    workflow_instance_id, workflow_version_id, workflow_action_run_id,
                    name, value_json, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_instance_id, name) DO UPDATE SET
                    workflow_version_id = excluded.workflow_version_id,
                    workflow_action_run_id = excluded.workflow_action_run_id,
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        instance_id,
                        workflow_version_id,
                        action_run_id,
                        name,
                        json.dumps(value, sort_keys=True),
                        now,
                        now,
                    )
                    for name, value in artifacts.items()
                ],
            )

    def workflow_artifact(self, instance_id: int, name: str) -> Any:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT value_json
                FROM workflow_artifacts
                WHERE workflow_instance_id = ? AND name = ?
                """,
                (instance_id, name),
            ).fetchone()
        if not row:
            return None
        return json.loads(str(row["value_json"]))

    def workflow_artifacts(self, instance_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT name, value_json
                FROM workflow_artifacts
                WHERE workflow_instance_id = ?
                ORDER BY name ASC
                """,
                (instance_id,),
            )
            return {str(row["name"]): json.loads(str(row["value_json"])) for row in rows}

    def latest_workflow_action_output(self, instance_id: int, transition_name: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT output_json
                FROM workflow_action_runs
                WHERE workflow_instance_id = ?
                  AND transition_name = ?
                  AND status = 'succeeded'
                ORDER BY id DESC
                LIMIT 1
                """,
                (instance_id, transition_name),
            ).fetchone()
        if not row:
            return {}
        return cast(dict[str, Any], json.loads(str(row["output_json"])))

    def latest_succeeded_workflow_action_run(
        self,
        instance_id: int,
        transition_name: str,
    ) -> sqlite3.Row | None:
        with self.connect() as conn:
            return cast(
                sqlite3.Row | None,
                conn.execute(
                    """
                    SELECT *
                    FROM workflow_action_runs
                    WHERE workflow_instance_id = ?
                      AND transition_name = ?
                      AND status = 'succeeded'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (instance_id, transition_name),
                ).fetchone(),
            )

    def failed_workflow_action_runs(self, instance_id: int, transition_name: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT *
                    FROM workflow_action_runs
                    WHERE workflow_instance_id = ?
                      AND transition_name = ?
                      AND status = 'failed'
                    ORDER BY id ASC
                    """,
                    (instance_id, transition_name),
                )
            )

    def workflow_transition_exists_after(
        self,
        instance_id: int,
        transition_names: Iterable[str],
        completed_at: str,
    ) -> bool:
        names = [name for name in transition_names if name]
        if not names or not completed_at:
            return False
        placeholders = ", ".join("?" for _ in names)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT 1
                FROM workflow_transition_events
                WHERE workflow_instance_id = ?
                  AND transition_name IN ({placeholders})
                  AND created_at >= ?
                LIMIT 1
                """,
                (instance_id, *names, completed_at),
            ).fetchone()
        return row is not None

    def workflow_action_failure_counts(self, instance_id: int) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT transition_name, COUNT(*) AS count
                FROM workflow_action_runs
                WHERE workflow_instance_id = ?
                  AND status = 'failed'
                GROUP BY transition_name
                """,
                (instance_id,),
            )
            return {str(row["transition_name"]): int(row["count"]) for row in rows}

    def fail_running_workflow_action_runs(self, attempt_id: int, *, error: str) -> int:
        ended_ns = monotonic_ns()
        now = utc_now()
        with self.connect() as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT id, started_monotonic_ns
                    FROM workflow_action_runs
                    WHERE attempt_id = ?
                      AND status = 'running'
                    """,
                    (attempt_id,),
                )
            )
            for row in rows:
                duration = elapsed_ms(int(row["started_monotonic_ns"]), ended_ns)
                conn.execute(
                    """
                    UPDATE workflow_action_runs
                    SET status = 'failed', error = ?, completed_at = ?,
                        ended_monotonic_ns = ?, duration_ms = ?
                    WHERE id = ?
                    """,
                    (error, now, ended_ns, duration, int(row["id"])),
                )
            return len(rows)

    def transition_workflow_instance(
        self,
        instance_id: int,
        *,
        workflow_version_id: int | None,
        transition_name: str,
        action_name: str,
        trigger: str,
        from_state: str,
        to_state: str,
        status: str = "active",
        data: dict[str, Any] | None = None,
    ) -> int:
        now = utc_now()
        with self.connect() as conn:
            current = conn.execute(
                "SELECT current_state FROM workflow_instances WHERE id = ?",
                (instance_id,),
            ).fetchone()
            if not current:
                raise ValueError(f"Workflow instance {instance_id} does not exist.")
            if str(current["current_state"]) != from_state:
                raise ValueError(
                    f"Workflow instance {instance_id} is in state '{current['current_state']}', not '{from_state}'."
                )
            completed_at = now if status in {"done", "failed", "blocked"} else None
            conn.execute(
                """
                UPDATE workflow_instances
                SET current_state = ?, status = ?, completed_at = COALESCE(completed_at, ?), updated_at = ?
                WHERE id = ?
                """,
                (to_state, status, completed_at, now, instance_id),
            )
            cursor = conn.execute(
                """
                INSERT INTO workflow_transition_events(
                    workflow_instance_id, workflow_version_id, transition_name, action_name,
                    trigger, from_state, to_state, data_json, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instance_id,
                    workflow_version_id,
                    transition_name,
                    action_name,
                    trigger,
                    from_state,
                    to_state,
                    json.dumps(data or {}, sort_keys=True),
                    now,
                ),
            )
            return _lastrowid(cursor)

    def fail_workflow_instance(
        self,
        instance_id: int,
        *,
        workflow_version_id: int | None,
        message: str,
    ) -> int:
        now = utc_now()
        with self.connect() as conn:
            current = conn.execute(
                "SELECT current_state FROM workflow_instances WHERE id = ?",
                (instance_id,),
            ).fetchone()
            if not current:
                raise ValueError(f"Workflow instance {instance_id} does not exist.")
            from_state = str(current["current_state"])
            conn.execute(
                """
                UPDATE workflow_instances
                SET current_state = 'failed', status = 'failed',
                    completed_at = COALESCE(completed_at, ?), updated_at = ?
                WHERE id = ?
                """,
                (now, now, instance_id),
            )
            cursor = conn.execute(
                """
                INSERT INTO workflow_transition_events(
                    workflow_instance_id, workflow_version_id, transition_name, action_name,
                    trigger, from_state, to_state, data_json, created_at
                )
                VALUES(?, ?, 'runtime_failed', '', 'automatic', ?, 'failed', ?, ?)
                """,
                (
                    instance_id,
                    workflow_version_id,
                    from_state,
                    json.dumps({"message": message}, sort_keys=True),
                    now,
                ),
            )
            return _lastrowid(cursor)

    def open_workflow_gate(
        self,
        *,
        instance_id: int,
        workflow_version_id: int | None,
        gate: str,
        transition_name: str,
        state: str,
        prompt: str = "",
    ) -> int:
        now = utc_now()
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT id
                FROM workflow_gates
                WHERE workflow_instance_id = ?
                  AND transition_name = ?
                  AND status IN ('pending', 'running')
                ORDER BY id DESC
                LIMIT 1
                """,
                (instance_id, transition_name),
            ).fetchone()
            if existing:
                return int(existing["id"])
            cursor = conn.execute(
                """
                INSERT INTO workflow_gates(
                    workflow_instance_id, workflow_version_id, gate, transition_name, state,
                    status, prompt, decision, decided_by, decided_at, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, 'pending', ?, '', '', NULL, ?, ?)
                """,
                (instance_id, workflow_version_id, gate, transition_name, state, prompt, now, now),
            )
            return _lastrowid(cursor)

    def pending_workflow_gates(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT g.*, i.repo, i.issue_number, i.task_type
                    FROM workflow_gates g
                    JOIN workflow_instances i ON i.id = g.workflow_instance_id
                    WHERE g.status = 'pending'
                    ORDER BY g.created_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def workflow_gate_by_id(self, gate_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return cast(
                sqlite3.Row | None,
                conn.execute(
                    """
                    SELECT g.*, i.repo, i.issue_number, i.task_type, i.attempt_id
                    FROM workflow_gates g
                    JOIN workflow_instances i ON i.id = g.workflow_instance_id
                    WHERE g.id = ?
                    """,
                    (gate_id,),
                ).fetchone(),
            )

    def pending_workflow_gate_for_attempt(
        self,
        attempt_id: int,
        transition_name: str,
    ) -> sqlite3.Row | None:
        with self.connect() as conn:
            return cast(
                sqlite3.Row | None,
                conn.execute(
                    """
                    SELECT g.*, i.repo, i.issue_number, i.task_type, i.attempt_id
                    FROM workflow_gates g
                    JOIN workflow_instances i ON i.id = g.workflow_instance_id
                    WHERE i.attempt_id = ?
                      AND g.transition_name = ?
                      AND g.status = 'pending'
                    ORDER BY g.id DESC
                    LIMIT 1
                    """,
                    (attempt_id, transition_name),
                ).fetchone(),
            )

    def running_workflow_gate_for_attempt(
        self,
        attempt_id: int,
        transition_name: str,
    ) -> sqlite3.Row | None:
        with self.connect() as conn:
            return cast(
                sqlite3.Row | None,
                conn.execute(
                    """
                    SELECT g.*, i.repo, i.issue_number, i.task_type, i.attempt_id
                    FROM workflow_gates g
                    JOIN workflow_instances i ON i.id = g.workflow_instance_id
                    WHERE i.attempt_id = ?
                      AND g.transition_name = ?
                      AND g.status = 'running'
                    ORDER BY g.id DESC
                    LIMIT 1
                    """,
                    (attempt_id, transition_name),
                ).fetchone(),
            )

    def pending_workflow_gates_for_attempt(self, attempt_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT g.*, i.repo, i.issue_number, i.task_type, i.attempt_id
                    FROM workflow_gates g
                    JOIN workflow_instances i ON i.id = g.workflow_instance_id
                    WHERE i.attempt_id = ?
                      AND g.status = 'pending'
                    ORDER BY g.created_at ASC, g.id ASC
                    """,
                    (attempt_id,),
                )
            )

    def start_workflow_gate(self, gate_id: int, *, decided_by: str) -> bool:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE workflow_gates
                SET status = 'running',
                    decision = 'approved',
                    decided_by = ?,
                    decided_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (decided_by, now, now, gate_id),
            )
            return cursor.rowcount == 1

    def reopen_workflow_gate(self, gate_id: int) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE workflow_gates
                SET status = 'pending',
                    decision = '',
                    decided_by = '',
                    decided_at = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (now, gate_id),
            )

    def workflow_state_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT current_state, COUNT(*) AS count
                FROM workflow_instances
                WHERE status = 'active'
                GROUP BY current_state
                """
            )
            return {str(row["current_state"]): int(row["count"]) for row in rows}

    def resolve_workflow_gate(self, gate_id: int, *, decision: str, decided_by: str) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE workflow_gates
                SET status = 'resolved', decision = ?, decided_by = ?, decided_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (decision, decided_by, now, now, gate_id),
            )

    def start_queued_work_automatically(self) -> bool:
        return True

    def set_start_queued_work_automatically(self, enabled: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO settings(key, value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (START_QUEUED_WORK_AUTOMATICALLY_KEY, "true", utc_now()),
            )

    def upsert_repo(self, full_name: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO repos(full_name, created_at, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(full_name) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (full_name, utc_now(), utc_now()),
            )

    def upsert_issue(self, issue: IssueSnapshot) -> None:
        now = utc_now()
        with self.connect() as conn:
            self._upsert_repo_conn(conn, issue.repo)
            conn.execute(
                """
                INSERT INTO issues(
                    repo, number, title, url, state, task_type, body, author,
                    labels_json, github_updated_at, first_seen_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo, number) DO UPDATE SET
                    title = excluded.title,
                    url = excluded.url,
                    state = excluded.state,
                    task_type = excluded.task_type,
                    body = excluded.body,
                    author = excluded.author,
                    labels_json = excluded.labels_json,
                    github_updated_at = excluded.github_updated_at,
                    updated_at = excluded.updated_at
                """,
                (
                    issue.repo,
                    issue.number,
                    issue.title,
                    issue.url,
                    issue.state,
                    issue.task_type,
                    issue.body,
                    issue.author,
                    json.dumps(issue.labels, sort_keys=True),
                    issue.updated_at,
                    now,
                    now,
                ),
            )
            conn.execute(
                "DELETE FROM issue_labels WHERE repo = ? AND issue_number = ?", (issue.repo, issue.number)
            )
            conn.executemany(
                "INSERT INTO issue_labels(repo, issue_number, label) VALUES(?, ?, ?)",
                [(issue.repo, issue.number, label) for label in issue.labels],
            )

    def list_issues(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT repo, number, title, state, task_type, labels_json, url, updated_at
                    FROM issues
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def eligible_issues(self, todo_label: str, blocked_label: str, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT i.*
                    FROM issues i
                    WHERE i.state = 'open'
                      AND EXISTS (
                          SELECT 1 FROM issue_labels l
                          WHERE l.repo = i.repo AND l.issue_number = i.number AND l.label = ?
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM issue_labels l
                          WHERE l.repo = i.repo AND l.issue_number = i.number AND l.label = ?
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM attempts a
                          WHERE a.repo = i.repo
                            AND a.issue_number = i.number
                            AND a.status IN ('queued', 'running', 'review')
                      )
                    ORDER BY i.first_seen_at ASC, i.repo ASC, i.number ASC
                    LIMIT ?
                    """,
                    (todo_label, blocked_label, limit),
                )
            )

    def create_attempt(
        self,
        *,
        repo: str,
        issue_number: int,
        task_type: str,
        workflow_version_id: int | None,
        worktree_path: str = "",
        base_repo_path: str = "",
        branch: str = "",
        status: str = "queued",
        parent_attempt_id: int | None = None,
        retry_count: int = 0,
        work_item_id: int | None = None,
        work_item_run_id: int | None = None,
    ) -> int:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO attempts(
                    repo, issue_number, task_type, workflow_version_id, work_item_id,
                    work_item_run_id, status,
                    base_repo_path, worktree_path, branch, parent_attempt_id, retry_count,
                    queued_at, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo,
                    issue_number,
                    task_type,
                    workflow_version_id,
                    work_item_id,
                    work_item_run_id,
                    status,
                    base_repo_path,
                    worktree_path,
                    branch,
                    parent_attempt_id,
                    retry_count,
                    now,
                    now,
                    now,
                ),
            )
            return _lastrowid(cursor)

    def update_attempt_workspace(
        self,
        attempt_id: int,
        *,
        base_repo_path: str,
        worktree_path: str,
        branch: str,
        commit_sha: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE attempts
                SET base_repo_path = ?, worktree_path = ?, branch = ?, commit_sha = ?, updated_at = ?
                WHERE id = ?
                """,
                (base_repo_path, worktree_path, branch, commit_sha, utc_now(), attempt_id),
            )

    def start_attempt(
        self,
        attempt_id: int,
        worker_id: str,
        *,
        pid: int | None = None,
        max_runtime_seconds: int | None = None,
    ) -> None:
        now = utc_now()
        deadline_at = utc_after(max_runtime_seconds) if max_runtime_seconds else None
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE attempts
                SET status = 'running',
                    worker_id = ?,
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (worker_id, now, now, attempt_id),
            )
            conn.execute(
                """
                INSERT INTO workers(
                    id, attempt_id, status, hostname, pid, heartbeat_at, deadline_at, started_at, updated_at
                )
                VALUES(?, ?, 'running', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    attempt_id = excluded.attempt_id,
                    status = excluded.status,
                    hostname = excluded.hostname,
                    pid = COALESCE(excluded.pid, workers.pid),
                    heartbeat_at = excluded.heartbeat_at,
                    deadline_at = COALESCE(excluded.deadline_at, workers.deadline_at),
                    updated_at = excluded.updated_at
                """,
                (worker_id, attempt_id, socket.gethostname(), pid, now, deadline_at, now, now),
            )

    def update_worker_pid(self, worker_id: str, pid: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE workers
                SET pid = ?, updated_at = ?
                WHERE id = ?
                """,
                (pid, utc_now(), worker_id),
            )

    def heartbeat_worker(self, worker_id: str) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE workers
                SET heartbeat_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (now, now, worker_id),
            )

    def finish_attempt(self, attempt_id: int, status: str, outcome: str = "") -> None:
        now = utc_now()
        with self.connect() as conn:
            started = conn.execute("SELECT started_at FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
            duration_ms = None
            if started and started["started_at"]:
                duration_ms = self._duration_from_timeline(conn, attempt_id)
            conn.execute(
                """
                UPDATE attempts
                SET status = ?, outcome = ?, completed_at = ?, duration_ms = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, outcome, now, duration_ms, now, attempt_id),
            )
            conn.execute(
                """
                UPDATE workers
                SET status = ?, completed_at = ?, updated_at = ?
                WHERE attempt_id = ? AND status = 'running'
                """,
                (status, now, now, attempt_id),
            )

    def record_timeline_event(
        self,
        attempt_id: int,
        *,
        phase: str,
        event_type: str,
        message: str = "",
        data: dict[str, Any] | None = None,
        started_monotonic_ns: int | None = None,
        ended_monotonic_ns: int | None = None,
    ) -> int:
        now = utc_now()
        start_ns = started_monotonic_ns or monotonic_ns()
        end_ns = ended_monotonic_ns
        duration = elapsed_ms(start_ns, end_ns) if end_ns is not None else None
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO worker_timeline_events(
                    attempt_id, phase, event_type, message, data_json,
                    started_at, ended_at, started_monotonic_ns, ended_monotonic_ns, duration_ms
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    phase,
                    event_type,
                    message,
                    json.dumps(data or {}, sort_keys=True),
                    now,
                    now if end_ns is not None else None,
                    start_ns,
                    end_ns,
                    duration,
                ),
            )
            conn.execute(
                "UPDATE attempts SET current_phase = ?, updated_at = ? WHERE id = ?",
                (phase, now, attempt_id),
            )
            return _lastrowid(cursor)

    def record_codex_event(
        self,
        attempt_id: int,
        *,
        thread_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO codex_events(attempt_id, thread_id, event_type, payload_json, created_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (attempt_id, thread_id, event_type, json.dumps(payload, sort_keys=True), utc_now()),
            )
            return _lastrowid(cursor)

    def record_codex_turn(
        self,
        attempt_id: int,
        *,
        thread_id: str,
        turn_index: int,
        status: str,
        model: str = "",
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        tool_call_count: int = 0,
        started_monotonic_ns: int | None = None,
        ended_monotonic_ns: int | None = None,
    ) -> int:
        now = utc_now()
        start_ns = started_monotonic_ns or monotonic_ns()
        end_ns = ended_monotonic_ns or monotonic_ns()
        duration = elapsed_ms(start_ns, end_ns)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO codex_turns(
                    attempt_id, thread_id, turn_index, status, model,
                    input_tokens, output_tokens, tool_call_count,
                    started_at, ended_at, started_monotonic_ns, ended_monotonic_ns, duration_ms
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    thread_id,
                    turn_index,
                    status,
                    model,
                    input_tokens,
                    output_tokens,
                    tool_call_count,
                    now,
                    now,
                    start_ns,
                    end_ns,
                    duration,
                ),
            )
            self._refresh_attempt_metrics(conn, attempt_id)
            return _lastrowid(cursor)

    def record_error(
        self,
        attempt_id: int,
        *,
        phase: str,
        error_type: str,
        message: str,
        recoverable: bool = False,
        turn_id: int | None = None,
        log_excerpt: str = "",
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO worker_errors(
                    attempt_id, turn_id, phase, error_type, message, recoverable, log_excerpt, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (attempt_id, turn_id, phase, error_type, message, int(recoverable), log_excerpt, utc_now()),
            )
            self._refresh_attempt_metrics(conn, attempt_id)
            return _lastrowid(cursor)

    def record_worker_log(self, attempt_id: int, level: str, message: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO worker_logs(attempt_id, level, message, created_at) VALUES(?, ?, ?, ?)",
                (attempt_id, level, message, utc_now()),
            )

    def record_worker_result(
        self,
        *,
        attempt_id: int,
        repo: str,
        issue_number: int,
        result_type: str,
        title: str,
        body: str,
        status: str = "ready_for_review",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO worker_results(
                    attempt_id, repo, issue_number, result_type, title, body,
                    status, metadata_json, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                    repo = excluded.repo,
                    issue_number = excluded.issue_number,
                    result_type = excluded.result_type,
                    title = excluded.title,
                    body = excluded.body,
                    status = excluded.status,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    attempt_id,
                    repo,
                    issue_number,
                    result_type,
                    title,
                    body,
                    status,
                    json.dumps(metadata or {}, sort_keys=True),
                    now,
                    now,
                ),
            )

    def record_pr(
        self,
        attempt_id: int,
        repo: str,
        number: int,
        url: str,
        title: str,
        *,
        state: str = "",
        merged_at: str = "",
    ) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pull_requests(
                    attempt_id, repo, number, url, title, state, merged_at, cleanup_error, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, '', ?)
                ON CONFLICT(repo, number) DO UPDATE SET
                    attempt_id = excluded.attempt_id,
                    url = excluded.url,
                    title = excluded.title,
                    state = excluded.state,
                    merged_at = excluded.merged_at,
                    cleanup_error = ''
                """,
                (attempt_id, repo, number, url, title, state, merged_at, now),
            )
            if merged_at:
                row = conn.execute(
                    "SELECT id FROM pull_requests WHERE repo = ? AND number = ?",
                    (repo, number),
                ).fetchone()
                if row is not None:
                    self._mark_work_item_done_for_merged_pull_request(conn, int(row["id"]), now)

    def record_issue_pull_request_link(
        self,
        *,
        repo: str,
        issue_number: int,
        pull_request_number: int,
        pull_request_url: str,
        pull_request_title: str,
        state: str,
        link_source: str,
        marker: str,
    ) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO issue_pull_requests(
                    repo, issue_number, pull_request_number, pull_request_url,
                    pull_request_title, state, link_source, marker, verified_at,
                    created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo, issue_number, pull_request_number) DO UPDATE SET
                    pull_request_url = excluded.pull_request_url,
                    pull_request_title = excluded.pull_request_title,
                    state = excluded.state,
                    link_source = excluded.link_source,
                    marker = excluded.marker,
                    verified_at = excluded.verified_at,
                    updated_at = excluded.updated_at
                """,
                (
                    repo,
                    issue_number,
                    pull_request_number,
                    pull_request_url,
                    pull_request_title,
                    state,
                    link_source,
                    marker,
                    now,
                    now,
                    now,
                ),
            )

    def issue_pull_request_links(self, repo: str, issue_number: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT *
                    FROM issue_pull_requests
                    WHERE repo = ? AND issue_number = ?
                    ORDER BY verified_at DESC, id DESC
                    """,
                    (repo, issue_number),
                )
            )

    def pull_requests_for_attempt(self, attempt_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM pull_requests WHERE attempt_id = ? ORDER BY id DESC",
                    (attempt_id,),
                )
            )

    def pending_pull_request_cleanups(self, *, retry_errors: bool = False) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                        pr.*,
                        a.worktree_path,
                        a.base_repo_path,
                        a.branch
                    FROM pull_requests pr
                    JOIN attempts a ON a.id = pr.attempt_id
                    WHERE pr.worktree_cleaned_at IS NULL
                      AND a.worktree_path != ''
                      AND a.base_repo_path != ''
                      AND (? OR pr.cleanup_error = '')
                    ORDER BY pr.created_at ASC, pr.id ASC
                    """,
                    (1 if retry_errors else 0,),
                )
            )

    def update_pull_request_status(
        self,
        pull_request_id: int,
        *,
        state: str,
        merged_at: str,
    ) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE pull_requests
                SET state = ?, merged_at = ?
                WHERE id = ?
                """,
                (state, merged_at, pull_request_id),
            )
            if merged_at:
                self._mark_work_item_done_for_merged_pull_request(conn, pull_request_id, now)

    def _mark_work_item_done_for_merged_pull_request(
        self,
        conn: sqlite3.Connection,
        pull_request_id: int,
        now: str,
    ) -> None:
        if not _sqlite_table_exists(conn, "work_items"):
            return
        row = conn.execute(
            """
            SELECT a.work_item_id
            FROM pull_requests pr
            JOIN attempts a ON a.id = pr.attempt_id
            WHERE pr.id = ?
            """,
            (pull_request_id,),
        ).fetchone()
        if row is None or row["work_item_id"] is None:
            return
        work_item = conn.execute(
            "SELECT id, state, disposition FROM work_items WHERE id = ?",
            (row["work_item_id"],),
        ).fetchone()
        if work_item is None or work_item["state"] == "done" or work_item["disposition"] != "active":
            return
        conn.execute(
            """
            UPDATE work_items
            SET state = 'done', outcome = ?, updated_at = ?
            WHERE id = ?
            """,
            ("pull_request_merged", now, work_item["id"]),
        )
        if not _sqlite_table_exists(conn, "work_item_state_events"):
            return
        conn.execute(
            """
            INSERT INTO work_item_state_events(
                work_item_id, from_state, to_state, reasons_json, note, created_at
            )
            VALUES(?, ?, 'done', '[]', ?, ?)
            """,
            (work_item["id"], work_item["state"], "pull_request_merged", now),
        )

    def mark_pull_request_worktree_cleaned(self, pull_request_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE pull_requests
                SET worktree_cleaned_at = ?, cleanup_error = ''
                WHERE id = ?
                """,
                (utc_now(), pull_request_id),
            )

    def mark_pull_request_worktree_cleanup_failed(self, pull_request_id: int, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE pull_requests
                SET cleanup_error = ?
                WHERE id = ?
                """,
                (error, pull_request_id),
            )

    def record_comment(
        self,
        attempt_id: int | None,
        repo: str,
        issue_number: int,
        url: str,
        body: str,
        status: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO comments(attempt_id, repo, issue_number, url, body, status, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (attempt_id, repo, issue_number, url, body, status, utc_now()),
            )

    def comment_by_id(self, comment_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return cast(
                sqlite3.Row | None,
                conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone(),
            )

    def mark_comment_posted(self, comment_id: int, *, body: str, url: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE comments
                SET body = ?, url = ?, status = 'posted'
                WHERE id = ?
                """,
                (body, url, comment_id),
            )

    def update_attempt_outcome(self, attempt_id: int, outcome: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE attempts SET outcome = ?, updated_at = ? WHERE id = ?",
                (outcome, utc_now(), attempt_id),
            )

    def attempt_by_id(self, attempt_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return cast(
                sqlite3.Row | None,
                conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone(),
            )

    def active_attempt_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT repo, COUNT(*) AS count
                FROM attempts
                WHERE status IN ('queued', 'running')
                GROUP BY repo
                """
            )
            counts = {str(row["repo"]): int(row["count"]) for row in rows}
            counts["*"] = sum(counts.values())
            return counts

    def running_attempt_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT repo, COUNT(*) AS count
                FROM attempts
                WHERE status = 'running'
                GROUP BY repo
                """
            )
            counts = {str(row["repo"]): int(row["count"]) for row in rows}
            counts["*"] = sum(counts.values())
            return counts

    def queued_attempts(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT *
                    FROM attempts
                    WHERE status = 'queued'
                    ORDER BY queued_at ASC, id ASC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def running_workers(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                        w.id AS worker_id,
                        w.attempt_id,
                        w.status AS worker_status,
                        w.hostname,
                        w.pid,
                        w.heartbeat_at,
                        w.deadline_at,
                        w.started_at AS worker_started_at,
                        w.updated_at AS worker_updated_at,
                        a.repo,
                        a.issue_number,
                        a.task_type,
                        a.retry_count,
                        a.status AS attempt_status
                    FROM workers w
                    JOIN attempts a ON a.id = w.attempt_id
                    WHERE w.status = 'running' AND a.status = 'running'
                    ORDER BY w.started_at ASC
                    """
                )
            )

    def requeue_attempt_for_retry(self, attempt_id: int, *, reason: str) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE attempts
                SET status = 'queued',
                    worker_id = '',
                    outcome = ?,
                    retry_count = retry_count + 1,
                    queued_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (f"retry_queued:{reason}", now, now, attempt_id),
            )

    def create_code_follow_up_attempt(self, source_attempt_id: int, workflow_version_id: int | None) -> int:
        source = self.attempt_by_id(source_attempt_id)
        if not source:
            raise ValueError(f"Attempt {source_attempt_id} does not exist.")
        if str(source["task_type"]) != "research":
            raise ValueError("Only research attempts can create code follow-up attempts.")
        result = self.worker_result_for_attempt(source_attempt_id)
        if not result or not str(result["body"]).strip():
            raise ValueError("Research attempt does not have a worker result to feed into a code task.")
        existing = self.code_follow_up_attempt(source_attempt_id)
        if existing:
            return int(existing["id"])
        target_attempt_id = self.create_attempt(
            repo=str(source["repo"]),
            issue_number=int(source["issue_number"]),
            task_type="code",
            workflow_version_id=workflow_version_id,
            status="queued",
        )
        self.record_attempt_link(
            source_attempt_id=source_attempt_id,
            target_attempt_id=target_attempt_id,
            relationship=FOLLOW_UP_CODE_RELATIONSHIP,
            metadata={"source_result_id": int(result["id"])},
        )
        self.record_timeline_event(
            target_attempt_id,
            phase="queue",
            event_type="created_from_research",
            message=f"attempt {source_attempt_id}",
            data={"source_attempt_id": source_attempt_id},
        )
        return target_attempt_id

    def record_attempt_link(
        self,
        *,
        source_attempt_id: int,
        target_attempt_id: int,
        relationship: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO attempt_links(
                    source_attempt_id, target_attempt_id, relationship, metadata_json, created_at
                )
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(source_attempt_id, target_attempt_id, relationship) DO UPDATE SET
                    metadata_json = excluded.metadata_json
                """,
                (
                    source_attempt_id,
                    target_attempt_id,
                    relationship,
                    json.dumps(metadata or {}, sort_keys=True),
                    utc_now(),
                ),
            )

    def worker_result_for_attempt(self, attempt_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return cast(
                sqlite3.Row | None,
                conn.execute("SELECT * FROM worker_results WHERE attempt_id = ?", (attempt_id,)).fetchone(),
            )

    def code_follow_up_attempt(self, source_attempt_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return cast(
                sqlite3.Row | None,
                conn.execute(
                    """
                    SELECT target.*
                    FROM attempt_links link
                    JOIN attempts target ON target.id = link.target_attempt_id
                    WHERE link.source_attempt_id = ?
                      AND link.relationship = ?
                      AND target.status IN ('queued', 'running', 'review', 'done')
                    ORDER BY target.id DESC
                    LIMIT 1
                    """,
                    (source_attempt_id, FOLLOW_UP_CODE_RELATIONSHIP),
                ).fetchone(),
            )

    def follow_up_source_result(self, target_attempt_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return cast(
                sqlite3.Row | None,
                conn.execute(
                    """
                    SELECT
                        link.source_attempt_id,
                        link.relationship,
                        source.repo,
                        source.issue_number,
                        source.task_type,
                        source.status AS source_status,
                        result.id AS result_id,
                        result.result_type,
                        result.title,
                        result.body,
                        result.status AS result_status,
                        result.updated_at AS result_updated_at
                    FROM attempt_links link
                    JOIN attempts source ON source.id = link.source_attempt_id
                    JOIN worker_results result ON result.attempt_id = source.id
                    WHERE link.target_attempt_id = ?
                      AND link.relationship IN (?, ?)
                    ORDER BY link.id DESC
                    LIMIT 1
                    """,
                    (
                        target_attempt_id,
                        FOLLOW_UP_CODE_RELATIONSHIP,
                        ATTEMPT_ADJUSTMENT_RELATIONSHIP,
                    ),
                ).fetchone(),
            )

    def attempt_follow_up_targets(self, source_attempt_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                        link.relationship,
                        link.created_at AS linked_at,
                        target.*
                    FROM attempt_links link
                    JOIN attempts target ON target.id = link.target_attempt_id
                    WHERE link.source_attempt_id = ?
                    ORDER BY link.id DESC
                    """,
                    (source_attempt_id,),
                )
            )

    def mark_worker_exited(self, worker_id: str, exit_code: int | None, stop_reason: str = "") -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE workers
                SET status = 'failed',
                    exit_code = ?,
                    stop_reason = ?,
                    completed_at = COALESCE(completed_at, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (exit_code, stop_reason, now, now, worker_id),
            )

    def dashboard_summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            issue_count = conn.execute("SELECT COUNT(*) AS count FROM issues").fetchone()["count"]
            running = conn.execute(
                "SELECT COUNT(*) AS count FROM attempts WHERE status = 'running'"
            ).fetchone()["count"]
            queued = conn.execute(
                "SELECT COUNT(*) AS count FROM attempts WHERE status = 'queued'"
            ).fetchone()["count"]
            errors = conn.execute("SELECT COUNT(*) AS count FROM worker_errors").fetchone()["count"]
            turns = conn.execute("SELECT COUNT(*) AS count FROM codex_turns").fetchone()["count"]
            attempts = list(
                conn.execute(
                    """
                    SELECT id, repo, issue_number, task_type, status, current_phase,
                           turn_count, error_count, duration_ms, worktree_path, updated_at
                    FROM attempts
                    ORDER BY updated_at DESC
                    LIMIT 20
                    """
                )
            )
            latest_workflow = conn.execute(
                """
                SELECT id, content_hash, status, error, created_at
                FROM workflow_versions
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            accepted_workflow = conn.execute(
                """
                SELECT id, content_hash, status, error, created_at
                FROM workflow_versions
                WHERE status = 'accepted'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            rejected_workflow = conn.execute(
                """
                SELECT id, content_hash, status, error, created_at
                FROM workflow_versions
                WHERE status = 'rejected'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            return {
                "issue_count": issue_count,
                "running_attempts": running,
                "queued_attempts": queued,
                "error_count": errors,
                "turn_count": turns,
                "attempts": attempts,
                "workflow": {
                    "latest": latest_workflow,
                    "accepted": accepted_workflow,
                    "rejected": rejected_workflow,
                    "has_current_error": bool(latest_workflow and latest_workflow["status"] == "rejected"),
                },
            }

    def attempt_detail(self, attempt_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            attempt = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
            if not attempt:
                return None
            work_item = _attempt_work_item(conn, attempt)
            return {
                "attempt": attempt,
                "work_item": work_item,
                "source_item": _attempt_source_item(conn, work_item),
                "result": conn.execute(
                    "SELECT * FROM worker_results WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone(),
                "pull_requests": self.pull_requests_for_attempt(attempt_id),
                "source_result": self.follow_up_source_result(attempt_id),
                "follow_up_targets": self.attempt_follow_up_targets(attempt_id),
                "code_follow_up": self.code_follow_up_attempt(attempt_id),
                "workflow_action_runs": self.workflow_action_runs_for_attempt(attempt_id),
                "timeline": list(
                    conn.execute(
                        "SELECT * FROM worker_timeline_events WHERE attempt_id = ? ORDER BY id ASC",
                        (attempt_id,),
                    )
                ),
                "turns": list(
                    conn.execute(
                        "SELECT * FROM codex_turns WHERE attempt_id = ? ORDER BY turn_index ASC",
                        (attempt_id,),
                    )
                ),
                "token_usage": _codex_token_usage_for_attempt(conn, attempt_id),
                "prompts": _codex_prompts_for_attempt(conn, attempt_id),
                "errors": list(
                    conn.execute(
                        "SELECT * FROM worker_errors WHERE attempt_id = ? ORDER BY id ASC", (attempt_id,)
                    )
                ),
                "logs": list(
                    conn.execute(
                        "SELECT * FROM worker_logs WHERE attempt_id = ? ORDER BY id DESC LIMIT 50",
                        (attempt_id,),
                    )
                ),
                "comments": list(
                    conn.execute(
                        "SELECT * FROM comments WHERE attempt_id = ? ORDER BY id DESC",
                        (attempt_id,),
                    )
                ),
            }

    def attempt_live_events(
        self,
        attempt_id: int,
        *,
        after_codex_id: int = 0,
        after_timeline_id: int = 0,
        after_error_id: int = 0,
        limit: int = 100,
    ) -> AttemptLiveSnapshot | None:
        with self.connect() as conn:
            attempt = conn.execute(
                "SELECT id, status, current_phase, updated_at FROM attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            if not attempt:
                return None
            bounded_limit = max(1, min(limit, 500))
            events: list[AttemptLiveEvent] = []
            events.extend(_live_timeline_events(conn, attempt_id, after_timeline_id, bounded_limit))
            events.extend(_live_codex_events(conn, attempt_id, after_codex_id, bounded_limit))
            events.extend(_live_error_events(conn, attempt_id, after_error_id, bounded_limit))
            events.sort(key=lambda event: (event.created_at, event.source, event.id))
            return AttemptLiveSnapshot(
                attempt_id=int(attempt["id"]),
                status=str(attempt["status"]),
                current_phase=str(attempt["current_phase"] or ""),
                updated_at=str(attempt["updated_at"]),
                events=events[:bounded_limit],
            )

    def issue_detail(self, repo: str, number: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            issue = conn.execute(
                "SELECT * FROM issues WHERE repo = ? AND number = ?", (repo, number)
            ).fetchone()
            if not issue:
                return None
            attempts = list(
                conn.execute(
                    "SELECT * FROM attempts WHERE repo = ? AND issue_number = ? ORDER BY id DESC",
                    (repo, number),
                )
            )
            return {
                "issue": issue,
                "attempts": attempts,
                "comments": list(
                    conn.execute(
                        "SELECT * FROM comments WHERE repo = ? AND issue_number = ? ORDER BY id DESC",
                        (repo, number),
                    )
                ),
                "labels": list(
                    conn.execute(
                        "SELECT label FROM issue_labels WHERE repo = ? AND issue_number = ? ORDER BY label",
                        (repo, number),
                    )
                ),
            }

    def answerable_attempts(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT id, repo, issue_number, task_type, status, current_phase,
                           duration_ms, codex_duration_ms, turn_count, error_count,
                           workflow_version_id, updated_at
                    FROM attempts
                    ORDER BY updated_at DESC
                    LIMIT 100
                    """
                )
            )

    def attempt_summaries(self, limit: int = 100) -> list[AttemptSummary]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, repo, issue_number, task_type, status, current_phase,
                       duration_ms, codex_duration_ms, turn_count, error_count,
                       workflow_version_id, updated_at
                FROM attempts
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [
                AttemptSummary(
                    id=int(row["id"]),
                    repo=str(row["repo"]),
                    issue_number=int(row["issue_number"]),
                    task_type=str(row["task_type"]),
                    status=str(row["status"]),
                    current_phase=str(row["current_phase"] or ""),
                    duration_ms=int(row["duration_ms"]) if row["duration_ms"] is not None else None,
                    codex_duration_ms=int(row["codex_duration_ms"] or 0),
                    turn_count=int(row["turn_count"] or 0),
                    error_count=int(row["error_count"] or 0),
                    workflow_version_id=int(row["workflow_version_id"])
                    if row["workflow_version_id"] is not None
                    else None,
                    updated_at=str(row["updated_at"]),
                )
                for row in rows
            ]

    def _upsert_repo_conn(self, conn: sqlite3.Connection, full_name: str) -> None:
        now = utc_now()
        conn.execute(
            """
            INSERT INTO repos(full_name, created_at, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(full_name) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (full_name, now, now),
        )

    def _refresh_attempt_metrics(self, conn: sqlite3.Connection, attempt_id: int) -> None:
        turn_count = conn.execute(
            "SELECT COUNT(*) AS count FROM codex_turns WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()["count"]
        error_count = conn.execute(
            "SELECT COUNT(*) AS count FROM worker_errors WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()["count"]
        codex_duration = conn.execute(
            "SELECT COALESCE(SUM(duration_ms), 0) AS duration FROM codex_turns WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()["duration"]
        conn.execute(
            """
            UPDATE attempts
            SET turn_count = ?, error_count = ?, codex_duration_ms = ?, updated_at = ?
            WHERE id = ?
            """,
            (turn_count, error_count, codex_duration, utc_now(), attempt_id),
        )

    def _duration_from_timeline(self, conn: sqlite3.Connection, attempt_id: int) -> int | None:
        row = conn.execute(
            """
            SELECT MIN(started_monotonic_ns) AS started, MAX(ended_monotonic_ns) AS ended
            FROM worker_timeline_events
            WHERE attempt_id = ? AND ended_monotonic_ns IS NOT NULL
            """,
            (attempt_id,),
        ).fetchone()
        if not row or row["started"] is None or row["ended"] is None:
            return None
        return elapsed_ms(int(row["started"]), int(row["ended"]))


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _attempt_work_item(conn: sqlite3.Connection, attempt: sqlite3.Row) -> sqlite3.Row | None:
    work_item_id = attempt["work_item_id"]
    if work_item_id is None or not _sqlite_table_exists(conn, "work_items"):
        return None
    return cast(
        sqlite3.Row | None, conn.execute("SELECT * FROM work_items WHERE id = ?", (work_item_id,)).fetchone()
    )


def _attempt_source_item(conn: sqlite3.Connection, work_item: sqlite3.Row | None) -> sqlite3.Row | None:
    if work_item is None or not _sqlite_table_exists(conn, "source_items"):
        return None
    return cast(
        sqlite3.Row | None,
        conn.execute(
            "SELECT * FROM source_items WHERE id = ?",
            (work_item["primary_source_item_id"],),
        ).fetchone(),
    )


def _sqlite_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _codex_prompts_for_attempt(conn: sqlite3.Connection, attempt_id: int) -> list[dict[str, object]]:
    placeholders = ", ".join("?" for _ in CODEX_PROMPT_EVENT_TYPES)
    rows = conn.execute(
        f"""
        SELECT id, thread_id, event_type, payload_json, created_at
        FROM codex_events
        WHERE attempt_id = ? AND event_type IN ({placeholders})
        ORDER BY id ASC
        """,
        (attempt_id, *CODEX_PROMPT_EVENT_TYPES),
    )
    prompts: list[dict[str, object]] = []
    for row in rows:
        payload = _json_object(row["payload_json"])
        prompts.append(
            {
                "id": int(row["id"]),
                "thread_id": str(row["thread_id"]),
                "event_type": str(row["event_type"]),
                "created_at": str(row["created_at"]),
                "cwd": _payload_string(payload, "cwd"),
                "model": _payload_string(payload, "model"),
                "approval_policy": _payload_string(payload, "approvalPolicy"),
                "prompt": _prompt_text_from_codex_payload(payload),
            }
        )
    return prompts


def _live_timeline_events(
    conn: sqlite3.Connection, attempt_id: int, after_id: int, limit: int
) -> list[AttemptLiveEvent]:
    rows = conn.execute(
        """
        SELECT id, phase, event_type, message, data_json, started_at, duration_ms
        FROM worker_timeline_events
        WHERE attempt_id = ? AND id > ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (attempt_id, after_id, limit),
    )
    events: list[AttemptLiveEvent] = []
    for row in rows:
        phase = str(row["phase"])
        event_type = str(row["event_type"])
        events.append(
            AttemptLiveEvent(
                source="timeline",
                id=int(row["id"]),
                event_type=event_type,
                created_at=str(row["started_at"]),
                title=f"{phase}/{event_type}",
                message=str(row["message"] or ""),
                payload={
                    "phase": phase,
                    "eventType": event_type,
                    "message": str(row["message"] or ""),
                    "durationMs": row["duration_ms"],
                    "data": _json_object(row["data_json"]),
                },
            )
        )
    return events


def _live_codex_events(
    conn: sqlite3.Connection, attempt_id: int, after_id: int, limit: int
) -> list[AttemptLiveEvent]:
    rows = conn.execute(
        """
        SELECT id, thread_id, event_type, payload_json, created_at
        FROM codex_events
        WHERE attempt_id = ? AND id > ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (attempt_id, after_id, limit),
    )
    events: list[AttemptLiveEvent] = []
    for row in rows:
        event_type = str(row["event_type"])
        payload = _json_object(row["payload_json"])
        output_delta = _codex_output_delta(event_type, payload)
        events.append(
            AttemptLiveEvent(
                source="codex",
                id=int(row["id"]),
                event_type=event_type,
                created_at=str(row["created_at"]),
                title=_codex_live_title(event_type),
                message=_codex_live_message(event_type, payload, output_delta),
                payload={
                    "threadId": str(row["thread_id"]),
                    "eventType": event_type,
                    "payload": payload,
                },
                output_delta=output_delta,
            )
        )
    return events


def _live_error_events(
    conn: sqlite3.Connection, attempt_id: int, after_id: int, limit: int
) -> list[AttemptLiveEvent]:
    rows = conn.execute(
        """
        SELECT id, phase, error_type, message, recoverable, log_excerpt, created_at
        FROM worker_errors
        WHERE attempt_id = ? AND id > ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (attempt_id, after_id, limit),
    )
    events: list[AttemptLiveEvent] = []
    for row in rows:
        phase = str(row["phase"])
        error_type = str(row["error_type"])
        events.append(
            AttemptLiveEvent(
                source="error",
                id=int(row["id"]),
                event_type=error_type,
                created_at=str(row["created_at"]),
                title=f"{phase}/{error_type}",
                message=str(row["message"]),
                payload={
                    "phase": phase,
                    "errorType": error_type,
                    "message": str(row["message"]),
                    "recoverable": bool(row["recoverable"]),
                    "logExcerpt": str(row["log_excerpt"] or ""),
                },
            )
        )
    return events


def _codex_live_title(event_type: str) -> str:
    if event_type in CODEX_PROMPT_EVENT_TYPES:
        return "Prompt sent"
    if event_type == "thread/start":
        return "Thread started"
    if event_type == "thread/tokenUsage/updated":
        return "Token usage updated"
    if event_type in CODEX_AGENT_MESSAGE_DELTA_EVENT_TYPES:
        return "Assistant output"
    if event_type == "turn/completed":
        return "Turn completed"
    return event_type.replace("_", " ").replace("/", " / ")


def _codex_live_message(event_type: str, payload: dict[str, Any], output_delta: str) -> str:
    if output_delta:
        return _clip_live_text(output_delta)
    if event_type in CODEX_PROMPT_EVENT_TYPES:
        prompt = _prompt_text_from_codex_payload(payload)
        return _clip_live_text(prompt) if prompt else "Prompt sent to Codex."
    if event_type == "thread/tokenUsage/updated":
        usage = codex_token_usage_from_payload(payload)
        if usage:
            return f"{usage.total_tokens:,} tokens"
    if event_type == "turn/completed":
        return "Codex turn completed."
    for key in ("message", "text", "title", "name", "status"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _clip_live_text(value)
    return ""


def _codex_output_delta(event_type: str, payload: dict[str, Any]) -> str:
    if event_type not in CODEX_AGENT_MESSAGE_DELTA_EVENT_TYPES:
        return ""
    value = payload.get("delta") or payload.get("text") or ""
    return str(value)


def _clip_live_text(value: str, *, limit: int = 220) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}..."


def _codex_token_usage_for_attempt(conn: sqlite3.Connection, attempt_id: int) -> CodexTokenUsage | None:
    event_usage = _codex_token_usage_from_events(conn, attempt_id)
    if event_usage:
        return event_usage
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COUNT(input_tokens) + COUNT(output_tokens) AS token_field_count
        FROM codex_turns
        WHERE attempt_id = ?
        """,
        (attempt_id,),
    ).fetchone()
    if not row or int(row["token_field_count"] or 0) == 0:
        return None
    input_tokens = int(row["input_tokens"] or 0)
    output_tokens = int(row["output_tokens"] or 0)
    return CodexTokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def _codex_token_usage_from_events(conn: sqlite3.Connection, attempt_id: int) -> CodexTokenUsage | None:
    rows = conn.execute(
        """
        SELECT thread_id, payload_json
        FROM codex_events
        WHERE attempt_id = ? AND event_type = 'thread/tokenUsage/updated'
        ORDER BY id ASC
        """,
        (attempt_id,),
    )
    latest_by_thread: dict[str, CodexTokenUsage] = {}
    for row in rows:
        usage = codex_token_usage_from_payload(_json_object(row["payload_json"]))
        if usage:
            latest_by_thread[str(row["thread_id"])] = usage
    if not latest_by_thread:
        return None
    return CodexTokenUsage(
        input_tokens=sum(usage.input_tokens for usage in latest_by_thread.values()),
        output_tokens=sum(usage.output_tokens for usage in latest_by_thread.values()),
        total_tokens=sum(usage.total_tokens for usage in latest_by_thread.values()),
        cached_input_tokens=sum(usage.cached_input_tokens for usage in latest_by_thread.values()),
        reasoning_output_tokens=sum(usage.reasoning_output_tokens for usage in latest_by_thread.values()),
    )


def codex_token_usage_from_payload(payload: Mapping[str, Any]) -> CodexTokenUsage | None:
    usage = _mapping_value(payload, "tokenUsage") or _mapping_value(payload, "token_usage")
    if usage is None:
        return None
    total = _mapping_value(usage, "total") or usage
    input_tokens = _int_value(total, "inputTokens", "input_tokens")
    output_tokens = _int_value(total, "outputTokens", "output_tokens")
    total_tokens = _int_value(total, "totalTokens", "total_tokens")
    cached_input_tokens = _int_value(total, "cachedInputTokens", "cached_input_tokens")
    reasoning_output_tokens = _int_value(total, "reasoningOutputTokens", "reasoning_output_tokens")
    if input_tokens == 0 and output_tokens == 0 and total_tokens == 0:
        return None
    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens
    return CodexTokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
    )


def _mapping_value(payload: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        return None
    return value


def _int_value(payload: Mapping[str, Any], *keys: str) -> int:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            return int(str(value))
        except ValueError:
            continue
    return 0


def _json_object(raw_value: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw_value))
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    return cast(dict[str, Any], value)


def _payload_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    return str(value)


def _prompt_text_from_codex_payload(payload: dict[str, Any]) -> str:
    direct_prompt = payload.get("prompt")
    if isinstance(direct_prompt, str):
        return direct_prompt
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return ""
    parts: list[str] = []
    for item in input_items:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not return a row id for an insert.")
    return cursor.lastrowid


def _migrate(conn: sqlite3.Connection) -> None:
    _add_column(conn, "attempts", "parent_attempt_id", "INTEGER")
    _add_column(conn, "attempts", "retry_count", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "attempts", "work_item_id", "INTEGER")
    _add_column(conn, "attempts", "work_item_run_id", "INTEGER")
    _add_column(conn, "workflow_instances", "work_item_id", "INTEGER")
    _add_column(conn, "workflow_instances", "work_item_run_id", "INTEGER")
    _add_column(conn, "workers", "pid", "INTEGER")
    _add_column(conn, "workers", "heartbeat_at", "TEXT")
    _add_column(conn, "workers", "deadline_at", "TEXT")
    _add_column(conn, "workers", "exit_code", "INTEGER")
    _add_column(conn, "workers", "stop_reason", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "pull_requests", "state", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "pull_requests", "merged_at", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "pull_requests", "worktree_cleaned_at", "TEXT")
    _add_column(conn, "pull_requests", "cleanup_error", "TEXT NOT NULL DEFAULT ''")


def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_locks(
        name TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        acquired_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS repos(
        full_name TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS issues(
        repo TEXT NOT NULL,
        number INTEGER NOT NULL,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        state TEXT NOT NULL,
        task_type TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        author TEXT NOT NULL DEFAULT '',
        labels_json TEXT NOT NULL,
        github_updated_at TEXT NOT NULL DEFAULT '',
        first_seen_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(repo, number),
        FOREIGN KEY(repo) REFERENCES repos(full_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS issue_labels(
        repo TEXT NOT NULL,
        issue_number INTEGER NOT NULL,
        label TEXT NOT NULL,
        PRIMARY KEY(repo, issue_number, label),
        FOREIGN KEY(repo, issue_number) REFERENCES issues(repo, number) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_versions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        content TEXT NOT NULL,
        parsed_config_json TEXT NOT NULL,
        status TEXT NOT NULL,
        error TEXT,
        diff TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_reload_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_version_id INTEGER,
        status TEXT NOT NULL,
        error TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(workflow_version_id) REFERENCES workflow_versions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attempts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        repo TEXT NOT NULL,
        issue_number INTEGER NOT NULL,
        task_type TEXT NOT NULL,
        workflow_version_id INTEGER,
        worker_id TEXT,
        work_item_id INTEGER,
        work_item_run_id INTEGER,
        status TEXT NOT NULL,
        outcome TEXT NOT NULL DEFAULT '',
        current_phase TEXT NOT NULL DEFAULT '',
        base_repo_path TEXT NOT NULL DEFAULT '',
        worktree_path TEXT NOT NULL DEFAULT '',
        branch TEXT NOT NULL DEFAULT '',
        commit_sha TEXT NOT NULL DEFAULT '',
        parent_attempt_id INTEGER,
        retry_count INTEGER NOT NULL DEFAULT 0,
        queued_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        duration_ms INTEGER,
        codex_duration_ms INTEGER NOT NULL DEFAULT 0,
        turn_count INTEGER NOT NULL DEFAULT 0,
        error_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(repo, issue_number) REFERENCES issues(repo, number),
        FOREIGN KEY(workflow_version_id) REFERENCES workflow_versions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_instances(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_version_id INTEGER,
        repo TEXT NOT NULL,
        issue_number INTEGER NOT NULL,
        attempt_id INTEGER,
        work_item_id INTEGER,
        work_item_run_id INTEGER,
        task_type TEXT NOT NULL,
        current_state TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        FOREIGN KEY(workflow_version_id) REFERENCES workflow_versions(id),
        FOREIGN KEY(repo, issue_number) REFERENCES issues(repo, number),
        FOREIGN KEY(attempt_id) REFERENCES attempts(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_action_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_instance_id INTEGER NOT NULL,
        workflow_version_id INTEGER,
        attempt_id INTEGER,
        transition_name TEXT NOT NULL,
        action_name TEXT NOT NULL,
        status TEXT NOT NULL,
        input_json TEXT NOT NULL DEFAULT '{}',
        output_json TEXT NOT NULL DEFAULT '{}',
        error TEXT NOT NULL DEFAULT '',
        idempotency_key TEXT NOT NULL DEFAULT '',
        retry_count INTEGER NOT NULL DEFAULT 0,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        started_monotonic_ns INTEGER NOT NULL,
        ended_monotonic_ns INTEGER,
        duration_ms INTEGER,
        FOREIGN KEY(workflow_instance_id) REFERENCES workflow_instances(id) ON DELETE CASCADE,
        FOREIGN KEY(workflow_version_id) REFERENCES workflow_versions(id),
        FOREIGN KEY(attempt_id) REFERENCES attempts(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_artifacts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_instance_id INTEGER NOT NULL,
        workflow_version_id INTEGER,
        workflow_action_run_id INTEGER,
        name TEXT NOT NULL,
        value_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(workflow_instance_id, name),
        FOREIGN KEY(workflow_instance_id) REFERENCES workflow_instances(id) ON DELETE CASCADE,
        FOREIGN KEY(workflow_version_id) REFERENCES workflow_versions(id),
        FOREIGN KEY(workflow_action_run_id) REFERENCES workflow_action_runs(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_transition_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_instance_id INTEGER NOT NULL,
        workflow_version_id INTEGER,
        transition_name TEXT NOT NULL,
        action_name TEXT NOT NULL,
        trigger TEXT NOT NULL,
        from_state TEXT NOT NULL,
        to_state TEXT NOT NULL,
        data_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        FOREIGN KEY(workflow_instance_id) REFERENCES workflow_instances(id) ON DELETE CASCADE,
        FOREIGN KEY(workflow_version_id) REFERENCES workflow_versions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_gates(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_instance_id INTEGER NOT NULL,
        workflow_version_id INTEGER,
        gate TEXT NOT NULL,
        transition_name TEXT NOT NULL,
        state TEXT NOT NULL,
        status TEXT NOT NULL,
        prompt TEXT NOT NULL DEFAULT '',
        decision TEXT NOT NULL DEFAULT '',
        decided_by TEXT NOT NULL DEFAULT '',
        decided_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(workflow_instance_id) REFERENCES workflow_instances(id) ON DELETE CASCADE,
        FOREIGN KEY(workflow_version_id) REFERENCES workflow_versions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workers(
        id TEXT PRIMARY KEY,
        attempt_id INTEGER,
        status TEXT NOT NULL,
        hostname TEXT NOT NULL,
        pid INTEGER,
        heartbeat_at TEXT,
        deadline_at TEXT,
        exit_code INTEGER,
        stop_reason TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL,
        completed_at TEXT,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(attempt_id) REFERENCES attempts(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS worker_timeline_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id INTEGER NOT NULL,
        phase TEXT NOT NULL,
        event_type TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        data_json TEXT NOT NULL DEFAULT '{}',
        started_at TEXT NOT NULL,
        ended_at TEXT,
        started_monotonic_ns INTEGER NOT NULL,
        ended_monotonic_ns INTEGER,
        duration_ms INTEGER,
        FOREIGN KEY(attempt_id) REFERENCES attempts(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS codex_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id INTEGER NOT NULL,
        thread_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(attempt_id) REFERENCES attempts(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS codex_turns(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id INTEGER NOT NULL,
        thread_id TEXT NOT NULL,
        turn_index INTEGER NOT NULL,
        status TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '',
        input_tokens INTEGER,
        output_tokens INTEGER,
        tool_call_count INTEGER NOT NULL DEFAULT 0,
        started_at TEXT NOT NULL,
        ended_at TEXT NOT NULL,
        started_monotonic_ns INTEGER NOT NULL,
        ended_monotonic_ns INTEGER NOT NULL,
        duration_ms INTEGER NOT NULL,
        FOREIGN KEY(attempt_id) REFERENCES attempts(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS worker_errors(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id INTEGER NOT NULL,
        turn_id INTEGER,
        phase TEXT NOT NULL,
        error_type TEXT NOT NULL,
        message TEXT NOT NULL,
        recoverable INTEGER NOT NULL DEFAULT 0,
        log_excerpt TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(attempt_id) REFERENCES attempts(id) ON DELETE CASCADE,
        FOREIGN KEY(turn_id) REFERENCES codex_turns(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS worker_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id INTEGER NOT NULL,
        level TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(attempt_id) REFERENCES attempts(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS worker_results(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id INTEGER NOT NULL,
        repo TEXT NOT NULL,
        issue_number INTEGER NOT NULL,
        result_type TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        status TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(attempt_id),
        FOREIGN KEY(attempt_id) REFERENCES attempts(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attempt_links(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_attempt_id INTEGER NOT NULL,
        target_attempt_id INTEGER NOT NULL,
        relationship TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        UNIQUE(source_attempt_id, target_attempt_id, relationship),
        FOREIGN KEY(source_attempt_id) REFERENCES attempts(id) ON DELETE CASCADE,
        FOREIGN KEY(target_attempt_id) REFERENCES attempts(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS issue_pull_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        repo TEXT NOT NULL,
        issue_number INTEGER NOT NULL,
        pull_request_number INTEGER NOT NULL,
        pull_request_url TEXT NOT NULL,
        pull_request_title TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT '',
        link_source TEXT NOT NULL,
        marker TEXT NOT NULL,
        verified_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(repo, issue_number, pull_request_number),
        FOREIGN KEY(repo, issue_number) REFERENCES issues(repo, number) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pull_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id INTEGER NOT NULL,
        repo TEXT NOT NULL,
        number INTEGER NOT NULL,
        url TEXT NOT NULL,
        title TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT '',
        merged_at TEXT NOT NULL DEFAULT '',
        worktree_cleaned_at TEXT,
        cleanup_error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(repo, number),
        FOREIGN KEY(attempt_id) REFERENCES attempts(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS comments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id INTEGER,
        repo TEXT NOT NULL,
        issue_number INTEGER NOT NULL,
        url TEXT NOT NULL,
        body TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(attempt_id) REFERENCES attempts(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orchestrator_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        message TEXT NOT NULL,
        data_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ask_threads(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question TEXT NOT NULL,
        answer TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
]
