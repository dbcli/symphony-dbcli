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
credentials before workers can comment, label issues, or open pull requests.

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
