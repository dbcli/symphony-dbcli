from __future__ import annotations

import hashlib
import os
import re
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .workflow_definition import (
    SetupConfig,
    WorkerPreferencesConfig,
    WorkflowDefinitionConfig,
    default_setup_config,
    default_worker_preferences,
    default_workflow_definition,
    setup_config_from_dict,
    validate_setup_config,
    validate_worker_preferences,
    validate_workflow_definition,
    worker_preferences_from_dict,
    workflow_definition_from_dict,
)


class WorkflowError(ValueError):
    """Raised when WORKFLOW.md cannot be parsed or validated."""


DEFAULT_INSTRUCTIONS = """\
Workers should be direct and evidence-driven.

For coding tasks:
- Inspect the GitHub issue and relevant repository context before editing.
- Keep changes focused on the issue.
- Run the narrowest meaningful tests and report the commands used.
- Leave unrelated files and metadata untouched.

For research/support tasks:
- Read the issue discussion and relevant code/docs before answering.
- Prefer concise answers with links or file references where useful.
- Save a draft answer for review; do not post a final answer automatically.
"""


@dataclass(frozen=True)
class LabelConfig:
    todo: str = "symphony:todo"
    working: str = "symphony:working"
    review: str = "symphony:review"
    blocked: str = "symphony:blocked"
    done: str = "symphony:done"
    type_code: str = "symphony:type:code"
    type_research: str = "symphony:type:research"
    priority_high: str = "symphony:priority:high"
    priority_low: str = "symphony:priority:low"


@dataclass(frozen=True)
class GitHubConfig:
    repos: list[str] = field(default_factory=lambda: ["dbcli/pgcli", "dbcli/mycli", "dbcli/litecli"])
    auth_strategy: str = "auto"
    token_env: str = "SYMPHONY_GITHUB_TOKEN"
    fallback_token_env: str = "GH_TOKEN"
    app_id_env: str = "SYMPHONY_GITHUB_APP_ID"
    installation_id_env: str = "SYMPHONY_GITHUB_INSTALLATION_ID"
    private_key_env: str = "SYMPHONY_GITHUB_PRIVATE_KEY"
    private_key_path_env: str = "SYMPHONY_GITHUB_PRIVATE_KEY_PATH"
    webhook_secret_env: str = "SYMPHONY_GITHUB_WEBHOOK_SECRET"
    api_base_url: str = "https://api.github.com"


@dataclass(frozen=True)
class TrackerConfig:
    kind: str = "github"


@dataclass(frozen=True)
class ProfileConfig:
    active: str = "local"


@dataclass(frozen=True)
class WorkspaceConfig:
    strategy: str = "worktree"
    root: str = ".symphony/worktrees"
    bare_repos_root: str = ".symphony/repos"
    retention_days: int = 14
    branch_prefix: str = "symphony"
    base_branch: str = ""


@dataclass(frozen=True)
class WorkerConfig:
    max_global: int = 3
    max_per_repo: int = 1
    default_task_type: str = "research"
    poll_interval_seconds: int = 60
    heartbeat_interval_seconds: int = 15
    heartbeat_timeout_seconds: int = 120
    max_runtime_seconds: int = 3600
    retry_limit: int = 1
    shutdown_grace_seconds: int = 10


@dataclass(frozen=True)
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass(frozen=True)
class DatabaseConfig:
    path: str = ".symphony/symphony.db"


@dataclass(frozen=True)
class CodexConfig:
    command: str = "codex"
    transport: str = "app-server"
    app_server_listen: str = "stdio://"
    model: str = ""
    approval_policy: str = "never"
    sandbox: str = "workspace-write"


@dataclass(frozen=True)
class PolicyConfig:
    dry_run: bool = True


