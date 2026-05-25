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
- Survive worker crashes, stale heartbeats, and runtime timeouts by resuming
  the same attempt from durable workflow checkpoints rather than starting a
  clean workflow from scratch.
- Enforce per-transition retry limits from `WORKFLOW.md` so retry behavior is
  owned by the workflow definition, not hidden Python control flow.
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
- workspace startup strategy, initially `worktree` with planned `clone`
  support for users who prefer fully separate checkouts
- branch naming policy, branch prefix, and base branch behavior for workspace
  strategies that create branches
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

## Workspace Strategy

Each coding worker should run in its own isolated workspace. The default v1
workspace implementation is a git worktree.

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
- Workspace startup should be modeled as a typed strategy. `worktree` is the v1
  default and should be implemented first because it supports efficient parallel
  workers against one shared base repository. `clone` should be supported as a
  follow-up strategy for users who prefer fully separate checkouts. Branches are
  not a standalone strategy; branch creation, naming, base branch selection, and
  cleanup behavior belong inside the selected workspace strategy.

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
- Persist workflow action runs as checkpoints. If a worker dies after a
  primitive succeeds but before the state transition is recorded, the
  orchestrator must be able to promote that succeeded action output without
  rerunning the primitive.
- Mark abandoned running action runs as failed when a worker crashes, times
  out, or misses its heartbeat. Those failed action runs count against the
  transition's `retry_limit`.
- Requeue process-level crash and timeout retries against the same attempt and
  workflow instance whenever retry budget remains, preserving current state,
  artifacts, action history, timing, and errors.
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
- Requeue interrupted eligible work against the same attempt when retry budget
  remains.
- Preserve previous attempts, logs, workflow action runs, workflow artifacts,
  and transition events.
- Resume from the last durable workflow state. Do not rerun succeeded
  primitives merely because the worker died before recording a transition.
- Rerun only the interrupted or failed primitive, and only while that
  transition's `retry_limit` allows another attempt.
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

## End-to-End Fixture Harness

Use `amjith/symphony-dbcli-e2e-fixture` as the disposable GitHub repository for
live orchestration tests. The repository intentionally contains a tiny Python
module with a failing `fixture_calc.add()` implementation plus a GitHub Actions
workflow that runs `python -m unittest discover -v`.

The local harness is exposed as:

```bash
uv run symphony-dbcli e2e run-fixture
```

The harness is optimized for fast workflow iteration:

- Generates an isolated `WORKFLOW.md`, SQLite database, worktree root, and fake
  Codex executable under `.symphony/e2e/`.
- Forces token-based GitHub API auth for the fixture run so local GitHub App
  config for DBCLI repos does not accidentally route writes to the wrong
  installation.
- Uses local SSH git auth for fixture branch pushes, matching the `gh` git
  protocol used to seed the repository.
- Ensures the Symphony labels exist on the fixture repo.
- Clears stale `symphony:todo` labels from open fixture issues by default so the
  next run claims exactly the newly created issue.
- Creates a fresh GitHub issue labeled `symphony:todo` and
  `symphony:type:code` or `symphony:type:research`.
- Runs the real poll, claim, worktree allocation, worker execution, result
  recording, label transition, review action, branch push, and draft PR
  creation path.
- Uses a deterministic fake Codex command in `codex.transport = "exec"` mode so
  the loop is fast and repeatable before spending real Codex turns.

This fixture becomes the primary experimentation surface for the next workflow
architecture iteration. The codebase should expose action primitives such as
`github.fetch_issues`, `github.fetch_comments`, `codex.research_issue`,
`codex.fix_issue`, `github.create_draft_pr`, `github.fetch_ci_status`,
`codex.fix_ci_failures`, and `codex.address_pr_comments`; `WORKFLOW.md` should
compose those primitives into state machines with explicit automatic
transitions and human gates. New workflow engine behavior should be validated
first against this fixture repository, then against DBCLI repositories.

## Remaining Workflow Engine Task List

The next implementation milestone is to move the workflow state machine out of
hardcoded Python control flow and into `WORKFLOW.md`. Python should provide a
small set of typed, durable action primitives; `WORKFLOW.md` should define how
those primitives are composed into automatic transitions and human-gated steps.

- [x] Define the workflow DSL inside the fenced TOML block in `WORKFLOW.md`.
  It must support states, terminal states, automatic transitions, human gates,
  transition conditions, action inputs, action outputs, retry policy, timeout
  policy, and artifact handoff between steps.
  Progress: states, terminal states, automatic/human transitions, conditions,
  retry limits, timeouts, explicit action input/output mappings, and
  per-primitive guidance are now represented. Runtime action outputs are now
  persisted as workflow artifacts and can be mapped into later transition
  inputs.
