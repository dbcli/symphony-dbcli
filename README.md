# symphony-dbcli

Python implementation of a Symphony-style worker orchestrator for the DBCLI
projects: `dbcli/pgcli`, `dbcli/mycli`, and `dbcli/litecli`.

The project is intentionally lightweight for the first implementation:

- `uv` and `pyproject.toml` for packaging.
- `WORKFLOW.md` with a fenced TOML configuration block.
- SQLite for durable state, workflow version history, and worker metrics.
- GitHub Issues as the tracker.
- Git worktrees for parallel per-issue coding workers.
- A FastAPI + Jinja + HTMX dashboard for status and operational questions.

## Quick Start

```bash
uv run symphony-dbcli init-workflow
uv run symphony-dbcli init-db
uv run symphony-dbcli status
uv run symphony-dbcli serve
```

The default workflow is safe for local development. GitHub writes require
credentials before workers can label issues. Workers save research answers and
code summaries as local review drafts instead of posting comments or opening
pull requests automatically.
`serve-web` is available for dashboard-only debugging without starting workers.
Normal local iteration should use `serve`.

## Worker Lifecycle

`serve` runs the FastAPI dashboard and the full local orchestration loop:

1. Acquire a SQLite-backed runtime leader lock so only one process dispatches workers.
2. Reload and record `WORKFLOW.md` when it changes.
3. Sync configured sources into the SQLite-backed board.
4. Advance ready workflow instances and claim queued work items.
5. Spawn worker subprocesses automatically.
6. Record worker ids, PIDs, heartbeats, deadlines, attempts, turns, errors, and outcomes in SQLite.
7. Mark crashed or timed-out workers as failed and queue a retry when `workers.retry_limit` allows it.

Worker lifecycle settings live in `WORKFLOW.md` under `[workers]`, including
`poll_interval_seconds`, `heartbeat_interval_seconds`,
`heartbeat_timeout_seconds`, `max_runtime_seconds`, `retry_limit`, and
`shutdown_grace_seconds`.

The pre-alpha runtime is designed for one FastAPI/Uvicorn process locally. A
SQLite leader lock prevents duplicate worker dispatch if another process starts,
but production multi-worker deployment still needs more soak testing before it
should be treated as hardened.

## Reviewing Results

Completed worker attempts are stored in SQLite with their final markdown result,
regardless of `policy.dry_run`. Open the dashboard, select an attempt in review,
and read the `Worker Result` section. Draft GitHub replies are shown separately
so they can be edited before posting to GitHub.

Code attempts in review can open a draft pull request from the dashboard. The PR
is created from the attempt worktree, records the PR URL in SQLite, links back to
the GitHub issue, and includes a concise summary plus verification notes from
the worker result. Once a recorded PR is merged, the orchestration loop checks
the PR state and removes the associated clean worktree to avoid filling the
disk. Dirty worktrees are left in place and the cleanup error is recorded for
review.

Research results can be promoted into code work. From a research attempt page,
use `Create Code Follow-up` to queue a linked code attempt. The code worker
receives the original issue plus the stored research result in its prompt, and
the relationship is recorded in SQLite. The same operation is available from the
CLI:

```bash
uv run symphony-dbcli attempt create-code-follow-up --attempt-id 2
```

Merged-PR worktree cleanup also has a manual command:

```bash
uv run symphony-dbcli worktree cleanup-merged-prs
```

## Profiles

`WORKFLOW.md` includes explicit runtime profiles. The default `local` profile
keeps SQLite, shared repos, and worktrees under `.symphony/` so local iteration
does not need privileged filesystem paths. The `prod` profile uses `/srv/symphony`
paths and binds the dashboard to `0.0.0.0` for hosted deployment.

Profile precedence is:

1. `--profile`, for example `uv run symphony-dbcli --profile prod serve`
2. `SYMPHONY_PROFILE`
3. `[profile].active` in `WORKFLOW.md`
4. `local`

## GitHub App Setup

Generate a local manifest form:

```bash
uv run symphony-dbcli github-app manifest --account amjith
```

Open `.symphony/github-app-manifest.html` in a browser and submit the form.
GitHub will redirect to `http://127.0.0.1:8765/github-app/callback` with a
temporary code. If the dashboard is not running, copy the `code` from the
browser address bar.

Exchange the code within one hour:

```bash
uv run symphony-dbcli github-app convert --code CODE
```

This writes `.symphony/github-app.env` and `.symphony/github-app.private-key.pem`
with `0600` permissions. Local CLI commands load `.symphony/github-app.env`
automatically without overriding existing environment variables.

Install the app on the target DBCLI repositories, then list installations:

```bash
uv run symphony-dbcli github-app installations
```

Set `SYMPHONY_GITHUB_INSTALLATION_ID` in the env file to the installation id for
the account that owns the DBCLI repositories.

## Development

```bash
uv sync --dev
uv run pre-commit install
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

## End-to-End Fixture

Use the disposable fixture repo to exercise the GitHub-backed orchestration loop
without touching DBCLI repos:

```bash
uv run symphony-dbcli e2e run-fixture
uv run symphony-dbcli e2e run-fixture --scenario research_to_code_follow_up
```

The harness targets `amjith/symphony-dbcli-e2e-fixture`, creates a labeled issue,
polls and claims it, allocates a worktree, runs a deterministic fake Codex
worker, stores the result in SQLite, and opens a draft PR for code-path
scenarios unless `--no-create-pr` is passed. Scenarios include
`code_happy_path`, `research_answer_review`, `research_to_code_follow_up`,
`pr_review_comments`, and `ci_failure_fix`.

Generated workflow files, databases, fake worker binaries, and worktrees live
under `.symphony/e2e/`. The fixture expects `gh` to be authenticated with repo
access and uses local SSH git auth for the fixture branch push. To run the live
pytest fixture, opt in explicitly:

```bash
SYMPHONY_RUN_GITHUB_E2E=1 uv run pytest tests/test_e2e.py
```