@dataclass(frozen=True)
class WorkflowConfig:
    profile: ProfileConfig = field(default_factory=ProfileConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    labels: LabelConfig = field(default_factory=LabelConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    workers: WorkerConfig = field(default_factory=WorkerConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    workflow: WorkflowDefinitionConfig = field(default_factory=default_workflow_definition)
    preferences: WorkerPreferencesConfig = field(default_factory=default_worker_preferences)
    setup: SetupConfig = field(default_factory=default_setup_config)
    instructions: str = DEFAULT_INSTRUCTIONS

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("instructions", None)
        return data

    @property
    def dispatch_labels(self) -> set[str]:
        return {self.labels.todo}


FENCE_RE = re.compile(r"```toml\s*\n(?P<body>.*?)\n```", re.DOTALL | re.IGNORECASE)
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
type ConfigTable = dict[str, Any]


def default_config() -> WorkflowConfig:
    return WorkflowConfig(instructions=DEFAULT_INSTRUCTIONS)


def default_config_for_profile(profile: str | None = None) -> WorkflowConfig:
    data = default_config().to_dict()
    data["profiles"] = default_profiles()
    return config_from_dict(data, profile=profile, instructions=DEFAULT_INSTRUCTIONS)


def workflow_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def load_workflow(path: str | Path = "WORKFLOW.md", profile: str | None = None) -> WorkflowConfig:
    content = Path(path).read_text(encoding="utf-8")
    return parse_workflow(content, profile=profile)


def parse_workflow(content: str, profile: str | None = None) -> WorkflowConfig:
    match = FENCE_RE.search(content)
    if not match:
        raise WorkflowError("WORKFLOW.md must contain one fenced toml config block.")
    try:
        data = tomllib.loads(match.group("body"))
    except tomllib.TOMLDecodeError as exc:
        raise WorkflowError(f"Invalid TOML in WORKFLOW.md: {exc}") from exc

    config = config_from_dict(data, profile=profile, instructions=_instructions_from_markdown(content))
    validate_config(config)
    return config


def config_from_dict(
    data: ConfigTable,
    profile: str | None = None,
    instructions: str = DEFAULT_INSTRUCTIONS,
) -> WorkflowConfig:
    active_profile = _selected_profile(data, profile)
    merged = _apply_profile(data, active_profile)
    try:
        return WorkflowConfig(
            profile=ProfileConfig(active=active_profile),
            tracker=TrackerConfig(**_section(merged, "tracker")),
            github=GitHubConfig(**_section(merged, "github")),
            labels=LabelConfig(**_section(merged, "labels")),
            workspace=WorkspaceConfig(**_section(merged, "workspace")),
            workers=WorkerConfig(**_section(merged, "workers")),
            dashboard=DashboardConfig(**_section(merged, "dashboard")),
            database=DatabaseConfig(**_section(merged, "database")),
            codex=CodexConfig(**_section(merged, "codex")),
            policy=_policy_config(merged),
            workflow=workflow_definition_from_dict(_section(merged, "workflow")),
            preferences=worker_preferences_from_dict(_section(merged, "preferences")),
            setup=setup_config_from_dict(_section(merged, "setup")),
            instructions=instructions.strip() or DEFAULT_INSTRUCTIONS,
        )
    except TypeError as exc:
        raise WorkflowError(str(exc)) from exc
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc


def validate_config(config: WorkflowConfig) -> None:
    errors: list[str] = []
    if config.tracker.kind != "github":
        errors.append("tracker.kind must be 'github'.")
    if not config.profile.active:
        errors.append("profile.active must not be empty.")
    if config.workspace.strategy != "worktree":
        errors.append("workspace.strategy must be 'worktree' until clone support is implemented.")
    if not config.workspace.branch_prefix:
        errors.append("workspace.branch_prefix must not be empty.")
    if config.github.auth_strategy not in {"auto", "github_app", "token"}:
        errors.append("github.auth_strategy must be 'auto', 'github_app', or 'token'.")
    if config.workers.max_global < 1:
        errors.append("workers.max_global must be at least 1.")
    if config.workers.max_per_repo < 1:
        errors.append("workers.max_per_repo must be at least 1.")
    if config.workers.default_task_type not in {"code", "research"}:
        errors.append("workers.default_task_type must be 'code' or 'research'.")
    if config.workers.poll_interval_seconds < 1:
        errors.append("workers.poll_interval_seconds must be at least 1.")
    if config.workers.heartbeat_interval_seconds < 1:
        errors.append("workers.heartbeat_interval_seconds must be at least 1.")
    if config.workers.heartbeat_timeout_seconds < config.workers.heartbeat_interval_seconds:
        errors.append("workers.heartbeat_timeout_seconds must be at least heartbeat_interval_seconds.")
    if config.workers.max_runtime_seconds < 1:
        errors.append("workers.max_runtime_seconds must be at least 1.")
    if config.workers.retry_limit < 0:
        errors.append("workers.retry_limit must be at least 0.")
    if config.workers.shutdown_grace_seconds < 0:
        errors.append("workers.shutdown_grace_seconds must be at least 0.")
    if config.dashboard.port < 1 or config.dashboard.port > 65535:
        errors.append("dashboard.port must be between 1 and 65535.")
    if not config.github.repos:
        errors.append("github.repos must include at least one repository.")
    for repo in config.github.repos:
        if not REPO_RE.match(repo):
            errors.append(f"github.repos contains invalid repository '{repo}'.")
    label_values = list(asdict(config.labels).values())
    duplicates = {label for label in label_values if label_values.count(label) > 1}
    if duplicates:
        errors.append(f"labels must be unique: {', '.join(sorted(duplicates))}.")
    errors.extend(validate_workflow_definition(config.workflow))
    errors.extend(validate_worker_preferences(config.preferences))
    errors.extend(validate_setup_config(config.setup))
    if errors:
        raise WorkflowError(" ".join(errors))


def render_workflow(config: WorkflowConfig | None = None) -> str:
    cfg = config or default_config()
    data = cfg.to_dict()
    data["profiles"] = default_profiles()
    return "\n".join(
        [
            "# Symphony DBCLI Workflow",
            "",
            "This file controls how symphony-dbcli dispatches GitHub Issues to workers.",
            "Edit the TOML block while the service is running; valid changes are recorded",
            "in SQLite and applied to new workers.",
            "Use --profile or SYMPHONY_PROFILE to select local/prod runtime defaults.",
            "",
            "```toml",
            render_toml(data).rstrip(),
            "```",
            "",
            "## Worker Instructions",
            "",
            cfg.instructions.strip(),
            "",
        ]
    )


def render_toml(data: ConfigTable) -> str:
    lines: list[str] = []
    _render_toml_sections(data, prefix="", lines=lines)
    return "\n".join(lines)


def prompt_for_config(
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> WorkflowConfig:
    defaults = default_config()

    def ask(prompt: str, default: str) -> str:
        answer = input_func(f"{prompt} [{default}]: ").strip()
        return answer or default

    repos = ask("GitHub repos, comma separated", ", ".join(defaults.github.repos))
    max_global = int(ask("Maximum concurrent workers", str(defaults.workers.max_global)))
    max_per_repo = int(ask("Maximum concurrent workers per repo", str(defaults.workers.max_per_repo)))
    worktree_root = ask("Worktree root", defaults.workspace.root)
    bare_root = ask("Shared repo root", defaults.workspace.bare_repos_root)
    db_path = ask("SQLite database path", defaults.database.path)
    dashboard_host = ask("Dashboard host", defaults.dashboard.host)
    dashboard_port = int(ask("Dashboard port", str(defaults.dashboard.port)))
    dry_run = _parse_bool(ask("Run in dry-run mode by default", "yes"))
    print_func("Using default Symphony label mapping. Edit WORKFLOW.md to customize it.")

    return WorkflowConfig(
        profile=defaults.profile,
        tracker=defaults.tracker,
        github=GitHubConfig(repos=[repo.strip() for repo in repos.split(",") if repo.strip()]),
        labels=defaults.labels,
        workspace=WorkspaceConfig(root=worktree_root, bare_repos_root=bare_root),
        workers=WorkerConfig(max_global=max_global, max_per_repo=max_per_repo),
        dashboard=DashboardConfig(host=dashboard_host, port=dashboard_port),
        database=DatabaseConfig(path=db_path),
        codex=defaults.codex,
        policy=PolicyConfig(dry_run=dry_run),
        instructions=DEFAULT_INSTRUCTIONS,
    )


def write_workflow(path: str | Path, config: WorkflowConfig, force: bool = False) -> Path:
    destination = Path(path)
    if destination.exists() and not force:
        raise WorkflowError(f"{destination} already exists; pass --force to overwrite it.")
    destination.write_text(render_workflow(config), encoding="utf-8")
    return destination


def default_profiles() -> ConfigTable:
    return {
        "local": {
            "database": {"path": ".symphony/symphony.db"},
            "workspace": {
                "root": ".symphony/worktrees",
                "bare_repos_root": ".symphony/repos",
            },
            "dashboard": {"host": "127.0.0.1"},
        },
        "prod": {
            "database": {"path": "/srv/symphony/symphony.db"},
            "workspace": {
                "root": "/srv/symphony/worktrees",
                "bare_repos_root": "/srv/symphony/repos",
            },
            "dashboard": {"host": "0.0.0.0"},
        },
    }


def _section(data: ConfigTable, name: str) -> ConfigTable:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise WorkflowError(f"[{name}] must be a TOML table.")
    return value


def _policy_config(data: ConfigTable) -> PolicyConfig:
    section = _section(data, "policy")
    disabled_side_effect_keys = ("post_research_answers", "open_pull_requests")
    enabled_keys = [key for key in disabled_side_effect_keys if section.get(key, False) is True]
    if enabled_keys:
        names = ", ".join(f"policy.{key}" for key in enabled_keys)
        raise WorkflowError(
            f"{names} are no longer configurable; remove them or set them to false. "
            "Use policy.dry_run as the only workflow side-effect switch."
        )
    invalid_keys = [key for key in disabled_side_effect_keys if key in section and section[key] is not False]
    if invalid_keys:
        names = ", ".join(f"policy.{key}" for key in invalid_keys)
        raise WorkflowError(f"{names} must be false when present.")
    dry_run = section.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise WorkflowError("policy.dry_run must be true or false.")
    return PolicyConfig(dry_run=dry_run)


def _selected_profile(data: ConfigTable, requested_profile: str | None) -> str:
    if requested_profile:
        return requested_profile
    env_profile = os.environ.get("SYMPHONY_PROFILE")
    if env_profile:
        return env_profile
    profile_section = _section(data, "profile")
    active = profile_section.get("active", "local")
    if not isinstance(active, str):
        raise WorkflowError("profile.active must be a string.")
    return active


def _apply_profile(data: ConfigTable, active_profile: str) -> ConfigTable:
    merged = _base_config_table(data)
    profiles = _section(data, "profiles") if "profiles" in data else default_profiles()
    if active_profile not in profiles:
        raise WorkflowError(f"Profile '{active_profile}' is not defined in [profiles].")
    profile_overrides = profiles[active_profile]
    if not isinstance(profile_overrides, dict):
        raise WorkflowError(f"profiles.{active_profile} must be a TOML table.")
    _merge_table(merged, profile_overrides)
    return merged


def _base_config_table(data: ConfigTable) -> ConfigTable:
    return {
        key: _copy_table(value)
        for key, value in data.items()
        if key not in {"profiles"} and isinstance(value, dict)
    }


def _merge_table(target: ConfigTable, overrides: ConfigTable) -> None:
    for key, value in overrides.items():
        if isinstance(value, dict):
            existing = target.get(key)
            if isinstance(existing, dict):
                _merge_table(existing, value)
            else:
                target[key] = _copy_table(value)
        else:
            target[key] = value


def _copy_table(value: ConfigTable) -> ConfigTable:
    copied: ConfigTable = {}
    for key, nested in value.items():
        copied[key] = _copy_table(nested) if isinstance(nested, dict) else nested
    return copied


def _render_toml_sections(data: ConfigTable, *, prefix: str, lines: list[str]) -> None:
    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        section_name = f"{prefix}.{section}" if prefix else section
        scalar_items = {key: value for key, value in values.items() if not isinstance(value, dict)}
        nested_items = {key: value for key, value in values.items() if isinstance(value, dict)}
        if scalar_items:
            lines.append(f"[{section_name}]")
            for key, value in scalar_items.items():
                lines.append(f"{key} = {_toml_value(value)}")
            lines.append("")
        _render_toml_sections(nested_items, prefix=section_name, lines=lines)


def _instructions_from_markdown(content: str) -> str:
    marker = "## Worker Instructions"
    if marker not in content:
        return DEFAULT_INSTRUCTIONS
    return content.split(marker, 1)[1].strip()


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"y", "yes", "true", "1", "on"}:
        return True
    if normalized in {"n", "no", "false", "0", "off"}:
        return False
    raise WorkflowError(f"Expected yes/no, got '{value}'.")