- [x] Extend `WORKFLOW.md` to capture worker preferences outside the state
  machine. These preferences should include review expectations, preferred test
  strategy, project-specific coding style, when to run `/review`, and any
  repo-specific instructions that should shape Codex prompts.
- [x] Add per-primitive guidance in `WORKFLOW.md`. Every exposed workflow
  primitive can carry user-editable taste instructions, such as concise reply
  style or PR description expectations, and Codex worker primitives receive
  those instructions in their prompt.
- [x] Add first-class workflow setup steps. The user should be able to define
  commands needed to prepare a repo or worktree before worker execution, such as
  installing test dependencies, running database migrations, generating local
  fixtures, or validating that required tools are available.
- [x] Add typed parser and validation models for the workflow DSL. Validation
  should reject unknown actions, invalid state references, unreachable states,
  missing gate labels, invalid retry settings, and action input/output
  mismatches before a workflow version is accepted. It should also validate
  preference sections and setup-step definitions.
  Progress: typed parsing and validation now cover workflow states,
  transitions, unknown actions, invalid state references, unreachable states,
  human-gate labels, retry settings, timeout settings, preferences, and setup
  steps. Action input/output mappings are validated against primitive
  contracts.
- [x] Add durable workflow runtime tables to SQLite. Track workflow instances,
  current state, pending gates, action runs, action outputs, transition events,
  retries, errors, and the workflow version that produced every runtime row.
- [x] Introduce an action primitive interface and registry. Each primitive must
  declare its name, typed input, typed output, side-effect behavior,
  idempotency key strategy, and whether it can run automatically or only after
  a human gate.
- [x] Implement the first GitHub primitives: `github.fetch_issues`,
  `github.fetch_issue`, `github.fetch_comments`, `github.apply_labels`,
  `github.create_draft_pr`, `github.post_issue_comment`,
  `github.fetch_pull_request`, and `github.fetch_ci_status`.
  Progress: all listed GitHub primitives now execute through the primitive
  layer. Read primitives return typed snapshots that are persisted through
  workflow action outputs/artifacts; write primitives remain guarded by
  `policy.dry_run`. The GitHub primitive surface also includes
  `github.fetch_pr_review_comments` for combined review-body and inline review
  comment context, plus `github.detect_merge_conflicts` for PR mergeability and
  conflict detection.
- [x] Implement the first Codex primitives: `codex.research_issue`,
  `codex.fix_issue`, `codex.address_pr_comments`, and
  `codex.fix_ci_failures`. These should store worker results in SQLite
  regardless of dry-run mode.
  Progress: all listed Codex primitives now execute through the primitive
  layer and store worker results in SQLite regardless of dry-run mode. The PR
  review and CI variants include fetched workflow context in the Codex prompt.
- [x] Implement workspace primitives: `workspace.allocate`,
  `workspace.run_setup`, `workspace.record_changes`, and
  `workspace.cleanup_after_merge`. The first implementation should support the
  existing worktree strategy; a clone strategy should share the same primitive
  contract once added. Setup execution should capture command, duration, exit
  status, stdout/stderr excerpts, and whether failure blocks the worker.
  Progress: all listed worktree-backed workspace primitives now execute through
  the primitive layer and record workflow action outputs/artifacts. Clone
  strategy support remains a future strategy extension.
- [x] Add workspace strategy configuration and validation. The dashboard and
  CLI should clearly show whether new tasks start from git worktrees or full
  clones, which branch policy is active, and where cleanup will happen.
  Progress: config validates the active strategy and branch policy for the
  current worktree implementation, and the dashboard / CLI show the workspace
  strategy, roots, branch prefix, base branch policy, and retention setting.
  Full clone support remains a future strategy implementation.
- [x] Implement human gate primitives for review steps, including reviewing a
  research answer, editing/posting an issue comment, reviewing a code diff, and
  approving draft PR creation.
  Progress: workflow gates are stored, opened after worker completion, shown on
  the dashboard, and executable through the generic human-gate dispatcher for
  posting issue comments, creating draft PRs, and marking attempts blocked.
  Draft replies and draft PR title/body are editable before GitHub side
  effects.
- [x] Refactor the orchestrator loop so it evaluates workflow instances and
  dispatches pending automatic transitions instead of directly encoding
  `todo -> working -> review` behavior in Python.
  Progress: claim and worker execution now advance workflow instances by
  evaluating automatic transitions from `WORKFLOW.md` and executing primitives
  through a generic dispatcher. The poll loop also advances ready, idle
  workflow instances for non-Codex automatic transitions while leaving queued
  and running attempts to worker processes.
