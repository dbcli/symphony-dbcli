# Symphony Work Orchestrator Spec

## Purpose

This document is a durable, project-neutral specification for rebuilding the
Symphony-style work orchestrator that was developed for DBCLI. It captures the
parts that should survive a rewrite or port to another organization: workflow
definition, durable state, ticket and source-repository abstractions, worker
lifecycle, kanban semantics, API boundaries, and test strategy.

The system coordinates AI workers over external tickets and source
repositories. It syncs external work into a local durable queue, lets a human
choose what should run, executes workflow-defined primitives, records every
meaningful step, and exposes operational APIs for reviewing, rerunning, and
understanding worker activity.

## Ethos

- The orchestrator owns durable execution state. External systems own their
  canonical ticket, pull request, check, and comment records.
- Configuration and workflow behavior should be inspectable and editable in a
  plain file, not hidden in Python control flow.
- The backend should expose clear APIs and typed service boundaries. A React,
  server-rendered, CLI, or other frontend should be able to sit on top without
  changing orchestration semantics.
- Human review gates are first-class workflow states, not special cases.
- Worker output is not trusted until it is persisted, reviewable, and tied to
  the workflow version that produced it.
- Failed, timed-out, and restarted work should resume from durable checkpoints
  instead of starting from scratch.

## Non-Goals

- Do not prescribe a frontend framework or rendering strategy.
- Do not require GitHub as the only ticket or repository provider.
- Do not require GitHub labels, GitHub Projects, Linear status fields, or Jira
  workflow states as the orchestrator queue source of truth.
- Do not make destructive source-control or ticket actions automatic unless a
  workflow explicitly models them behind a human gate.
- Do not target any specific deployment platform in this spec.

## Core Concepts

### Ticket Source

A ticket source is an external work tracker. Examples:

- GitHub Issues
- Linear issues
- Jira issues
- another system with stable ticket identity and comments

Ticket sources provide normalized tickets with:

- provider and external id
- project or repository scope
- title, body, author, labels/status fields, timestamps, and URL
- open/closed state
- comments and attachments when available

Provider-specific data should be stored in metadata, but core workflow logic
should operate on typed ticket snapshots.

### Source Repository

A source repository is a code repository the worker may inspect or modify.
Examples:

- GitHub repository
- GitLab repository
- local git repository
- monorepo path or package boundary

A source repository provides:

- clone/fetch URL and auth strategy
- default branch and branch naming policy
- pull request or merge request provider, if available
- check/CI status provider, if available
- setup instructions and test policy

Tickets and source repositories are related but distinct. A Jira ticket can map
to one or more GitHub repositories; a GitHub issue can map to the repository it
lives in; a Linear issue can be linked to several repos.

### Source Item

A source item is a synchronized external artifact from a ticket or repository
provider. Common source item types:

- ticket
- pull request or merge request
- repository check run
- review comment

Source items mirror external facts. They do not own Symphony workflow state.

### Adapter Boundaries

Adapters normalize provider-specific APIs into the core model:

- ticket adapters sync tickets, comments, labels/status, and ticket-side writes
- repository adapters sync repository metadata, branches, PRs/MRs, checks,
  review comments, mergeability, and repository-side writes
- auth adapters provide provider credentials without leaking provider-specific
  token handling into workflow logic

The core orchestrator should be able to run the same workflow against different
adapters as long as they satisfy the primitive contracts.

### Work Item

A work item is the orchestrator-owned unit of planning, execution, review, and
history. Workers run against work items, not raw provider ids.

A work item:

- links to one or more source items
- has a kanban state defined by the active workflow configuration
- has a task type
- may have an optional human hint
- may have rerun reasons
- may have one active pull request or merge request for the current run
- owns worker runs, attempts, workflow instances, artifacts, gates, and results

## Kanban Model

The orchestrator stores kanban state in SQLite. External tracker state may be
synced and displayed, but it is not the queue authority.

Kanban states are user-configurable in `WORKFLOW.md`. The backend and frontend
clients must read state definitions from the accepted workflow version instead
of hardcoding column names. Users may add states such as `triage`, `blocked`,
`qa`, `ready_to_merge`, or `deferred`, and may remove default states that do
not fit their process.

