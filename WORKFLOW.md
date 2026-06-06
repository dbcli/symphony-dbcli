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
max_per_repo = 1
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
path = "/Users/amjith/.local/state/symphony-dbcli/symphony.db"

[codex]
command = "codex"
transport = "app-server"
app_server_listen = "stdio://"
model = ""
workflow_edit_model = "gpt-5.4-mini"
workflow_edit_reasoning_effort = "low"
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

[workflow.states.associated_pr_checked]
description = "Durable issue-to-PR bookkeeping has been checked."
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

[workflow.states.pr_refreshed]
description = "Latest pull request metadata has been fetched."
terminal = false
gate = ""

[workflow.states.pr_checks_complete]
description = "CI, PR comments, and mergeability have been checked."
terminal = false
gate = ""

[workflow.states.pr_feedback_context_ready]
description = "Pull request follow-up context has been prepared for Codex."
terminal = false
gate = ""

[workflow.states.pr_follow_up_complete]
description = "Codex has addressed pull request feedback and needs human review."
terminal = false
gate = "review_pr_feedback"

[workflow.states.pr_waiting]
description = "Pull request has no immediate Symphony follow-up and is waiting for external activity."
terminal = false
gate = "check_pr_again"

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
parallel_group = ""
description = "Move a dispatchable issue into the working state."
condition = ""
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Keep label changes minimal and explainable.", "Do not alter labels unrelated to Symphony state."]

[workflow.transitions.find_issue_pull_requests]
from_state = "claimed"
to_state = "associated_pr_checked"
action = "github.find_issue_pull_requests"
trigger = "automatic"
parallel_group = ""
description = "Find pull requests durably associated with this issue."
condition = ""
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Trust only DB bookkeeping or the exact Symphony issue-link marker in the PR body.", "Ignore incidental issue mentions to avoid false positive associations."]

[workflow.transitions.find_issue_pull_requests.outputs]
has_pull_request = "artifact.pull_request.exists"
pull_request_count = "artifact.pull_request.count"
pull_requests = "artifact.pull_request.associated"
pull_request_number = "artifact.pull_request.number"
pull_request_url = "artifact.pull_request.url"
pull_request_title = "artifact.pull_request.title"
pull_request_head_ref = "artifact.pull_request.head_ref"
pull_request_head_sha = "artifact.pull_request.head_sha"
pull_request_source_ref = "artifact.pull_request.source_ref"

[workflow.transitions.allocate_workspace]
from_state = "associated_pr_checked"
to_state = "workspace_ready"
action = "workspace.allocate"
trigger = "automatic"
parallel_group = ""
description = "Create the per-attempt workspace."
condition = ""
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Prefer isolated worktrees so concurrent workers do not share a checkout.", "When an associated PR exists, resume from that PR branch.", "Use deterministic paths and branch names that are easy to inspect."]

[workflow.transitions.allocate_workspace.inputs]
branch = "artifact.pull_request.head_ref"
source_ref = "artifact.pull_request.source_ref"

[workflow.transitions.run_setup]
from_state = "workspace_ready"
to_state = "setup_complete"
action = "workspace.run_setup"
trigger = "automatic"
parallel_group = ""
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
parallel_group = ""
description = "Use Codex to draft a research or support answer."
condition = "task.type == \"research\" and not pull_request.exists"
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
parallel_group = ""
description = "Use Codex to implement a code change."
condition = "task.type == \"code\" and not pull_request.exists"
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Keep the code change focused on the issue.", "Prefer narrow unit tests before broader integration tests.", "Run a review pass after implementation when the workflow asks for it."]

[workflow.transitions.check_pr_ci]
from_state = "setup_complete"
to_state = "pr_checks_complete"
action = "github.fetch_ci_status"
trigger = "automatic"
parallel_group = "initial_pr_checks"
description = "Fetch current CI status for the associated pull request."
condition = "pull_request.exists"
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Capture both failed checks and the full check list for follow-up workers."]

[workflow.transitions.check_pr_ci.inputs]
pull_request_number = "artifact.pull_request.number"

[workflow.transitions.check_pr_ci.outputs]
failed_checks = "artifact.ci.failed_checks"
checks = "artifact.ci.checks"
state = "artifact.ci.state"
conclusion = "artifact.ci.conclusion"

[workflow.transitions.check_pr_comments]
from_state = "setup_complete"
to_state = "pr_checks_complete"
action = "github.fetch_pr_review_comments"
trigger = "automatic"
parallel_group = "initial_pr_checks"
description = "Fetch review-body and inline comments for the associated pull request."
condition = "pull_request.exists"
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Keep review-body comments and inline comments together for the worker."]

[workflow.transitions.check_pr_comments.inputs]
pull_request_number = "artifact.pull_request.number"

[workflow.transitions.check_pr_comments.outputs]
comments = "artifact.review_comments.comments"