- [x] Make workflow execution resilient to worker failure. Worker crashes,
  heartbeat misses, and runtime timeouts should preserve the same attempt and
  workflow instance, mark abandoned running action runs failed, requeue the
  attempt while retry budget remains, and resume from the last durable
  checkpoint. Per-transition `retry_limit` from `WORKFLOW.md` must be enforced
  by the workflow engine.
  Progress: the supervisor now requeues the same attempt after crash/timeout
  when `[workers].retry_limit` allows it, failed/running action runs are
  persisted for retry accounting, and the workflow engine blocks automatic and
  human transitions once their configured `retry_limit` is exhausted.
  Succeeded action runs are reusable checkpoints, so a resumed worker can
  advance the workflow without rerunning a primitive that already completed.
- [x] Move dashboard review actions to workflow gates. The dashboard should
  render available actions from pending gate rows rather than from hardcoded
  route-specific assumptions.
  Progress: attempt pages now render pending workflow gates directly, hide
  review controls unless the matching gate exists, and submit editable draft
  replies / draft PR content through the workflow-gate route.
- [x] Add conversational workflow editing to the dashboard. The user should be
  able to describe any intended workflow change in plain language, including
  state-machine changes, worker preferences, testing policy, `/review`
  behavior, setup commands, and repo-specific instructions. The dashboard
  should show the proposed `WORKFLOW.md` diff, validate it, and apply it
  without leaving the dashboard.
  Progress: the dashboard now has a workflow edit page that accepts a
  plain-language change note, asks Codex to produce a full `WORKFLOW.md`
  proposal, shows the validated diff, allows direct editing of the proposed
  file, and records applied edits in SQLite.
- [x] Upgrade Ask Symphony into a hybrid query system. Keep the current
  deterministic SQLite fast paths for common questions about issue timing,
  turns, errors, worker status, workflow versions, and pending gates; add an
  LLM-backed fallback over structured SQLite context for richer questions such
  as why a worker is stuck, what changed between attempts, or how to interpret
  recent failures.
  Progress: Ask now keeps the fast deterministic paths and adds a structured
  SQLite context fallback for pending gates, stuck/blocked work, and recent
  failures, with a typed fallback provider seam for an LLM-backed answerer.
- [x] Add workflow state-machine visualization to the dashboard. Render states,
  automatic transitions, human gates, terminal states, and the current runtime
  position of active issues so users can verify the workflow intention before
  and while it runs.
- [x] Recreate the current behavior as the default workflow in `WORKFLOW.md`:
  fetch labeled issues, claim work, allocate a worktree, run Codex, store the
  result, move to human review, optionally post a reply or create a draft PR,
  and clean up the worktree after PR merge.
  Progress: the default workflow is encoded in `WORKFLOW.md` and the runtime
  executes the claim, workspace, Codex, review, PR/comment, and cleanup path via
  workflow transitions and primitives. After draft PR creation, the default
  workflow now fetches PR metadata, detects merge conflicts, checks CI, feeds
  failing checks to Codex, fetches review/inline comments together, feeds review
  comments to Codex, and uses human gates before pushing follow-up fixes.
- [x] Add durable issue-to-PR association and parallel PR health checks.
  When an issue is picked up, Symphony first finds associated PRs using
  database bookkeeping plus an exact hidden PR-description marker of the form
  `symphony-dbcli:issue-link=<issue-url>`. Incidental issue mentions are not
  trusted. If a PR is found, the workflow allocates the PR branch, then fans out
  CI status, review/inline comments, and mergeability checks in parallel. The
  fan-in step stores all outputs as workflow artifacts and either launches one
  combined Codex PR-feedback task or waits behind the next human gate when
  nothing needs action.
- [x] Add fixture workflows under the e2e harness for fast iteration: code
  happy path, research answer review, research-to-code follow-up, PR review
  comments addressed by Codex, and CI failure fixed by Codex.
  Progress: the e2e harness exposes named scenarios for each fixture workflow
  shape so local and GitHub-backed runs can select them quickly from the CLI;
  follow-up scenarios queue code tasks or run the relevant Codex PR/CI
  primitive after the draft PR path. It also includes
  `associated_pr_parallel_checks`, which pre-creates a marker-linked PR and
  review comment so the full PR-discovery, parallel-check, and combined
  PR-feedback path can be exercised against the fixture repository.
