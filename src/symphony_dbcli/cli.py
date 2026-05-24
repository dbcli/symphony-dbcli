from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

from .ask import answer_question
from .config import (
    WorkflowConfig,
    WorkflowError,
    default_config,
    default_config_for_profile,
    load_workflow,
    prompt_for_config,
    validate_config,
    write_workflow,
)
from .dashboard import DashboardState, serve_dashboard
from .env import load_local_env, parse_env_file
from .github import GitHubClient
from .github_app import default_manifest, write_manifest_form
from .orchestrator import Orchestrator, WorkflowWatcher, load_and_record_workflow
from .store import Store
from .supervisor import WorkerSupervisor
from .worktree import WorktreeManager


def main(argv: list[str] | None = None) -> int:
    load_local_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    command = cast(Callable[[argparse.Namespace], int], args.func)
    try:
        return command(args)
    except WorkflowError as exc:
        print(f"workflow error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="symphony-dbcli")
    parser.add_argument("--workflow", default="WORKFLOW.md", help="Path to WORKFLOW.md")
    parser.add_argument(
        "--profile",
        default=None,
        help="Workflow profile to use; overrides SYMPHONY_PROFILE and [profile].active",
    )
    subcommands = parser.add_subparsers(required=True)

    init_workflow = subcommands.add_parser("init-workflow", help="Interactively create WORKFLOW.md")
    init_workflow.add_argument("--force", action="store_true", help="Overwrite an existing workflow file")
    init_workflow.add_argument("--defaults", action="store_true", help="Write defaults without prompting")
    init_workflow.set_defaults(func=cmd_init_workflow)

    init_db = subcommands.add_parser("init-db", help="Create or migrate the SQLite database")
    init_db.set_defaults(func=cmd_init_db)

    workflow = subcommands.add_parser("workflow", help="Workflow tools")
    workflow_sub = workflow.add_subparsers(required=True)
    validate = workflow_sub.add_parser("validate", help="Validate WORKFLOW.md")
    validate.set_defaults(func=cmd_workflow_validate)
    history = workflow_sub.add_parser("history", help="Show recorded workflow versions")
    history.add_argument("--limit", type=int, default=20)
    history.set_defaults(func=cmd_workflow_history)

    status = subcommands.add_parser("status", help="Show orchestrator status")
    status.set_defaults(func=cmd_status)

    attempt = subcommands.add_parser("attempt", help="Attempt review and follow-up commands")
    attempt_sub = attempt.add_subparsers(required=True)
    follow_up = attempt_sub.add_parser(
        "create-code-follow-up", help="Queue a code task from a research result"
    )
    follow_up.add_argument("--attempt-id", required=True, type=int)
    follow_up.set_defaults(func=cmd_attempt_create_code_follow_up)

    ask = subcommands.add_parser("ask", help="Ask about workers, issues, timing, turns, or errors")
    ask.add_argument("question", nargs="+")
    ask.set_defaults(func=cmd_ask)

    poll_once = subcommands.add_parser("poll-once", help="Sync dispatchable GitHub issues into SQLite")
    poll_once.set_defaults(func=cmd_poll_once)

    serve = subcommands.add_parser("serve", help="Run dashboard and optional polling loop")
    serve.add_argument("--no-poll", action="store_true", help="Only run the dashboard")
    serve.add_argument("--dispatch", action="store_true", help="Claim eligible issues and start workers")
    serve.set_defaults(func=cmd_serve)

    worker = subcommands.add_parser("worker", help="Worker commands")
    worker_sub = worker.add_subparsers(required=True)
    run = worker_sub.add_parser("run", help="Run one issue worker")
    run.add_argument("--repo", required=True)
    run.add_argument("--issue", required=True, type=int)
    run.add_argument("--task-type", choices=["code", "research"])
    run.set_defaults(func=cmd_worker_run)
    run_attempt = worker_sub.add_parser("run-attempt", help="Run an already queued attempt")
    run_attempt.add_argument("--attempt-id", required=True, type=int)
    run_attempt.set_defaults(func=cmd_worker_run_attempt)

    worktree = subcommands.add_parser("worktree", help="Worktree commands")
    worktree_sub = worktree.add_subparsers(required=True)
    cleanup = worktree_sub.add_parser("cleanup", help="Prune stale git worktree metadata")
    cleanup.set_defaults(func=cmd_worktree_cleanup)

    github_app = subcommands.add_parser("github-app", help="GitHub App setup commands")
    github_app_sub = github_app.add_subparsers(required=True)
    manifest = github_app_sub.add_parser("manifest", help="Write a GitHub App manifest HTML form")
    manifest.add_argument("--account", default="amjith", help="User or organization that will own the app")
    manifest.add_argument("--owner-type", choices=["user", "org"], default="user")
    manifest.add_argument("--name", default="symphony-dbcli")
    manifest.add_argument("--homepage-url", default="https://github.com/amjith/symphony-dbcli")
    manifest.add_argument("--redirect-url", default="http://127.0.0.1:8765/github-app/callback")
    manifest.add_argument("--webhook-url", default="https://github.com/amjith/symphony-dbcli")
    manifest.add_argument("--out", default=".symphony/github-app-manifest.html")
    manifest.set_defaults(func=cmd_github_app_manifest)

    convert = github_app_sub.add_parser("convert", help="Exchange a manifest code for app credentials")
    convert.add_argument("--code", required=True)
    convert.add_argument("--env-out", default=".symphony/github-app.env")
    convert.add_argument("--private-key-out", default=".symphony/github-app.private-key.pem")
    convert.set_defaults(func=cmd_github_app_convert)

    installations = github_app_sub.add_parser(
        "installations", help="List installations for the configured app"
    )
    installations.set_defaults(func=cmd_github_app_installations)

    return parser


def cmd_init_workflow(args: argparse.Namespace) -> int:
    config = default_config() if args.defaults else prompt_for_config()
    validate_config(config)
    path = write_workflow(args.workflow, config, force=args.force)
    print(f"Wrote {path}")
    return 0


def cmd_init_db(args: argparse.Namespace) -> int:
    profile = _runtime_profile(args)
    config = _load_config_if_exists(args.workflow, profile=profile)
    store = Store(config.database.path)
    store.init()
    workflow_path = Path(args.workflow)
    if workflow_path.exists():
        load_and_record_workflow(store, workflow_path, profile=profile)
    print(f"Initialized {config.database.path}")
    return 0


def cmd_workflow_validate(args: argparse.Namespace) -> int:
    config = load_workflow(args.workflow, profile=_runtime_profile(args))
    validate_config(config)
    print(f"{args.workflow} is valid for profile={config.profile.active}")
    return 0


def cmd_workflow_history(args: argparse.Namespace) -> int:
    config = _load_config_if_exists(args.workflow, profile=_runtime_profile(args))
    store = Store(config.database.path)
    store.init()
    for row in store.workflow_history(args.limit):
        error = f" error={row['error']}" if row["error"] else ""
        print(f"{row['id']:>4} {row['created_at']} {row['status']} {row['content_hash'][:12]}{error}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = _load_config_if_exists(args.workflow, profile=_runtime_profile(args))
    store = Store(config.database.path)
    store.init()
    summary = store.dashboard_summary()
    print(f"profile={config.profile.active} database={config.database.path}")
    print(
        f"issues={summary['issue_count']} running={summary['running_attempts']} queued={summary['queued_attempts']}"
    )
    print(f"turns={summary['turn_count']} errors={summary['error_count']}")
    for row in summary["attempts"][:10]:
        print(
            f"attempt={row['id']} {row['repo']}#{row['issue_number']} "
            f"status={row['status']} phase={row['current_phase'] or '-'} "
            f"turns={row['turn_count']} errors={row['error_count']}"
        )
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    config = _load_config_if_exists(args.workflow, profile=_runtime_profile(args))
    store = Store(config.database.path)
    store.init()
    print(answer_question(store, " ".join(args.question)))
    return 0


def cmd_attempt_create_code_follow_up(args: argparse.Namespace) -> int:
    _config, version_id, store = _load_config_store_and_record(
        args.workflow,
        profile=_runtime_profile(args),
    )
    attempt_id = store.create_code_follow_up_attempt(args.attempt_id, version_id)
    print(f"Queued code follow-up attempt {attempt_id}")
    return 0


def cmd_poll_once(args: argparse.Namespace) -> int:
    config, version_id, store = _load_config_store_and_record(
        args.workflow,
        profile=_runtime_profile(args),
    )
    synced = Orchestrator(config, store, version_id).poll_once()
    print(f"Synced {synced} issues")
    return 0


def cmd_worker_run(args: argparse.Namespace) -> int:
    config, version_id, store = _load_config_store_and_record(
        args.workflow,
        profile=_runtime_profile(args),
    )
    attempt_id = Orchestrator(config, store, version_id).run_issue(
        args.repo,
        args.issue,
        task_type=args.task_type,
    )
    print(f"Recorded attempt {attempt_id}")
    return 0


def cmd_worker_run_attempt(args: argparse.Namespace) -> int:
    config, version_id, store = _load_config_store_and_record(
        args.workflow,
        profile=_runtime_profile(args),
    )
    attempt_id = Orchestrator(config, store, version_id).run_attempt(args.attempt_id)
    print(f"Recorded attempt {attempt_id}")
    return 0


def cmd_worktree_cleanup(args: argparse.Namespace) -> int:
    config = load_workflow(args.workflow, profile=_runtime_profile(args))
    print(WorktreeManager(config.workspace).cleanup_prunable())
    return 0


def cmd_github_app_manifest(args: argparse.Namespace) -> int:
    manifest = default_manifest(
        account=args.account,
        owner_type=args.owner_type,
        name=args.name,
        homepage_url=args.homepage_url,
        redirect_url=args.redirect_url,
        webhook_url=args.webhook_url,
    )
    path = write_manifest_form(manifest, args.out)
    print(f"Wrote {path}")
    print("Open this file in a browser, submit the form, then run:")
    print("  uv run symphony-dbcli github-app convert --code CODE")
    return 0


def cmd_github_app_convert(args: argparse.Namespace) -> int:
    config = _load_config_if_exists(args.workflow, profile=_runtime_profile(args))
    conversion = GitHubClient(config.github).convert_manifest_code(args.code)
    private_key_path = Path(args.private_key_out)
    private_key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key_path.write_text(conversion.pem, encoding="utf-8")
    private_key_path.chmod(0o600)

    env_path = Path(args.env_out)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        "\n".join(
            [
                f"{config.github.app_id_env}={conversion.app_id}",
                f"{config.github.installation_id_env}=",
                f"{config.github.private_key_path_env}={private_key_path.resolve()}",
                f"{config.github.webhook_secret_env}={conversion.webhook_secret}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    env_path.chmod(0o600)

    print(f"Wrote {private_key_path}")
    print(f"Wrote {env_path}")
    print(f"GitHub App: {conversion.html_url or conversion.slug}")
    print("Install the app on the DBCLI repos, source the env file, then run:")
    print("  uv run symphony-dbcli github-app installations")
    return 0


def cmd_github_app_installations(args: argparse.Namespace) -> int:
    config = _load_config_if_exists(args.workflow, profile=_runtime_profile(args))
    for key, value in parse_env_file(".symphony/github-app.env").items():
        os.environ.setdefault(key, value)
    installations = GitHubClient(config.github).list_app_installations()
    if not installations:
        print("No installations found for this app.")
        return 0
    for installation in installations:
        print(f"{installation.id} {installation.account_login} ({installation.account_type})")
    print(f"Set {config.github.installation_id_env} to the installation id for the target account.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    config, _, store = _load_config_store_and_record(
        args.workflow,
        profile=_runtime_profile(args),
    )
    dashboard_state = DashboardState(config)
    if not args.no_poll:
        thread = threading.Thread(target=_poll_loop, args=(args, store, dashboard_state), daemon=True)
        thread.start()
    serve_dashboard(store, config.dashboard.host, config.dashboard.port, state=dashboard_state)
    return 0


def _poll_loop(args: argparse.Namespace, store: Store, dashboard_state: DashboardState) -> None:
    watcher = WorkflowWatcher(store, args.workflow, profile=_runtime_profile(args))
    supervisor = WorkerSupervisor(store, workflow_path=args.workflow, profile=_runtime_profile(args))
    interval = 60
    while True:
        try:
            config, version_id, _changed = watcher.reload_if_changed()
            dashboard_state.update_config(config)
            interval = config.workers.poll_interval_seconds
            orchestrator = Orchestrator(config, store, version_id)
            supervisor.reconcile(config, version_id)
            orchestrator.poll_once()
            if args.dispatch:
                orchestrator.claim_available()
                supervisor.start_queued(config)
        except Exception as exc:  # Keep the dashboard alive.
            print(f"poll loop error: {exc}", file=sys.stderr)
        time.sleep(interval)


def _load_config_store_and_record(
    workflow_path: str,
    profile: str | None,
) -> tuple[WorkflowConfig, int, Store]:
    config = load_workflow(workflow_path, profile=profile)
    store = Store(config.database.path)
    store.init()
    config, version_id = load_and_record_workflow(store, workflow_path, profile=profile)
    return config, version_id, store


def _load_config_if_exists(workflow_path: str, profile: str | None) -> WorkflowConfig:
    path = Path(workflow_path)
    if path.exists():
        return load_workflow(path, profile=profile)
    return default_config_for_profile(profile=profile)


def _runtime_profile(args: argparse.Namespace) -> str | None:
    return cast(str | None, args.profile)


if __name__ == "__main__":
    raise SystemExit(main())