[workflow.transitions.check_pr_mergeability]
from_state = "setup_complete"
to_state = "pr_checks_complete"
action = "github.detect_merge_conflicts"
trigger = "automatic"
parallel_group = "initial_pr_checks"
description = "Detect merge conflicts for the associated pull request."
condition = "pull_request.exists"
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Treat GitHub mergeability as a snapshot that can change after new commits."]

[workflow.transitions.check_pr_mergeability.inputs]
pull_request_number = "artifact.pull_request.number"

[workflow.transitions.check_pr_mergeability.outputs]
has_conflicts = "artifact.pull_request.has_conflicts"
mergeable = "artifact.pull_request.mergeable"
mergeable_state = "artifact.pull_request.mergeable_state"
head_sha = "artifact.pull_request.head_sha"

[workflow.transitions.fetch_ci_failure_context]
from_state = "pr_checks_complete"
to_state = "pr_feedback_context_ready"
action = "github.fetch_ci_failure_context"
trigger = "automatic"
parallel_group = ""
description = "Fetch bounded logs and annotations for failed CI checks."
condition = "ci.has_failures"
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Prefer failure excerpts, check annotations, and concise summaries over full CI logs."]

[workflow.transitions.fetch_ci_failure_context.inputs]
pull_request_number = "artifact.pull_request.number"
failed_checks = "artifact.ci.failed_checks"

[workflow.transitions.fetch_ci_failure_context.outputs]
failure_context = "artifact.ci.failure_context"
unavailable_reason = "artifact.ci.failure_context_unavailable_reason"

[workflow.transitions.skip_ci_failure_context]
from_state = "pr_checks_complete"
to_state = "pr_feedback_context_ready"
action = "workflow.noop"
trigger = "automatic"
parallel_group = ""
description = "Continue without CI failure logs when CI has no failures."
condition = "not ci.has_failures"
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Only fetch CI failure logs when failed checks are present."]

[workflow.transitions.address_pr_feedback]
from_state = "pr_feedback_context_ready"
to_state = "pr_follow_up_complete"
action = "codex.address_pr_feedback"
trigger = "automatic"
parallel_group = ""
description = "Ask Codex to address PR feedback from CI, comments, and mergeability."
condition = "pull_request.needs_follow_up"
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Address only the PR feedback captured in workflow artifacts.", "Keep the update focused and preserve the original issue intent.", "Prefer the narrowest local test that validates the change."]

[workflow.transitions.address_pr_feedback.inputs]
pull_request_number = "artifact.pull_request.number"
failed_checks = "artifact.ci.failed_checks"
failure_context = "artifact.ci.failure_context"
checks = "artifact.ci.checks"
comments = "artifact.review_comments.comments"
has_conflicts = "artifact.pull_request.has_conflicts"
mergeable_state = "artifact.pull_request.mergeable_state"

[workflow.transitions.push_pr_feedback_fix]
from_state = "pr_follow_up_complete"
to_state = "pr_waiting"
action = "github.push_pr_update"
trigger = "human"
parallel_group = ""
description = "Review and push the PR feedback fix to the existing pull request branch."
condition = ""
gate = "review_pr_feedback"
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Let the human inspect the follow-up fix before pushing it to GitHub."]

[workflow.transitions.wait_existing_pr]
from_state = "pr_feedback_context_ready"
to_state = "pr_waiting"
action = "workflow.noop"
trigger = "automatic"
parallel_group = ""
description = "Wait when the associated PR has no current Symphony follow-up."
condition = "not pull_request.needs_follow_up"
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Stop automatic work until a human asks Symphony to check the pull request again."]

[workflow.transitions.request_review]
from_state = "worker_complete"
to_state = "review"
action = "github.apply_labels"
trigger = "automatic"
parallel_group = ""
description = "Move completed worker output into human review."
condition = "task.type == \"research\""
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
parallel_group = ""
description = "Post an edited research answer to GitHub."
condition = "task.type == \"research\""
gate = "review_answer"
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Let the human edit the final reply before posting.", "Keep the posted response succinct and avoid unnecessary caveats."]

[workflow.transitions.auto_create_draft_pr]
from_state = "worker_complete"
to_state = "pr_ready"
action = "github.create_draft_pr"
trigger = "automatic"
parallel_group = ""
description = "Create a draft pull request automatically after the code worker finishes."
condition = "task.type == \"code\""
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Use the worker's PR title and body when present.", "Create only a draft pull request.", "Require the PR description to include the GitHub issue URL and Symphony issue-link marker."]

[workflow.transitions.auto_create_draft_pr.outputs]
pull_request_number = "artifact.pull_request.number"
pull_request_url = "artifact.pull_request.url"
pull_request_title = "artifact.pull_request.title"
head_ref = "artifact.pull_request.head_ref"
head_sha = "artifact.pull_request.head_sha"

