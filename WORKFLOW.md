# Symphony DBCLI Workflow

This file controls how symphony-dbcli dispatches GitHub Issues to workers.
Edit the TOML block while the service is running; valid changes are recorded
in SQLite and applied to new workers.
Use --profile or SYMPHONY_PROFILE to select local/prod runtime defaults.

```toml
[profile]
active = "local"

[tracker]
kind = "github"

[github]
repos = ["dbcli/pgcli", "dbcli/mycli", "dbcli/litecli"]
auth_strategy = "auto"
token_env = "SYMPHONY_GITHUB_TOKEN"
fallback_token_env = "GH_TOKEN"
app_id_env = "SYMPHONY_GITHUB_APP_ID"
installation_id_env = "SYMPHONY_GITHUB_INSTALLATION_ID"
private_key_env = "SYMPHONY_GITHUB_PRIVATE_KEY"
private_key_path_env = "SYMPHONY_GITHUB_PRIVATE_KEY_PATH"
webhook_secret_env = "SYMPHONY_GITHUB_WEBHOOK_SECRET"
api_base_url = "https://api.github.com"

[labels]
todo = "symphony:todo"
working = "symphony:working"
review = "symphony:review"
blocked = "symphony:blocked"
done = "symphony:done"
type_code = "symphony:type:code"
type_research = "symphony:type:research"
priority_high = "symphony:priority:high"
priority_low = "symphony:priority:low"

[workspace]
strategy = "worktree"
root = ".symphony/worktrees"
bare_repos_root = ".symphony/repos"
retention_days = 14

[workers]
max_global = 3
max_per_repo = 2
default_task_type = "research"
poll_interval_seconds = 60

[dashboard]
host = "127.0.0.1"
port = 8765

[database]
path = ".symphony/symphony.db"

[codex]
command = "codex"
transport = "app-server"
app_server_listen = "stdio://"
model = ""
approval_policy = "never"
sandbox = "workspace-write"

[policy]
dry_run = false

[profiles.local.database]
path = ".symphony/symphony.db"

[profiles.local.workspace]
root = ".symphony/worktrees"
bare_repos_root = ".symphony/repos"

[profiles.local.dashboard]
host = "127.0.0.1"

[profiles.prod.database]
path = "/srv/symphony/symphony.db"

[profiles.prod.workspace]
root = "/srv/symphony/worktrees"
bare_repos_root = "/srv/symphony/repos"

[profiles.prod.dashboard]
host = "0.0.0.0"
```

## Worker Instructions

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