Customization is for power users. A fresh installation should work out of the
box with an excellent, opinionated default board, sensible transition behavior,
and no requirement to design a workflow before the first useful run. Workflow
initialization should generate the default kanban state definitions, role
mappings, and move policies automatically.

The default workflow should provide these states:

- `backlog`: synced external item exists, but has not been queued
- `todo`: human selected it for orchestration
- `in_progress`: worker or workflow is actively handling it
- `in_review`: output, PR update, answer, or operation summary needs review
- `done`: work is complete

Each configured kanban state should define:

- stable state id
- display label
- board order
- state role, such as backlog, queued, active, review, blocked, or terminal
- whether source items can be activated into it
- whether entering it queues a worker run
- whether entering it should schedule a runtime cycle
- whether it opens a human gate
- allowed manual transitions, if the workflow wants to restrict moves
- default outcome rules for terminal states

Some semantic roles are required even when state names differ:

- one backlog-like role for synced source items not yet accepted as work
- one queued role that represents work selected for orchestration
- one active role that workers can claim
- one review/gated role for human inspection
- at least one terminal role

Recommended terminal outcomes:

- `pr_merged`
- `ticket_closed_external`
- `posted_answer`
- `user_completed`
- `superseded`
- `archived`

### Move Semantics

Moving cards is a workflow input, not just a UI update.

- Moving from a backlog-like state into a queued role creates or reuses a work
  item. If task type is ambiguous, the API should require one of `research`,
  `code`, or `operations`. The move may include an optional hint that is stored
  on the work item and fed into worker prompts.
- Moving into an active role queues a worker run and lets the runtime claim it.
- Moving from a review/gated role back into an active role queues a rerun. The
  request may include rerun reasons such as `fix_ci`, `address_pr_comments`,
  `resolve_merge_conflicts`, `continue_implementation`, `revise_answer`, or
  `rerun_from_top`.
- For review-to-active reruns, do not automatically reuse the original
  activation hint. Use the rerun note/hint for that transition only; stale
  activation context can confuse the worker.
- Moving to a terminal role should require an explicit outcome or external
  completion signal.

When entering a state configured to wake workers, the HTTP response should not
wait for a full orchestration cycle. Persist the state and queued run first,
return the response, then schedule a runtime cycle after the response.

Workflow changes that add, remove, or rename kanban states must be validated
against existing work items. The system should require an explicit migration or
state mapping when accepted workflow changes would strand existing work items in
unknown states.

## Task Types

### Research

Research tasks produce a written answer, investigation summary, or support
response. Posting to the external ticket should be a human-gated action unless
the workflow explicitly allows automatic posting.

### Code

Code tasks allocate a workspace, ask a worker to implement a focused change,
capture tests and summaries, and produce a reviewable diff or pull request.

### Operations

Operations tasks produce durable operational summaries. They may inspect
systems, run bounded commands, or prepare instructions depending on configured
primitives. Potentially destructive operations should require human gates.

## Workflow Definition

Workflow behavior lives in `WORKFLOW.md`. The file should contain prose worker
instructions plus a fenced TOML configuration block. The backend validates the
TOML before accepting a new workflow version.

Required top-level configuration areas:

- profile selection
- ticket source adapters
- source repository adapters
- kanban state definitions and role mappings
- workspace strategy
- worker limits and heartbeat/timeout policy
- dashboard/API binding
- SQLite database path
- Codex or worker runner settings
- policy settings such as dry-run/live mode
- workflow state machine
- worker preferences
- setup steps

### Workflow State Machine

The workflow definition must support:

- `initial_state`
- terminal states
- named states with descriptions and optional gates
- kanban metadata for each state, including display label, board order, and
  role
- named transitions
- `from_state` and `to_state`
- action primitive name
- trigger: `automatic` or `human`
- optional condition expression
- optional human gate name
- retry limit
- timeout seconds
- parallel group
- input mappings
- output mappings
- per-transition guidance
- failure target state

The runtime should reject workflows with:

