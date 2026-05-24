# symphony-dbcli

Python implementation of a Symphony-style worker orchestrator for the DBCLI
projects: `dbcli/pgcli`, `dbcli/mycli`, and `dbcli/litecli`.

The project is intentionally lightweight for the first implementation:

- `uv` and `pyproject.toml` for packaging.
- `WORKFLOW.md` with a fenced TOML configuration block.
- SQLite for durable state, workflow version history, and worker metrics.
- GitHub Issues as the tracker.
- Git worktrees for parallel per-issue coding workers.
- A Jinja-rendered dashboard for status and operational questions.

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
Use the dashboard toggle named `Start queued work automatically` to control
whether queued attempts are started by worker subprocesses.

## Worker Lifecycle

`serve` runs the full local orchestration loop:

1. Poll GitHub Issues with the `symphony:todo` label.
2. Claim eligible issues into durable SQLite attempts.
3. Spawn worker subprocesses when `Start queued work automatically` is on.
4. Record worker ids, PIDs, heartbeats, deadlines, attempts, turns, errors, and outcomes in SQLite.
5. Mark crashed or timed-out workers as failed and queue a retry when `workers.retry_limit` allows it.

Worker lifecycle settings live in `WORKFLOW.md` under `[workers]`, including
`poll_interval_seconds`, `heartbeat_interval_seconds`,
`heartbeat_timeout_seconds`, `max_runtime_seconds`, `retry_limit`, and
`shutdown_grace_seconds`.

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
