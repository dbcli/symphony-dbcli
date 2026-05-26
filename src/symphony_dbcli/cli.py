from __future__ import annotations

import argparse
import os
import sys
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
from .db import create_db_engine
from .e2e import DEFAULT_FIXTURE_REPO, E2EFixtureConfig, fixture_scenarios, run_fixture
from .env import load_local_env, parse_env_file
from .github import GitHubClient
from .github_app import default_manifest, write_manifest_form
from .models import create_model_tables
from .orchestrator import Orchestrator, load_and_record_workflow
from .store import Store
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

    serve = subcommands.add_parser("serve", help="Run the FastAPI dashboard and orchestration runtime")
    _add_serve_reload_flags(serve)
    serve.set_defaults(func=cmd_serve)

    serve_web = subcommands.add_parser("serve-web", help="Run the FastAPI dashboard without workers")
    _add_serve_reload_flags(serve_web)
    serve_web.set_defaults(func=cmd_serve_web)

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
    cleanup_merged_prs = worktree_sub.add_parser(
        "cleanup-merged-prs",
        help="Remove attempt worktrees after their recorded pull request is merged",
    )
    cleanup_merged_prs.add_argument(
        "--retry-errors",
        action="store_true",
        help="Retry pull request worktree cleanups that previously failed",
    )
    cleanup_merged_prs.set_defaults(func=cmd_worktree_cleanup_merged_prs)

    e2e = subcommands.add_parser("e2e", help="GitHub-backed end-to-end fixture tools")
    e2e_sub = e2e.add_subparsers(required=True)
    run_fixture_parser = e2e_sub.add_parser(
        "run-fixture",
        help="Create a fixture issue, dispatch it, run the worker, and optionally open a draft PR",
    )
    run_fixture_parser.add_argument("--repo", default=DEFAULT_FIXTURE_REPO)
    run_fixture_parser.add_argument("--root", default=".symphony/e2e")
    run_fixture_parser.add_argument("--scenario", choices=fixture_scenarios(), default="code_happy_path")
    run_fixture_parser.add_argument("--task-type", choices=["code", "research"], default="")
    run_fixture_parser.add_argument(
        "--no-create-pr",
        action="store_true",
        help="Stop after the worker reaches review instead of creating a draft PR",
    )
    run_fixture_parser.add_argument(
        "--keep-existing-todo",
        action="store_true",
        help="Do not remove symphony:todo from existing open fixture issues before creating a new one",
    )
    run_fixture_parser.set_defaults(func=cmd_e2e_run_fixture)

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


def _add_serve_reload_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--reload",
        dest="reload",
        action="store_true",
        default=None,
        help="Reload the FastAPI process when Python, template, static, or workflow files change.",
    )
    parser.add_argument(
        "--no-reload",
        dest="reload",
        action="store_false",
        help="Disable Uvicorn file watching.",
    )


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
    create_model_tables(create_db_engine(config.database.path))
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
        "workspace="
        f"{config.workspace.strategy} root={config.workspace.root} "
        f"bare_repos={config.workspace.bare_repos_root} "
        f"branch_prefix={config.workspace.branch_prefix} "
        f"base_branch={config.workspace.base_branch or 'default'} "
        f"retention_days={config.workspace.retention_days}"
    )
    print("auto_dispatch=always_on")
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


def cmd_worktree_cleanup_merged_prs(args: argparse.Namespace) -> int:
    config, version_id, store = _load_config_store_and_record(
        args.workflow,
        profile=_runtime_profile(args),
    )
    summary = Orchestrator(config, store, version_id).cleanup_merged_pull_request_worktrees(
        retry_errors=bool(args.retry_errors)
    )
    print(
        " ".join(
            [
                f"scanned={summary.scanned}",
                f"merged={summary.merged}",
                f"cleaned={summary.cleaned}",
                f"skipped={summary.skipped}",
                f"errors={summary.errors}",
            ]
        )
    )
    return 0


def cmd_e2e_run_fixture(args: argparse.Namespace) -> int:
    result = run_fixture(
        E2EFixtureConfig(
            repo=str(args.repo),
            root=Path(str(args.root)),
            task_type=str(args.task_type),
            create_pr=not bool(args.no_create_pr),
            reset_open_todo=not bool(args.keep_existing_todo),
            scenario=str(args.scenario),
        )
    )
    print(f"issue={result.issue_url}")
    print(f"attempt={result.attempt_id}")
    print(f"workflow={result.workflow_path}")
    print(f"database={result.database_path}")
    print(f"worktree={result.worktree_path}")
    if result.pull_request_url:
        print(f"pull_request={result.pull_request_url}")
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
    return _run_fastapi(args, run_runtime=True)


def cmd_serve_web(args: argparse.Namespace) -> int:
    return _run_fastapi(args, run_runtime=False)


def _run_fastapi(args: argparse.Namespace, *, run_runtime: bool) -> int:
    import uvicorn

    from .web.app import create_app

    config, _, store = _load_config_store_and_record(
        args.workflow,
        profile=_runtime_profile(args),
    )
    reload_enabled = bool(args.reload) if args.reload is not None else config.profile.active == "local"
    if reload_enabled:
        _set_fastapi_factory_env(args.workflow, config.profile.active, run_runtime=run_runtime)
        uvicorn.run(
            "symphony_dbcli.web.app:create_app_from_env",
            factory=True,
            host=config.dashboard.host,
            port=config.dashboard.port,
            reload=True,
            reload_dirs=_reload_dirs(args.workflow),
            reload_includes=_reload_includes(args.workflow),
        )
    else:
        app = create_app(config, store, workflow_path=args.workflow, run_runtime=run_runtime)
        uvicorn.run(app, host=config.dashboard.host, port=config.dashboard.port)
    return 0


def _set_fastapi_factory_env(workflow_path: str, profile: str, *, run_runtime: bool) -> None:
    os.environ["SYMPHONY_WORKFLOW"] = str(Path(workflow_path))
    os.environ["SYMPHONY_PROFILE"] = profile
    os.environ["SYMPHONY_RUN_RUNTIME"] = "1" if run_runtime else "0"


def _reload_dirs(workflow_path: str) -> list[str]:
    dirs = {str(Path(workflow_path).resolve().parent)}
    src_dir = Path("src")
    if src_dir.exists():
        dirs.add(str(src_dir.resolve()))
    return sorted(dirs)


def _reload_includes(workflow_path: str) -> list[str]:
    includes = ["*.py", "*.html", "*.css", "*.js"]
    workflow_name = Path(workflow_path).name
    if workflow_name not in includes:
        includes.append(workflow_name)
    return includes


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