- unknown actions
- invalid state references
- missing required kanban roles
- duplicate kanban order values within one board
- state removals without migration rules for existing work items
- unreachable non-terminal states
- invalid gate references
- action input/output mapping mismatches
- invalid retry or timeout values
- unsupported conditions

### Artifact Mappings

Primitive outputs are stored as workflow artifacts and may be mapped into later
transition inputs. Artifacts should be named with stable paths, for example:

- `artifact.ticket.primary`
- `artifact.source_repo.primary`
- `artifact.pull_request.number`
- `artifact.ci.failed_checks`
- `artifact.ci.failure_context`
- `artifact.review_comments.comments`
- `artifact.worker.result`

Workflow execution must use stored artifacts as checkpoints. If a primitive
already completed successfully, a resumed worker should reuse its output rather
than rerunning it.

### Example Workflow Shape

A default code workflow should roughly be:

1. claim a queued work item
2. refresh linked ticket and repository source items
3. find or select an active pull request if one exists
4. allocate or reuse an isolated workspace
5. run configured setup steps
6. if no PR exists, run a code or research worker according to task type
7. move worker output to human review
8. after review, post an answer or create a draft PR
9. for an existing PR, fetch CI, review comments, and mergeability
10. fetch bounded CI failure context only when checks failed
11. ask the worker to address PR feedback when CI/comments/conflicts require it
12. human-review the follow-up before pushing to the existing PR branch
13. wait for external PR activity or mark done after merge/closure

## Primitive Contract

Python exposes typed primitives. `WORKFLOW.md` composes them.

Each primitive declares:

- name
- input type and required input fields
- output type and output fields
- side-effect class: none, workspace write, tracker read, tracker write,
  source repo write, or external operation
- idempotency strategy
- whether it can run automatically
- whether it requires a human gate
- timeout policy
- retry behavior
- guidance text accepted from the workflow

Primitive execution requirements:

- validate inputs at the boundary
- persist action start before external side effects
- persist output and status after completion
- store enough metadata to resume or skip safely
- record stdout/stderr excerpts for setup and command primitives
- classify errors by phase and recoverability
- avoid rerunning successful side-effecting primitives during resume

Recommended primitive families:

- `source.sync`
- `source.sync_all`
- `ticket.fetch`
- `ticket.fetch_comments`
- `ticket.post_comment`
- `work_item.activate`
- `work_item.move`
- `work_item.link_source_item`
- `work_item.select_active_pr`
- `workspace.allocate`
- `workspace.run_setup`
- `workspace.record_changes`
- `workspace.cleanup_after_merge`
- `repo.create_draft_pr`
- `repo.push_pr_update`
- `repo.fetch_pull_request`
- `repo.fetch_ci_status`
- `repo.fetch_ci_failure_context`
- `repo.fetch_pr_review_comments`
- `repo.detect_merge_conflicts`
- `codex.research_ticket`
- `codex.fix_issue`
- `codex.address_pr_feedback`
- `codex.operations_task`
- `workflow.noop`

Provider-specific names may be used internally, but the durable spec should keep
the conceptual contract provider-neutral.

## Worker Lifecycle

Workers run one attempt for one work item against one accepted workflow version.

Required lifecycle:

1. claim a queued work item run
2. create attempt and workflow instance rows
3. persist worker record before launching the external worker process
4. heartbeat while running
5. execute workflow transitions through primitives
6. persist timeline, logs, prompt payloads, action runs, artifacts, and results
7. move work item state according to workflow output
8. open human gates when required
9. clean up only through explicit workflow actions or retention policy

Crash and timeout behavior:

- detect stale heartbeat and timed-out workers
- mark abandoned running action runs failed
- count abandoned action runs against transition retry limits
- requeue the same attempt/workflow instance while retry budget remains
- resume from last durable workflow state
- never rerun successful side-effecting primitives just because a process died

## Prompt Logging

Prompt logging is part of durable execution auditability.

- Log the exact prompt payload before it is sent to the worker runner.
- Store prompt events in SQLite and associate them with the attempt and thread.
- For app-server style transports, log just before sending the turn start
  request.
