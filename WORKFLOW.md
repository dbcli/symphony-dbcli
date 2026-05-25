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
branch_prefix = "symphony"
base_branch = ""

[workers]
max_global = 3
max_per_repo = 2
default_task_type = "research"
poll_interval_seconds = 60
heartbeat_interval_seconds = 15
heartbeat_timeout_seconds = 120
max_runtime_seconds = 3600
retry_limit = 1
shutdown_grace_seconds = 10

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

[workflow]
initial_state = "todo"
terminal_states = ["done", "failed", "blocked"]

[workflow.states.todo]
description = "Issue is eligible for Symphony dispatch."
terminal = false
gate = ""

[workflow.states.claimed]
description = "Issue has been claimed and labeled as working."
terminal = false
gate = ""

[workflow.states.workspace_ready]
description = "An isolated workspace has been prepared."
terminal = false
gate = ""

[workflow.states.setup_complete]
description = "Configured setup steps have completed."
terminal = false
gate = ""

[workflow.states.worker_complete]
description = "Codex has produced a durable worker result."
terminal = false
gate = ""

[workflow.states.review]
description = "Human review is required before GitHub side effects."
terminal = false
gate = "review"

[workflow.states.pr_ready]
description = "A draft pull request exists and is waiting for merge."
terminal = false
gate = ""

[workflow.states.done]
description = "Workflow completed successfully."
terminal = true
gate = ""

[workflow.states.failed]
description = "Workflow failed and requires inspection."
terminal = true
gate = ""

[workflow.states.blocked]
description = "Workflow is blocked by a human decision."
terminal = true
gate = ""

[workflow.transitions.claim_issue]
from_state = "todo"
to_state = "claimed"
action = "github.apply_labels"
trigger = "automatic"
description = "Move a dispatchable issue into the working state."
condition = ""
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Keep label changes minimal and explainable.", "Do not alter labels unrelated to Symphony state."]

[workflow.transitions.allocate_workspace]
from_state = "claimed"
to_state = "workspace_ready"
action = "workspace.allocate"
trigger = "automatic"
description = "Create the per-attempt workspace."
condition = ""
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Prefer isolated worktrees so concurrent workers do not share a checkout.", "Use deterministic paths and branch names that are easy to inspect."]

[workflow.transitions.run_setup]
from_state = "workspace_ready"
to_state = "setup_complete"
action = "workspace.run_setup"
trigger = "automatic"
description = "Run configured setup commands before Codex starts."
condition = ""
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Run only setup commands declared in this workflow.", "Treat blocking setup failures as worker-blocking failures."]

[workflow.transitions.research_issue]
from_state = "setup_complete"
to_state = "worker_complete"
action = "codex.research_issue"
trigger = "automatic"
description = "Use Codex to draft a research or support answer."
condition = "task.type == \"research\""
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Draft a concise support answer in the user's voice.", "Keep the reply under two sentences unless the issue requires concrete steps.", "Cite specific files, commands, or issue facts when they matter."]

[workflow.transitions.fix_issue]
from_state = "setup_complete"
to_state = "worker_complete"
action = "codex.fix_issue"
trigger = "automatic"
description = "Use Codex to implement a code change."
condition = "task.type == \"code\""
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Keep the code change focused on the issue.", "Prefer narrow unit tests before broader integration tests.", "Run a review pass after implementation when the workflow asks for it."]

[workflow.transitions.request_review]
from_state = "worker_complete"
to_state = "review"
action = "github.apply_labels"
trigger = "automatic"
description = "Move completed worker output into human review."
condition = ""
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Preserve worker output for human review before external side effects.", "Avoid posting comments or opening PRs in this step."]

[workflow.transitions.post_answer]
from_state = "review"
to_state = "done"
action = "github.post_issue_comment"
trigger = "human"
description = "Post an edited research answer to GitHub."
condition = "task.type == \"research\""
gate = "review_answer"
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Let the human edit the final reply before posting.", "Keep the posted response succinct and avoid unnecessary caveats."]

[workflow.transitions.create_draft_pr]
from_state = "review"
to_state = "pr_ready"
action = "github.create_draft_pr"
trigger = "human"
description = "Create a draft pull request after human diff review."
condition = "task.type == \"code\""
gate = "review_diff"
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Let the human edit the PR title and description before creation.", "Keep the PR description clear, succinct, and linked to the GitHub issue."]

[workflow.transitions.cleanup_after_merge]
from_state = "pr_ready"
to_state = "done"
action = "workspace.cleanup_after_merge"
trigger = "automatic"
description = "Clean up the workspace after the pull request is merged."
condition = "pull_request.is_merged"
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Clean only workspaces owned by Symphony.", "Do not remove worktrees with uncommitted changes."]

[workflow.transitions.mark_blocked]
from_state = "review"
to_state = "blocked"
action = "github.apply_labels"
trigger = "human"
description = "Let a human stop progress when review cannot continue."
condition = ""
gate = "mark_blocked"
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Use this only when human review cannot continue safely.", "Leave enough context in the dashboard for a future retry."]

[preferences]
run_review_after_code_change = true
preferred_test_strategy = "unit"
require_tests_for_code_changes = true
coding_style = ["Keep changes focused on the issue.", "Prefer narrow unit tests before broader integration tests.", "Avoid unrelated refactors."]
additional_instructions = ""

[setup]
enabled = true

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