- [x] Add end-to-end tests that execute workflow files against
  `amjith/symphony-dbcli-e2e-fixture` and assert state transitions, stored
  artifacts, labels, comments, draft PRs, and cleanup behavior.
  Progress: pytest now includes an opt-in GitHub-backed fixture test guarded by
  `SYMPHONY_RUN_GITHUB_E2E=1`; normal local runs keep fast unit coverage while
  the live fixture path exercises the real repository, workflow file, issue,
  worker, and draft PR flow.
- [x] Keep compatibility with the existing dashboard, CLI, and SQLite data
  where practical. Add migrations for the new workflow runtime tables instead
  of rewriting existing attempt, worker, comment, and PR history.

## Source-Backed Kanban Pivot Task List

The next major architecture milestone is documented in
[SOURCES_AND_WORK_ITEMS.md](SOURCES_AND_WORK_ITEMS.md). This pivots Symphony
from a GitHub-label-driven dispatcher to a source-backed, database-owned kanban
board. GitHub Issues and PRs become synced source items; Symphony work items
become the durable unit of orchestration.

Core decisions recorded in the design:

- Sources are GitHub repositories in v1.
- Source sync defaults to all open issues and pull requests.
- Source filters can include labels, authors, stale items, and date ranges.
- GitHub labels are no longer the primary queue mechanism.
- Kanban state lives in SQLite on Symphony work items.
- Workflows run against `work_item_id`, not a raw GitHub issue number.
- The dashboard should migrate to FastAPI, Jinja partials, HTMX, SortableJS,
  SQLAlchemy 2.0, and Alembic. The project is pre-alpha, so compatibility may
  be broken when the cleaner work-item design requires it.
- Moving backlog to todo creates or reuses a work item and records task type,
  optional user hint, and linked source items.
- Task types are `research`, `code`, and `operations`.
- Operations produce durable summaries viewable in the dashboard.
- Issue and PR links are stored strongly in SQLite and reinforced by exact PR
  body markers for Symphony-created PRs.
- Multiple PRs per issue are allowed in the model and shown as grouped cards
  with one active PR for the current run.
- Moving `in_review` back to `in_progress` allows optional multi-select
  reasons; no reason means rerun from the top.
- Symphony may automatically mark work items done when linked PRs merge or
  linked issues close externally.

Implementation checklist:

- [x] Add FastAPI application scaffolding alongside the current dashboard.
- [x] Split FastAPI routers by dashboard hierarchy: board, sources, work
  items, workers, workflow, ask, settings, and narrowly scoped JSON APIs.
- [x] Add SQLAlchemy 2.0 models and session/repository boundaries for source
  data.
- [x] Add SQLAlchemy 2.0 models and session/repository boundaries for work-item
  data.
- [x] Add Alembic migration scaffolding.
- [x] Add Alembic revisions for new source schema changes.
- [x] Add Alembic revisions for new work-item schema changes.
- [x] Add HTMX and SortableJS assets for server-rendered interactive kanban
  behavior.
- [x] Add SQLite/Alembic table for `sources`.
- [x] Add SQLite/Alembic tables for `source_sync_runs` and `source_items`.
- [x] Implement GitHub source sync for open issues and open PRs.
- [x] Add source filter support for labels, authors, stale items, and date
  ranges.
- [x] Build a basic Sources dashboard page with add/list controls.
- [x] Add source sync controls and sync status.
- [x] Add source edit controls for filters and source settings.
- [x] Add `work_items`, `work_item_links`, state history, and run/reason
  storage.
- [x] Build the source-scoped kanban board shell with backlog, todo, in
  progress, in review, and done columns.
- [x] Populate source-scoped backlog from synced source items.
- [x] Populate todo, in progress, in review, and done from work-item state.
- [x] Implement backlog-to-todo activation with task type and optional user
  hint.
- [ ] Default issue-with-linked-PR and PR cards to review/fix mode.
- [x] Implement in-review-to-in-progress reason selection with multi-select
  reasons.
- [ ] Pivot orchestrator runtime identity to `work_item_id`.
- [ ] Adapt workflow artifacts and input resolution to expose linked issue,
  active PR, user hint, and rerun reasons.
- [ ] Add source/work-item primitives such as `source.sync`,
  `work_item.activate`, `work_item.move`, and `work_item.select_active_pr`.
- [ ] Add `codex.operations_task` and operation-summary dashboard views.
- [ ] Update PR creation to link work item, issue, and PR in SQLite and in the
  PR body marker.
- [ ] Add grouped issue/PR cards with an expandable detail view and active PR
  selector.
