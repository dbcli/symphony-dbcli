# Python Symphony for DBCLI on exe.dev

## Summary

Build a Python implementation of the
[OpenAI Symphony spec](https://github.com/openai/symphony/blob/main/SPEC.md)
for `dbcli/pgcli`, `dbcli/mycli`, and `dbcli/litecli`, replacing Linear with
GitHub Issues and using SQLite as durable orchestrator state.

The v1 system runs as a single long-lived exe.dev VM with a private
exe.dev-authenticated dashboard.

Primary v1 capabilities:

- Poll GitHub Issues across the three DBCLI repos.
- Dispatch labeled issues into isolated per-issue git worktrees so workers can
  operate in parallel against the same repository without sharing a checkout.
- Run Codex App Server over stdio JSON-RPC.
- Support both coding tasks and research/support-answer tasks.
- Persist workers, issue snapshots, attempts, events, comments, PR links,
  token metrics, and dashboard state in SQLite.
- Provide a dashboard plus an "ask Symphony" interface for issue, worker, and
  task status questions.

## Architecture

The implementation should be a Python package in this repository with a CLI
entrypoint named `symphony-dbcli`.

Main subsystems:

- Orchestrator: polls GitHub, reconciles SQLite state, claims work, and launches
  workers.
- GitHub tracker adapter: normalizes GitHub Issues into the Symphony issue model
  and handles labels, comments, branches, and PR metadata.
- SQLite store: durable source of truth for issue snapshots, workers, attempts,
  events, logs, comments, PRs, and dashboard state.
- Workspace manager: creates deterministic per-issue workspaces on the exe.dev
  persistent disk using `git worktree`.
- Codex runner: starts `codex app-server` per worker over local stdio JSON-RPC.
- Dashboard web app: shows worker health, queue state, attempts, timelines, and
  issue/worker details.
- Ask interface: answers questions about active tasks and workers from SQLite,
  recent logs, and optionally refreshed GitHub state.

## GitHub Workflow

Use GitHub labels as the workflow source of truth instead of Linear or GitHub
Projects.

Initial labels:

- `symphony:todo`: dispatchable work.
- `symphony:working`: claimed or running.
- `symphony:review`: human review needed, PR ready, or answer comment ready.
- `symphony:blocked`: not dispatchable.
- `symphony:done`: terminal.
- `symphony:type:code`: coding task.
- `symphony:type:research`: research, triage, or support-answer task.

Dispatch rules:

- Only open issues with `symphony:todo` and without `symphony:blocked` are
  eligible.
- Task type comes from the `symphony:type:*` label.
- Candidates are sorted by label-derived priority, creation time, then repo and
  issue number.
- The orchestrator moves claimed issues to `symphony:working`.
- Finished coding tasks move to `symphony:review` with a linked PR.
- Finished research tasks move to `symphony:review` with a drafted or posted
  answer comment.

## Configuration

Repository-owned configuration should live in `WORKFLOW.md`, extended from the
Symphony model with DBCLI-specific settings.

`WORKFLOW.md` should be created interactively by `symphony-dbcli init-workflow`.
The wizard must ask for the tracker repositories, GitHub label mapping, worker
limits, worktree root, database path, dashboard binding, and default task
policies. It should write a readable Markdown file with a fenced TOML config
block plus prose instructions for workers.

Minimum v1 configuration fields:

- `tracker.kind: github`
- `github.repos: ["dbcli/pgcli", "dbcli/mycli", "dbcli/litecli"]`
- label mappings for active, terminal, blocked, task type, and review states
- `workspace.strategy: worktree`
- `workspace.root`, defaulting locally to `.symphony/worktrees`
- `workspace.bare_repos_root`, defaulting locally to `.symphony/repos`
- maximum concurrent workers globally and per repo
- per-repo workspace bootstrap hooks
- Codex command/settings pass-through
- dashboard host/port settings
- SQLite database path, defaulting locally to `.symphony/symphony.db`
- runtime profiles for local and production defaults

The default local configuration should run without credentials for development
where possible, but GitHub writes require a configured GitHub App.

Runtime configuration behavior:

- Profile selection uses `--profile`, then `SYMPHONY_PROFILE`, then
  `[profile].active`, then `local`.
- The generated workflow includes `local` defaults under `.symphony/` and `prod`
  defaults under `/srv/symphony`.
- The orchestrator watches `WORKFLOW.md` for changes while running.
- A valid update is applied without restarting the service.
- Every accepted workflow version is recorded in SQLite with timestamp,
  content hash, parsed config JSON, and a text diff from the prior version.
- Invalid workflow updates are rejected, recorded as failed reload attempts, and
  do not affect already-running workers.
- New workers use the latest accepted workflow version.
- Existing workers continue with the workflow version they started with, and
  that version id is stored on their attempt record.

## Worktree Strategy

Each coding worker should run in its own git worktree.

Worktree behavior:

- Maintain one bare or shared base repository per GitHub repo under
  `workspace.bare_repos_root`.
- For each issue attempt, create a branch named with a deterministic prefix,
  such as `symphony/dbcli-pgcli-123-attempt-2`.
- Create a worktree under `workspace.root`, such as
  `.symphony/worktrees/dbcli_pgcli_123_attempt_2` locally or
  `/srv/symphony/worktrees/dbcli_pgcli_123_attempt_2` in production.
- Never let two active workers share a worktree path or branch.
- Remove stale worktrees only through an explicit cleanup command or retention
  policy; do not delete active or recently failed attempts automatically.
- Store the base repo path, worktree path, branch, commit SHA, and workflow
  version id in SQLite.
- Research/support tasks may use a read-only worktree when repository context is
  needed, but they must not push branches unless explicitly converted to a code
  task.

## SQLite Data Model

SQLite is the durable orchestrator database, not a cache.

Initial tables:

- `repos`
- `issues`
- `issue_labels`
- `workers`
- `attempts`
- `codex_events`
- `codex_turns`
- `worker_timeline_events`
- `worker_errors`
- `worker_logs`
- `pull_requests`
- `comments`
- `orchestrator_events`
- `workflow_versions`
- `workflow_reload_events`
- `ask_threads`
- `settings`

Database requirements:

- Enable WAL mode.
- Enable foreign keys.
- Store immutable event rows with timestamps.
- Keep current-state columns for fast dashboard queries.
- Persist worker state before launching external processes so restarts can
  reconcile safely.
- Store monotonic timestamps and wall-clock timestamps for worker timing so
  elapsed durations are accurate even if system time changes.
- Associate every attempt, turn, error, PR, comment, and log event with the
  workflow version that produced it when applicable.

Metrics to record:

- Queue latency: issue first seen, eligible, claimed, worker started.
- Worker duration: process start, Codex session start, first model response,
  final response, test start/end, PR/comment creation, completion.
- Codex activity: thread id, turn count, per-turn start/end time, input/output
  token counts when available, model name, and tool call count.
- Error activity: error type, phase, message, recoverability, retry count,
  associated turn id when available, and stack/log excerpt.
- Outcome: success, blocked, failed, cancelled, timed out, or needs review.

## Worker Behavior

For coding tasks:

- Update the shared base repository and create a fresh per-attempt git worktree.
- Prompt Codex to inspect the issue, implement the fix, run relevant tests, and
  summarize proof of work.
- Create a branch, commit, push, and open or update a PR through the GitHub App.
- Comment on the issue with the PR link and test summary.
- Move the issue to `symphony:review`.

For research/support tasks:

- Load issue context, recent comments, relevant repository docs/code, and any
  configured support context.
- Prompt Codex to draft a concise answer with evidence.
- Either post the answer or store it for review, depending on workflow policy.
- Move the issue to `symphony:review` once the answer is ready.

On restart:

- Reconcile active workers from SQLite.
- Check whether GitHub state changed while the service was down.
- Requeue interrupted eligible work.
- Preserve previous attempts and logs.
- Preserve timing data and mark interrupted attempts with a restart/reconcile
  event rather than overwriting their state.

## Dashboard

Expose a private dashboard through exe.dev's authenticated HTTPS proxy.

Initial routes:

- `/`: worker health, queue depth, retries, blocked issues, PR-ready tasks, and
  answer-ready tasks.
- `/issues/{repo}/{number}`: issue timeline, worker attempts, Codex events,
  comments, PR links, and current labels.
- `/workers/{id}`: worker status, issue assignment, workspace path, recent logs,
  runtime, current phase, turn count, error count, and token metrics.
- `/ask`: natural-language questions about current tasks, workers, and issue
  state.

The dashboard should be polished but operational: dense, scannable, and useful
for repeated monitoring rather than a marketing-style page.

The dashboard and ask interface must be able to answer:

- How long did Codex work on a specific issue?
- How much time was spent queued, running Codex, testing, and opening a PR or
  drafting an answer?
- How many Codex turns were taken for an issue?
- How many errors occurred, in which phase, and whether they were recovered?
- Which workflow version was active for an issue attempt?

## CLI

Initial commands:

- `symphony-dbcli init-db`
- `symphony-dbcli init-workflow`
- `symphony-dbcli workflow validate`
- `symphony-dbcli workflow history`
- `symphony-dbcli serve`
- `symphony-dbcli poll-once`
- `symphony-dbcli worker run --repo OWNER/REPO --issue NUMBER`
- `symphony-dbcli worktree cleanup`
- `symphony-dbcli status`

The CLI should read configuration from `WORKFLOW.md` by default, with explicit
flags for profile selection, database path, log level, and dry-run mode.

## exe.dev Deployment

Run the v1 service on a single exe.dev VM.

Deployment assumptions:

- SQLite database lives on the persistent VM disk under the `prod` profile.
- Shared base repositories and per-attempt worktrees live under the `prod`
  profile's `/srv/symphony/repos` and `/srv/symphony/worktrees` paths.
- Dashboard is exposed through exe.dev private HTTPS.
- Codex App Server is launched locally per worker over stdio.
- The Codex WebSocket listener is not exposed remotely.

## Test Plan

Add focused tests for:

- Workflow parsing and validation.
- Interactive `WORKFLOW.md` generation.
- Runtime workflow reload, accepted-version history, invalid-version rejection,
  and diff recording.
- GitHub label-state mapping.
- Issue normalization.
- Workspace key sanitization and git worktree path/branch allocation.
- Retry/backoff behavior.
- SQLite schema creation and persistence.
- Polling and claiming behavior with mocked GitHub API responses.
- Label transitions, comments, PR recording, and restart reconciliation.
- Codex runner behavior using a fake JSON-RPC app-server process.
- Worker timing, turn counting, error recording, and duration aggregation.
- Dashboard pages and `/ask` responses using seeded SQLite fixtures.

Before enabling the three DBCLI repos, run an end-to-end dry run against a test
GitHub repository.

## Assumptions

- Initial scope is exactly `dbcli/pgcli`, `dbcli/mycli`, and `dbcli/litecli`.
- GitHub Issues are the v1 tracker source of truth.
- GitHub Projects are out of scope for v1.
- Linear is out of scope for v1.
- Dashboard access is private through exe.dev authentication.
- GitHub writes use a GitHub App, not a user PAT or persistent `gh` session.
- Merging PRs and destructive repo actions are out of scope for v1.
- Research/support tasks produce reviewed answers before any later automation
  that posts without human review.
- Parallel coding workers require git worktrees; plain cloned directories are
  not an acceptable v1 substitute.
- Hot workflow updates are accepted for future workers only; active workers keep
  the workflow version they started with.

## References

- [OpenAI Symphony spec](https://github.com/openai/symphony/blob/main/SPEC.md)
- [OpenAI Symphony announcement](https://openai.com/index/open-source-codex-orchestration-symphony/)
- [Codex App Server docs](https://developers.openai.com/codex/app-server)
- [exe.dev docs](https://exe.dev/docs/all)
- [GitHub Issues REST docs](https://docs.github.com/en/rest/issues/issues)
- [dbcli/pgcli](https://github.com/dbcli/pgcli)
- [dbcli/mycli](https://github.com/dbcli/mycli)
- [dbcli/litecli](https://github.com/dbcli/litecli)
