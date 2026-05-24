from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

from symphony_dbcli.config import WorkspaceConfig, default_config
from symphony_dbcli.github import GitHubIssue, PullRequest
from symphony_dbcli.orchestrator import Orchestrator, build_worker_prompt
from symphony_dbcli.primitive_executor import PrimitiveContext, PrimitiveOutcome
from symphony_dbcli.store import IssueSnapshot, Store


def test_code_follow_up_prompt_includes_research_context() -> None:
    prompt = build_worker_prompt(
        default_config(),
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        title="Logging support question",
        follow_up_context="Research result:\nExpand log_file before checking its parent directory.",
    )

    assert "Task type: code" in prompt
    assert "Follow-up context:" in prompt
    assert "Expand log_file" in prompt


def test_worker_prompt_includes_primitive_guidance() -> None:
    prompt = build_worker_prompt(
        default_config(),
        repo="dbcli/litecli",
        issue_number=245,
        task_type="research",
        title="Logging support question",
        primitive_guidance=["Keep the reply under two sentences.", "Stay succinct."],
    )

    assert "Primitive guidance:" in prompt
    assert "Keep the reply under two sentences." in prompt
    assert "Stay succinct." in prompt


def test_orchestrator_claim_records_workflow_runtime_state(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)

    attempt_id = Orchestrator(default_config(), store, github=FakeCleanupGitHub()).claim_next()

    assert attempt_id is not None
    instance = store.workflow_instance_for_attempt(attempt_id)
    assert instance is not None
    assert instance["current_state"] == "claimed"
    with store.connect() as conn:
        action_run = conn.execute(
            "SELECT * FROM workflow_action_runs WHERE workflow_instance_id = ?", (instance["id"],)
        ).fetchone()
        transition = conn.execute(
            "SELECT * FROM workflow_transition_events WHERE workflow_instance_id = ?", (instance["id"],)
        ).fetchone()
    assert action_run is not None
    assert action_run["transition_name"] == "claim_issue"
    assert action_run["status"] == "succeeded"
    assert transition is not None
    assert transition["from_state"] == "todo"
    assert transition["to_state"] == "claimed"


def test_orchestrator_runs_attempt_from_workflow_transitions(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="queued",
    )
    primitives = FakeWorkflowPrimitives()

    Orchestrator(
        default_config(),
        store,
        github=FakeCleanupGitHub(),
        primitives=primitives,
    ).run_attempt(attempt_id)

    instance = store.workflow_instance_for_attempt(attempt_id)
    detail = store.attempt_detail(attempt_id)
    gates = store.pending_workflow_gates()
    with store.connect() as conn:
        action_runs = list(
            conn.execute(
                "SELECT transition_name, action_name, status FROM workflow_action_runs ORDER BY id ASC"
            )
        )
    assert primitives.transitions == ["allocate_workspace", "run_setup", "fix_issue", "request_review"]
    assert instance is not None
    assert instance["current_state"] == "review"
    assert detail is not None
    assert detail["attempt"]["status"] == "review"
    assert [row["transition_name"] for row in action_runs] == primitives.transitions
    assert {row["transition_name"] for row in gates} == {"create_draft_pr", "mark_blocked"}


def test_orchestrator_runs_human_gate_from_workflow_transition(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="review",
    )
    instance_id = store.create_workflow_instance(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        initial_state="review",
        attempt_id=attempt_id,
    )
    gate_id = store.open_workflow_gate(
        instance_id=instance_id,
        workflow_version_id=None,
        gate="review_diff",
        transition_name="create_draft_pr",
        state="review",
        prompt="Review the generated diff.",
    )
    primitives = FakeWorkflowPrimitives()

    result = Orchestrator(
        default_config(),
        store,
        github=FakeCleanupGitHub(),
        primitives=primitives,
    ).run_human_gate(gate_id)

    instance = store.workflow_instance_by_id(instance_id)
    gate = store.workflow_gate_by_id(gate_id)
    attempt = store.attempt_by_id(attempt_id)
    assert primitives.transitions == ["create_draft_pr"]
    assert result.current_state == "pr_ready"
    assert result.stop_reason == "no_transition"
    assert instance is not None
    assert instance["current_state"] == "pr_ready"
    assert gate is not None
    assert gate["status"] == "resolved"
    assert gate["decision"] == "approved"
    assert attempt is not None
    assert attempt["outcome"] == "draft_pr_created"