- [ ] Auto-mark work items done on external PR merge or issue close with
  explicit outcome.
- [ ] Add ignore/archive support for source items.
- [ ] Update Ask Symphony for source, work item, and kanban-state questions.
- [x] Add fast local tests for source sync, backlog-to-todo activation, and
  kanban state transitions.
- [ ] Add fast local tests for grouping and work item workflow execution.
- [x] Add a GitHub-backed e2e smoke scenario for source sync through kanban
  activation.
- [ ] Extend the GitHub-backed e2e scenario through PR review/fix workflow.

Progress notes:

- 2026-05-24: Installed FastAPI, Uvicorn, SQLAlchemy, Alembic,
  python-multipart, and httpx with `uv`. Added a typed FastAPI app factory,
  route modules that match the dashboard hierarchy, separate CSS/JS assets,
  a `serve-web` CLI command, SQLAlchemy base/session helpers, Alembic
  scaffolding, and fast route tests.
- 2026-05-24: Added the first real Sources flow: `/sources/new`, source
  validation, SQLite persistence through SQLAlchemy, an Alembic revision for
  `sources`, and tests that cover adding and listing a repository source.
- 2026-05-25: Added a dashboard dark-mode toggle and removed the
  auto-dispatch toggle. Auto-dispatch is now treated as always on, including
  when an old database setting says otherwise.
- 2026-05-25: Split the board by source. `/board` now selects a source when
  available, source tabs link to isolated board views, the Sources table links
  to each source board, and the sync action is shown as pending instead of a
  dead button until GitHub source sync lands.
- 2026-05-25: Implemented GitHub source sync. Source sync now fetches open
  issues and pull requests, records sync runs and source items in SQLite, and
  renders synced items in the selected source board backlog.
- 2026-05-25: Added work-item tables, repository boundaries, source-item to
  work-item activation, task type and hint capture, state event/run records,
  work-item list/detail pages, and board rendering from work-item state.
  Verified source sync through backlog-to-todo activation against the
  `amjith/symphony-dbcli-e2e-fixture` repository.
- 2026-05-25: Added source edit controls and structured source filters for
  labels, authors, updated date ranges, and stale items. Source sync now
  applies those filters before writing source items to SQLite.
- 2026-05-25: Added HTMX and SortableJS dashboard assets, work-item move
  endpoints, durable state events for manual kanban transitions, and queued
  rerun records when in-review items move back to in-progress with selected
  reasons.

## Durable Cross-Project Spec

After the DBCLI implementation is complete, produce a durable, project-neutral
spec file that can be used to recreate this orchestrator for repositories
outside `dbcli`. This should be a first-class deliverable, not an afterthought
buried in the DBCLI implementation notes.

The durable spec should separate reusable Symphony behavior from DBCLI defaults:

- Reusable core: workflow DSL, primitive registry contract, SQLite runtime
  state model, worker lifecycle, retry/resume semantics, human gates,
  dashboard/ask behavior, setup steps, workspace strategies, and deployment
  profiles.
- Project adapters: tracker configuration, repository list, labels, auth
  strategy, worker taste preferences, setup commands, test policy, branch
  naming, and dashboard branding.
- Example defaults: DBCLI can be included as one worked example, but the spec
  must not require DBCLI repo names, DBCLI labels, exe.dev paths, or GitHub-only
  assumptions except where explicitly called out as one adapter choice.

Minimum contents for the durable spec:

- System goals and non-goals.
- Required storage schema and invariants.
- `WORKFLOW.md` DSL reference with states, transitions, conditions, inputs,
  outputs, artifacts, retries, timeouts, setup, preferences, profiles, and
  human gates.
- Primitive interface contract, including side-effect classification,
  idempotency strategy, input/output fields, guidance text, retry behavior,
  and resume/checkpoint requirements.
- Worker lifecycle contract covering launch, heartbeat, timeout, crash
  detection, retry, resume, cancellation, and cleanup.
- Dashboard contract covering workflow visualization, pending human gates,
  editable artifacts before side effects, worker status, attempt details, and
  Ask Symphony answers with links back to detailed pages.
- Deployment contract for local and production profiles.
- Test harness requirements for fast local fixture runs and optional live
  tracker-backed end-to-end runs.
- Porting checklist for bringing the orchestrator to a new organization or
  project set.
- A minimal example workflow for a non-DBCLI repository.

## Assumptions

- Initial scope is exactly `dbcli/pgcli`, `dbcli/mycli`, and `dbcli/litecli`.
- A reusable cross-project spec is required after the DBCLI implementation is
  complete.
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