[workflow.transitions.create_draft_pr]
from_state = "review"
to_state = "pr_ready"
action = "github.create_draft_pr"
trigger = "human"
parallel_group = ""
description = "Create a draft pull request from a reviewed code attempt."
condition = "task.type == \"code\""
gate = "review_diff"
on_failure = "failed"
retry_limit = 2
timeout_seconds = 0
guidance = ["Use the worker's PR title and body when present.", "Create only a draft pull request.", "Require the PR description to include the GitHub issue URL and Symphony issue-link marker."]

[workflow.transitions.create_draft_pr.outputs]
pull_request_number = "artifact.pull_request.number"
pull_request_url = "artifact.pull_request.url"
pull_request_title = "artifact.pull_request.title"
head_ref = "artifact.pull_request.head_ref"
head_sha = "artifact.pull_request.head_sha"

[workflow.transitions.wait_created_pr]
from_state = "pr_ready"
to_state = "pr_waiting"
action = "workflow.noop"
trigger = "automatic"
parallel_group = ""
description = "Wait for external PR activity after creating a draft pull request."
condition = ""
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Do not immediately reprocess a freshly created draft PR.", "Wait for CI, review, or an explicit human check."]

[workflow.transitions.check_pr_again]
from_state = "pr_waiting"
to_state = "pr_refreshed"
action = "github.fetch_pull_request"
trigger = "human"
parallel_group = ""
description = "Manually check the pull request again for merge, CI, or review changes."
condition = ""
gate = "check_pr_again"
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Use this when GitHub activity has changed and Symphony should reassess the PR."]

[workflow.transitions.check_pr_again.inputs]
pull_request_number = "artifact.pull_request.number"

[workflow.transitions.check_pr_again.outputs]
is_merged = "artifact.pull_request.is_merged"
head_ref = "artifact.pull_request.head_ref"
head_sha = "artifact.pull_request.head_sha"
state = "artifact.pull_request.state"

[workflow.transitions.cleanup_after_merge]
from_state = "pr_refreshed"
to_state = "done"
action = "workspace.cleanup_after_merge"
trigger = "automatic"
parallel_group = ""
description = "Clean up the workspace after the pull request is merged."
condition = "pull_request.is_merged"
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Clean only workspaces owned by Symphony.", "Do not remove worktrees with uncommitted changes."]

[workflow.transitions.refresh_pr_ci]
from_state = "pr_refreshed"
to_state = "pr_checks_complete"
action = "github.fetch_ci_status"
trigger = "automatic"
parallel_group = "refreshed_pr_checks"
description = "Refresh CI status for an unmerged pull request."
condition = "not pull_request.is_merged"
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Capture both failed checks and the full check list for follow-up workers."]

[workflow.transitions.refresh_pr_ci.inputs]
pull_request_number = "artifact.pull_request.number"

[workflow.transitions.refresh_pr_ci.outputs]
failed_checks = "artifact.ci.failed_checks"
checks = "artifact.ci.checks"
state = "artifact.ci.state"
conclusion = "artifact.ci.conclusion"

[workflow.transitions.refresh_pr_comments]
from_state = "pr_refreshed"
to_state = "pr_checks_complete"
action = "github.fetch_pr_review_comments"
trigger = "automatic"
parallel_group = "refreshed_pr_checks"
description = "Refresh review-body and inline comments for an unmerged pull request."
condition = "not pull_request.is_merged"
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Keep review-body comments and inline comments together for the worker."]

[workflow.transitions.refresh_pr_comments.inputs]
pull_request_number = "artifact.pull_request.number"

[workflow.transitions.refresh_pr_comments.outputs]
comments = "artifact.review_comments.comments"

[workflow.transitions.refresh_pr_mergeability]
from_state = "pr_refreshed"
to_state = "pr_checks_complete"
action = "github.detect_merge_conflicts"
trigger = "automatic"
parallel_group = "refreshed_pr_checks"
description = "Refresh mergeability for an unmerged pull request."
condition = "not pull_request.is_merged"
gate = ""
on_failure = "failed"
retry_limit = 1
timeout_seconds = 0
guidance = ["Treat GitHub mergeability as a snapshot that can change after new commits."]

[workflow.transitions.refresh_pr_mergeability.inputs]
pull_request_number = "artifact.pull_request.number"

[workflow.transitions.refresh_pr_mergeability.outputs]
has_conflicts = "artifact.pull_request.has_conflicts"
mergeable = "artifact.pull_request.mergeable"
mergeable_state = "artifact.pull_request.mergeable_state"
head_sha = "artifact.pull_request.head_sha"

[workflow.transitions.mark_blocked]
from_state = "review"
to_state = "blocked"
action = "github.apply_labels"
trigger = "human"
parallel_group = ""
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
path = "/Users/amjith/.local/state/symphony-dbcli/symphony.db"

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