def test_orchestrator_cleans_worktree_after_pr_merge(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    source = tmp_path / "source"
    bare = tmp_path / "repos" / "litecli.git"
    worktree = tmp_path / "worktrees" / "litecli"
    source.mkdir()
    bare.parent.mkdir()
    worktree.parent.mkdir()
    _git(source, "init", "--initial-branch=main")
    (source / "README.md").write_text("start\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    subprocess.run(["git", "clone", "--bare", str(source), str(bare)], check=True, capture_output=True)
    subprocess.run(
        ["git", "--git-dir", str(bare), "worktree", "add", str(worktree), "main"],
        check=True,
        capture_output=True,
    )
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        status="review",
    )
    store.update_attempt_workspace(
        attempt_id,
        base_repo_path=str(bare),
        worktree_path=str(worktree),
        branch="symphony/dbcli-litecli-245-attempt-1",
    )
    store.record_pr(
        attempt_id,
        repo="dbcli/litecli",
        number=254,
        url="https://github.com/dbcli/litecli/pull/254",
        title="Fix #245",
    )
    config = replace(
        default_config(),
        workspace=WorkspaceConfig(root=str(worktree.parent), bare_repos_root=str(bare.parent)),
    )

    summary = Orchestrator(config, store, github=FakeCleanupGitHub()).cleanup_merged_pull_request_worktrees()

    detail = store.attempt_detail(attempt_id)
    assert summary.scanned == 1
    assert summary.merged == 1
    assert summary.cleaned == 1
    assert not worktree.exists()
    assert detail is not None
    pull_request = detail["pull_requests"][0]
    assert pull_request["state"] == "closed"
    assert pull_request["merged_at"] == "2026-05-24T14:00:00Z"
    assert pull_request["worktree_cleaned_at"]
    assert detail["timeline"][-1]["event_type"] == "cleaned_after_pr_merge"


def test_orchestrator_skips_open_pr_worktree_cleanup(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    attempt_id = store.create_attempt(
        repo="dbcli/litecli",
        issue_number=245,
        task_type="code",
        workflow_version_id=None,
        worktree_path=str(tmp_path / "worktrees" / "litecli"),
        base_repo_path=str(tmp_path / "repos" / "litecli.git"),
        status="review",
    )
    store.record_pr(
        attempt_id,
        repo="dbcli/litecli",
        number=254,
        url="https://github.com/dbcli/litecli/pull/254",
        title="Fix #245",
    )
    summary = Orchestrator(
        default_config(), store, github=FakeCleanupGitHub(merged=False)
    ).cleanup_merged_pull_request_worktrees()

    detail = store.attempt_detail(attempt_id)
    assert summary.scanned == 1
    assert summary.skipped == 1
    assert detail is not None
    assert detail["pull_requests"][0]["state"] == "open"
    assert detail["pull_requests"][0]["worktree_cleaned_at"] is None


class FakeCleanupGitHub:
    def __init__(self, *, merged: bool = True) -> None:
        self.merged = merged

    def list_issues(self, repo: str, labels: list[str] | None = None) -> list[GitHubIssue]:
        return []

    def add_labels(self, repo: str, issue_number: int, labels: list[str]) -> None:
        return

    def remove_label(self, repo: str, issue_number: int, label: str) -> None:
        return

    def pull_request(self, repo: str, number: int) -> PullRequest:
        if not self.merged:
            return PullRequest(
                number=number,
                url=f"https://github.com/{repo}/pull/{number}",
                title="Fix #245",
                state="open",
            )
        return PullRequest(
            number=number,
            url=f"https://github.com/{repo}/pull/{number}",
            title="Fix #245",
            state="closed",
            merged_at="2026-05-24T14:00:00Z",
        )


class FakeWorkflowPrimitives:
    def __init__(self) -> None:
        self.transitions: list[str] = []

    def fetch_issues(self) -> PrimitiveOutcome:
        return PrimitiveOutcome({"synced": 0})

    def execute(self, context: PrimitiveContext) -> PrimitiveOutcome:
        self.transitions.append(context.transition_name)
        return PrimitiveOutcome({"transition": context.transition_name})


def _seed_store(tmp_path: Path) -> Store:
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
    return store


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)