- For exec style transports, log just before launching the process.
- Attempt detail APIs should expose parsed prompt records with timestamp, model,
  approval policy, cwd, and prompt text.
- Prompt logging is not retroactive.

## CI and Pull Request Feedback

PR health checks should gather:

- CI/check status
- failed checks
- bounded failure context
- review-body comments
- inline comments
- mergeability and conflict state

Failed checks alone are not enough for repair prompts. Worker prompts need
actionable failure output: test failure excerpts, tracebacks, check-run output,
or actionable annotations.

CI context filtering requirements:

- Ignore low-signal annotations such as runtime deprecation warnings.
- Ignore generic lines like `Process completed with exit code N` unless paired
  with actionable context.
- Include failure/error annotations only when the message points to a concrete
  test, file, traceback, assertion, or command failure.
- If no actionable CI output is captured, state that explicitly:
  `unavailable: no actionable CI failure output captured.`

This protects workers from chasing infrastructure noise rather than the actual
failure.

## Storage

SQLite is the durable orchestrator database. It is not a cache.

Use:

- SQLite WAL mode
- foreign keys
- SQLAlchemy 2.0 models and repository boundaries
- Alembic migrations for schema changes
- typed boundary objects for service inputs and outputs

The schema can evolve, but it must represent these durable concepts:

- settings
- workflow versions and reload events
- ticket sources
- source repositories
- source sync runs
- source items
- source item links
- work items
- work item links
- work item state events
- work item runs
- attempts
- workflow instances
- workflow transition events
- workflow action runs
- workflow artifacts
- pending workflow gates
- workers
- worker timeline events
- worker logs
- worker errors
- worker results
- prompt/runner events
- turns
- pull requests or merge requests
- comments or drafted responses
- orchestrator chat threads

Important invariants:

- Work item state is owned by the orchestrator DB.
- External source facts are snapshots with `first_seen_at`, `last_seen_at`, and
  external updated timestamps.
- Current-state columns may exist for fast queries, but immutable event rows
  must preserve history.
- Every worker attempt should be tied to the workflow version that produced it.
- Every side-effecting primitive should have a persisted action-run record.
- Use monotonic timestamps for durations and wall-clock timestamps for display.
- Store enough external ids to refresh provider state after restart.

## Backend API

The backend should be FastAPI with typed request/response boundaries. It should
not assume a frontend implementation.

Recommended route groups:

- `/api/ticket-sources`
- `/api/source-repositories`
- `/api/source-items`
- `/api/tickets`
- `/api/work-items`
- `/api/work-items/{id}/moves`
- `/api/work-items/{id}/runs`
- `/api/work-items/{id}/links`
- `/api/work-items/{id}/active-pr`
- `/api/kanban/states`
- `/api/kanban/transitions`
- `/api/workflow`
- `/api/workflow/versions`
- `/api/workflow/validate`
- `/api/workflow/apply`
- `/api/workflow/run-cycle`
- `/api/workflow-gates`
- `/api/workflow-gates/{id}/run`
- `/api/workers`
- `/api/attempts`
- `/api/attempts/{id}`
- `/api/orchestrator/chat`
- `/api/settings`

API behavior:

- write endpoints should validate state transitions server-side
- board clients should fetch kanban state order, labels, roles, and allowed
  transitions from the accepted workflow version
- responses should include durable ids and links to related resources
- human-gate endpoints should accept edited artifacts before side effects
- long orchestration cycles should not block ordinary UI/API state changes
- background scheduling must still preserve exactly-once durable state changes
- errors should return structured machine-readable details

Frontend clients may be React, server-rendered HTML, CLI, or another API
consumer. This spec only requires that the API expose the data and actions
needed to build those clients.

## Orchestrator Runtime

The runtime owns the orchestration loop:

- reload accepted `WORKFLOW.md` changes
- reconcile workers
- sync sources
- advance ready workflow instances
- claim queued work
- launch workers
- clean up eligible workspaces
- expose runtime status

Only one cycle should run at a time. Timer-triggered, manual, and move-triggered
cycles share the same lock and should report `busy` rather than overlapping.

