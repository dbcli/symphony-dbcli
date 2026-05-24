from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from .actions import DEFAULT_ACTION_REGISTRY

type TriggerKind = Literal["automatic", "human"]
type SetupRunPolicy = Literal["per_attempt", "per_repo", "manual"]
type SetupWorkingDirectory = Literal["workspace", "repo"]
type ConfigTable = dict[str, Any]


TRIGGER_KINDS = frozenset({"automatic", "human"})
SETUP_RUN_POLICIES = frozenset({"per_attempt", "per_repo", "manual"})
SETUP_WORKING_DIRECTORIES = frozenset({"workspace", "repo"})
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class WorkflowStateConfig:
    description: str = ""
    terminal: bool = False
    gate: str = ""


@dataclass(frozen=True)
class WorkflowTransitionConfig:
    from_state: str
    to_state: str
    action: str
    trigger: TriggerKind = "automatic"
    description: str = ""
    condition: str = ""
    gate: str = ""
    on_failure: str = "failed"
    retry_limit: int = 0
    timeout_seconds: int = 0
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowDefinitionConfig:
    initial_state: str = "todo"
    terminal_states: list[str] = field(default_factory=lambda: ["done", "failed", "blocked"])
    states: dict[str, WorkflowStateConfig] = field(default_factory=dict)
    transitions: dict[str, WorkflowTransitionConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerPreferencesConfig:
    run_review_after_code_change: bool = True
    preferred_test_strategy: str = "unit"
    require_tests_for_code_changes: bool = True
    coding_style: list[str] = field(
        default_factory=lambda: [
            "Keep changes focused on the issue.",
            "Prefer narrow unit tests before broader integration tests.",
            "Avoid unrelated refactors.",
        ]
    )
    additional_instructions: str = ""


@dataclass(frozen=True)
class SetupStepConfig:
    command: list[str]
    description: str = ""
    run: SetupRunPolicy = "per_attempt"
    cwd: SetupWorkingDirectory = "workspace"
    timeout_seconds: int = 600
    blocks_worker: bool = True


@dataclass(frozen=True)
class SetupConfig:
    enabled: bool = True
    steps: dict[str, SetupStepConfig] = field(default_factory=dict)


def default_workflow_definition() -> WorkflowDefinitionConfig:
    return WorkflowDefinitionConfig(
        initial_state="todo",
        terminal_states=["done", "failed", "blocked"],
        states={
            "todo": WorkflowStateConfig("Issue is eligible for Symphony dispatch."),
            "claimed": WorkflowStateConfig("Issue has been claimed and labeled as working."),
            "workspace_ready": WorkflowStateConfig("An isolated workspace has been prepared."),
            "setup_complete": WorkflowStateConfig("Configured setup steps have completed."),
            "worker_complete": WorkflowStateConfig("Codex has produced a durable worker result."),
            "review": WorkflowStateConfig(
                "Human review is required before GitHub side effects.", gate="review"
            ),
            "pr_ready": WorkflowStateConfig("A draft pull request exists and is waiting for merge."),
            "done": WorkflowStateConfig("Workflow completed successfully.", terminal=True),
            "failed": WorkflowStateConfig("Workflow failed and requires inspection.", terminal=True),
            "blocked": WorkflowStateConfig("Workflow is blocked by a human decision.", terminal=True),
        },
        transitions={
            "claim_issue": WorkflowTransitionConfig(
                from_state="todo",
                to_state="claimed",
                action="github.apply_labels",
                description="Move a dispatchable issue into the working state.",
            ),
            "allocate_workspace": WorkflowTransitionConfig(
                from_state="claimed",
                to_state="workspace_ready",
                action="workspace.allocate",
                description="Create the per-attempt workspace.",
            ),
            "run_setup": WorkflowTransitionConfig(
                from_state="workspace_ready",
                to_state="setup_complete",
                action="workspace.run_setup",
                description="Run configured setup commands before Codex starts.",
            ),
            "research_issue": WorkflowTransitionConfig(
                from_state="setup_complete",
                to_state="worker_complete",
                action="codex.research_issue",
                description="Use Codex to draft a research or support answer.",
                condition='task.type == "research"',
            ),
            "fix_issue": WorkflowTransitionConfig(
                from_state="setup_complete",
                to_state="worker_complete",
                action="codex.fix_issue",
                description="Use Codex to implement a code change.",
                condition='task.type == "code"',
            ),
            "request_review": WorkflowTransitionConfig(
                from_state="worker_complete",
                to_state="review",
                action="github.apply_labels",
                description="Move completed worker output into human review.",
            ),
            "post_answer": WorkflowTransitionConfig(
                from_state="review",
                to_state="done",
                action="github.post_issue_comment",
                trigger="human",
                gate="review_answer",
                description="Post an edited research answer to GitHub.",
                condition='task.type == "research"',
            ),
            "create_draft_pr": WorkflowTransitionConfig(
                from_state="review",
                to_state="pr_ready",
                action="github.create_draft_pr",
                trigger="human",
                gate="review_diff",
                description="Create a draft pull request after human diff review.",
                condition='task.type == "code"',
            ),
            "cleanup_after_merge": WorkflowTransitionConfig(
                from_state="pr_ready",
                to_state="done",
                action="workspace.cleanup_after_merge",
                description="Clean up the workspace after the pull request is merged.",
                condition="pull_request.is_merged",
            ),
            "mark_blocked": WorkflowTransitionConfig(
                from_state="review",
                to_state="blocked",
                action="github.apply_labels",
                trigger="human",
                gate="mark_blocked",
                description="Let a human stop progress when review cannot continue.",
            ),
        },
    )


def default_worker_preferences() -> WorkerPreferencesConfig:
    return WorkerPreferencesConfig()


def default_setup_config() -> SetupConfig:
    return SetupConfig()


def workflow_definition_from_dict(data: ConfigTable) -> WorkflowDefinitionConfig:
    if not data:
        return default_workflow_definition()
    states = {
        name: _state_from_dict(name, state_data) for name, state_data in _nested_table(data, "states").items()
    }
    transitions = {
        name: _transition_from_dict(name, transition_data)
        for name, transition_data in _nested_table(data, "transitions").items()
    }
    return WorkflowDefinitionConfig(
        initial_state=_str_value(data, "initial_state", default_workflow_definition().initial_state),
        terminal_states=_str_list(data, "terminal_states", default_workflow_definition().terminal_states),
        states=states or default_workflow_definition().states,
        transitions=transitions or default_workflow_definition().transitions,
    )


def worker_preferences_from_dict(data: ConfigTable) -> WorkerPreferencesConfig:
    if not data:
        return default_worker_preferences()
    return WorkerPreferencesConfig(
        run_review_after_code_change=_bool_value(data, "run_review_after_code_change", True),
        preferred_test_strategy=_str_value(data, "preferred_test_strategy", "unit"),
        require_tests_for_code_changes=_bool_value(data, "require_tests_for_code_changes", True),
        coding_style=_str_list(data, "coding_style", default_worker_preferences().coding_style),
        additional_instructions=_str_value(data, "additional_instructions", ""),
    )


def setup_config_from_dict(data: ConfigTable) -> SetupConfig:
    if not data:
        return default_setup_config()
    steps = {
        name: _setup_step_from_dict(name, step_data)
        for name, step_data in _nested_table(data, "steps").items()
    }
    return SetupConfig(
        enabled=_bool_value(data, "enabled", True),
        steps=steps,
    )


def validate_workflow_definition(workflow: WorkflowDefinitionConfig) -> list[str]:
    errors: list[str] = []
    if not workflow.states:
        errors.append("workflow.states must include at least one state.")
        return errors
    if workflow.initial_state not in workflow.states:
        errors.append(f"workflow.initial_state '{workflow.initial_state}' is not defined in workflow.states.")
    for state_name in workflow.states:
        if not NAME_RE.match(state_name):
            errors.append(f"workflow state '{state_name}' must use letters, numbers, hyphen, or underscore.")
    terminal_states = set(workflow.terminal_states)
    for state_name in terminal_states:
        state = workflow.states.get(state_name)
        if state is None:
            errors.append(f"workflow.terminal_states references undefined state '{state_name}'.")
        elif not state.terminal:
            errors.append(f"workflow state '{state_name}' must set terminal = true.")
    for transition_name, transition in workflow.transitions.items():
        errors.extend(_transition_errors(transition_name, transition, workflow.states, terminal_states))
    errors.extend(_unreachable_state_errors(workflow))
    return errors


def validate_worker_preferences(preferences: WorkerPreferencesConfig) -> list[str]:
    errors: list[str] = []
    if preferences.preferred_test_strategy not in {"unit", "integration", "balanced", "project_default"}:
        errors.append(
            "preferences.preferred_test_strategy must be 'unit', 'integration', 'balanced', or 'project_default'."
        )
    if any(not item.strip() for item in preferences.coding_style):
        errors.append("preferences.coding_style entries must not be empty.")
    return errors


def validate_setup_config(setup: SetupConfig) -> list[str]:
    errors: list[str] = []
    for step_name, step in setup.steps.items():
        if not NAME_RE.match(step_name):
            errors.append(f"setup step '{step_name}' must use letters, numbers, hyphen, or underscore.")
        if not step.command:
            errors.append(f"setup.steps.{step_name}.command must include at least one argument.")
        if any(not argument.strip() for argument in step.command):
            errors.append(f"setup.steps.{step_name}.command entries must not be empty.")
        if step.run not in SETUP_RUN_POLICIES:
            errors.append(f"setup.steps.{step_name}.run must be 'per_attempt', 'per_repo', or 'manual'.")
        if step.cwd not in SETUP_WORKING_DIRECTORIES:
            errors.append(f"setup.steps.{step_name}.cwd must be 'workspace' or 'repo'.")
        if step.timeout_seconds < 1:
            errors.append(f"setup.steps.{step_name}.timeout_seconds must be at least 1.")
    return errors


def _state_from_dict(name: str, data: Any) -> WorkflowStateConfig:
    table = _table_value(data, f"workflow.states.{name}")
    return WorkflowStateConfig(
        description=_str_value(table, "description", ""),
        terminal=_bool_value(table, "terminal", False),
        gate=_str_value(table, "gate", ""),
    )


def _transition_from_dict(name: str, data: Any) -> WorkflowTransitionConfig:
    table = _table_value(data, f"workflow.transitions.{name}")
    return WorkflowTransitionConfig(
        from_state=_required_str(table, "from_state", f"workflow.transitions.{name}"),
        to_state=_required_str(table, "to_state", f"workflow.transitions.{name}"),
        action=_required_str(table, "action", f"workflow.transitions.{name}"),
        trigger=_trigger_kind(_str_value(table, "trigger", "automatic")),
        description=_str_value(table, "description", ""),
        condition=_str_value(table, "condition", ""),
        gate=_str_value(table, "gate", ""),
        on_failure=_str_value(table, "on_failure", "failed"),
        retry_limit=_int_value(table, "retry_limit", 0),
        timeout_seconds=_int_value(table, "timeout_seconds", 0),
        inputs=_str_map(table, "inputs", f"workflow.transitions.{name}"),
        outputs=_str_map(table, "outputs", f"workflow.transitions.{name}"),
    )


def _setup_step_from_dict(name: str, data: Any) -> SetupStepConfig:
    table = _table_value(data, f"setup.steps.{name}")
    return SetupStepConfig(
        command=_required_str_list(table, "command", f"setup.steps.{name}"),
        description=_str_value(table, "description", ""),
        run=_setup_run_policy(_str_value(table, "run", "per_attempt")),
        cwd=_setup_working_directory(_str_value(table, "cwd", "workspace")),
        timeout_seconds=_int_value(table, "timeout_seconds", 600),
        blocks_worker=_bool_value(table, "blocks_worker", True),
    )


def _transition_errors(
    name: str,
    transition: WorkflowTransitionConfig,
    states: dict[str, WorkflowStateConfig],
    terminal_states: set[str],
) -> list[str]:
    errors: list[str] = []
    if not NAME_RE.match(name):
        errors.append(f"workflow transition '{name}' must use letters, numbers, hyphen, or underscore.")
    if transition.from_state not in states:
        errors.append(
            f"workflow.transitions.{name}.from_state references undefined state '{transition.from_state}'."
        )
    if transition.to_state not in states:
        errors.append(
            f"workflow.transitions.{name}.to_state references undefined state '{transition.to_state}'."
        )
    if transition.on_failure and transition.on_failure not in states:
        errors.append(
            f"workflow.transitions.{name}.on_failure references undefined state '{transition.on_failure}'."
        )
    primitive = DEFAULT_ACTION_REGISTRY.get(transition.action)
    if primitive is None:
        errors.append(f"workflow.transitions.{name}.action '{transition.action}' is not a known primitive.")
    elif transition.trigger == "automatic" and not primitive.automatic_allowed:
        errors.append(f"workflow.transitions.{name}.action '{transition.action}' cannot run automatically.")
    elif transition.trigger == "human" and not primitive.human_gate_allowed:
        errors.append(
            f"workflow.transitions.{name}.action '{transition.action}' cannot run behind a human gate."
        )
    if primitive is not None:
        errors.extend(_mapping_errors(name, "inputs", transition.inputs, primitive.input_fields))
        errors.extend(_mapping_errors(name, "outputs", transition.outputs, primitive.output_fields))
    if transition.trigger not in TRIGGER_KINDS:
        errors.append(f"workflow.transitions.{name}.trigger must be 'automatic' or 'human'.")
    if transition.trigger == "human" and not transition.gate:
        errors.append(f"workflow.transitions.{name}.gate is required for human transitions.")
    if transition.from_state in terminal_states:
        errors.append(f"workflow.transitions.{name}.from_state must not be terminal.")
    if transition.retry_limit < 0:
        errors.append(f"workflow.transitions.{name}.retry_limit must be at least 0.")
    if transition.timeout_seconds < 0:
        errors.append(f"workflow.transitions.{name}.timeout_seconds must be at least 0.")
    return errors


def _mapping_errors(
    transition_name: str,
    mapping_name: str,
    mapping: dict[str, str],
    allowed_fields: frozenset[str],
) -> list[str]:
    unknown = sorted(set(mapping) - allowed_fields)
    return [
        f"workflow.transitions.{transition_name}.{mapping_name}.{field} is not valid for this primitive."
        for field in unknown
    ]


def _unreachable_state_errors(workflow: WorkflowDefinitionConfig) -> list[str]:
    reachable = {workflow.initial_state}
    transitions = list(workflow.transitions.values())
    changed = True
    while changed:
        changed = False
        for transition in transitions:
            if transition.from_state in reachable:
                for target in (transition.to_state, transition.on_failure):
                    if target and target not in reachable:
                        reachable.add(target)
                        changed = True
    unreachable = sorted(set(workflow.states) - reachable)
    return [
        f"workflow state '{state}' is not reachable from '{workflow.initial_state}'." for state in unreachable
    ]


def _nested_table(data: ConfigTable, key: str) -> ConfigTable:
    value = data.get(key, {})
    return _table_value(value, key)


def _table_value(value: Any, path: str) -> ConfigTable:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a TOML table.")
    return value


def _required_str(data: ConfigTable, key: str, path: str) -> str:
    if key not in data:
        raise ValueError(f"{path}.{key} is required.")
    return _str_value(data, key, "")


def _str_value(data: ConfigTable, key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string.")
    return value


def _bool_value(data: ConfigTable, key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false.")
    return value


def _int_value(data: ConfigTable, key: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer.")
    return value


def _str_list(data: ConfigTable, key: str, default: list[str]) -> list[str]:
    value = data.get(key, default)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of strings.")
    return list(value)


def _str_map(data: ConfigTable, key: str, path: str) -> dict[str, str]:
    value = data.get(key, {})
    if not isinstance(value, dict) or any(
        not isinstance(field, str) or not isinstance(target, str) for field, target in value.items()
    ):
        raise ValueError(f"{path}.{key} must be a table of string keys and values.")
    return dict(value)


def _required_str_list(data: ConfigTable, key: str, path: str) -> list[str]:
    if key not in data:
        raise ValueError(f"{path}.{key} is required.")
    return _str_list(data, key, [])


def _trigger_kind(value: str) -> TriggerKind:
    if value not in TRIGGER_KINDS:
        raise ValueError("trigger must be 'automatic' or 'human'.")
    return cast(TriggerKind, value)


def _setup_run_policy(value: str) -> SetupRunPolicy:
    if value not in SETUP_RUN_POLICIES:
        raise ValueError("run must be 'per_attempt', 'per_repo', or 'manual'.")
    return cast(SetupRunPolicy, value)


def _setup_working_directory(value: str) -> SetupWorkingDirectory:
    if value not in SETUP_WORKING_DIRECTORIES:
        raise ValueError("cwd must be 'workspace' or 'repo'.")
    return cast(SetupWorkingDirectory, value)