Multiple backend processes require a database-backed leader/runtime lock so
only one process runs the loop.

## Orchestrator Chat

The system should expose a chat/query API for asking the orchestrator about its
state. This is an operational interface, not a worker execution channel.

It should answer questions such as:

- What are the workers doing?
- Why is this work item stuck?
- Which gate is blocking progress?
- What changed between attempts?
- What was the last prompt sent to the worker?
- Which CI failure context was captured?
- What should I review next?
- Is the runtime healthy?

Implementation shape:

- deterministic SQLite fast paths for common status questions
- optional LLM-backed fallback over structured, bounded SQLite context
- links or ids back to source items, work items, attempts, workers, gates, and
  workflow versions
- stored chat threads for auditability

The chat API must not invent state. It should answer from persisted data and
clearly say when information was not captured.

## Workspace Strategy

The default workspace strategy is git worktree:

- maintain one base/bare repository per source repository
- allocate one branch and worktree per active attempt
- never share a writable worktree between active workers
- store base repo path, worktree path, branch, source ref, and commit SHA
- reuse an existing PR branch for follow-up fixes when safe
- clean up only after merge or explicit retention policy

Future strategies may include full clone or remote sandbox execution. They must
share the same primitive contract so workflow definitions do not change.

## Pull Request Update Semantics

When a work item already has an active PR, follow-up actions should update that
PR rather than creating a new one.

Requirements:

- reuse the existing workspace/branch when safe
- fetch latest PR metadata before acting
- push only after a human gate when configured
- do not update stored commit SHA before the push succeeds
- let the worker produce PR title and description content when smart
  summarization is useful
- generate human-readable commit messages from ticket title and worker result
- keep PR title and description succinct and focused on the actual change

## Test Strategy

### Fast Local Tests

Required local coverage:

- workflow parsing and validation
- condition evaluation
- primitive registry validation
- SQLite schema and migrations
- source sync with fake providers
- backlog activation and work-item transitions
- rerun reasons and hints
- runtime cycle locking
- worker crash/timeout resume
- prompt logging
- CI context filtering
- human gate execution
- API route behavior
- orchestrator chat fast paths

### Test Repository

Maintain at least one disposable test repository for end-to-end exercises.

The test repo should contain:

- a tiny codebase
- a deliberately failing test scenario
- a CI workflow
- fixture issues/tickets
- fixture pull requests
- review comments
- merge conflict scenarios when practical

The harness should support:

- local fake-worker runs for speed
- optional live provider-backed runs
- isolated workflow file, DB, repos, and worktree roots
- automatic cleanup of stale fixture state
- scenarios for code fix, research answer, PR feedback, CI failure, and
  source-sync-to-kanban activation

End-to-end tests should verify stored artifacts, state transitions, worker
results, created or updated PRs, comments, gates, and cleanup behavior.

## Porting Checklist

To implement this for a new organization or project set:

1. Define ticket source adapters and auth.
2. Define source repository adapters and auth.
3. Configure source repositories and ticket/project scopes.
4. Configure workspace roots and branch policy.
5. Define task types and default activation behavior.
6. Write or adapt `WORKFLOW.md`.
7. Register provider primitives.
8. Configure worker runner command, model, sandbox, and approval policy.
9. Set SQLite location and migration strategy.
10. Set runtime worker limits and retry policy.
11. Create a disposable test repository and fixture tickets.
12. Run local fake-worker e2e scenarios.
13. Run live provider-backed smoke scenarios.
14. Enable live side effects only after gates and audit logs are verified.

## DBCLI Example Defaults

The DBCLI implementation used these defaults as one concrete adapter, not as
requirements of this spec:

- GitHub repositories: `dbcli/pgcli`, `dbcli/mycli`, `dbcli/litecli`
- local database: `/Users/amjith/.local/state/symphony-dbcli/symphony.db`
- local workspace roots under `.symphony/`
- task types: `research`, `code`, `operations`
- workflow file: `WORKFLOW.md`
- e2e fixture: `amjith/symphony-dbcli-e2e-fixture`

These defaults are useful examples, but a new implementation should treat them
as replaceable adapter configuration.
