# Sources and Work Items Design

This document defines the pivot from a GitHub-label-driven queue to a
source-backed, database-owned task board. GitHub remains the external system of
record for issues, pull requests, comments, checks, and merge state. Symphony
owns the work queue, kanban state, worker runs, artifacts, and review gates.

## Goals

- Add repositories as sources and sync their open issues and pull requests.
- Stop relying on GitHub labels as the primary dispatch mechanism.
- Build a minimal, durable task list in SQLite.
- Let the user choose which synced items enter the work queue.
- Run workflows against Symphony work items instead of raw GitHub issue numbers.
- Keep issue and pull request relationships durable, explicit, and visible.
- Support research, code, and operations tasks.
- Preserve the existing workflow DSL direction: Python exposes primitives;
  `WORKFLOW.md` composes them.

## Non-Goals

- Do not implement GitHub Projects as the kanban source of truth.
- Do not require GitHub labels for dispatch.
- Do not infer issue-to-PR links from casual text mentions.
- Do not fully automate stacked PR management in the first slice.
- Do not make operations tasks arbitrary shell automation without human-gated
  action primitives.

## Technical Stack

The source-backed kanban pivot makes the current custom HTTP server and
handwritten persistence layer too limiting. The project is still pre-alpha, so
this migration may break compatibility where that produces a cleaner design.

Recommended stack:

- FastAPI for HTTP routing, forms, responses, dependency boundaries, and tests.
- Jinja templates for server-rendered pages and partials.
- HTMX for server-driven interactions and partial page updates.
- SortableJS for kanban drag/drop events.
- Small, plain JavaScript glue only where browser behavior requires it.
- SQLAlchemy 2.0 for persistence boundaries and relationship-heavy queries.
- Alembic for explicit schema migrations.
- Custom CSS files; no component library until the UI clearly needs one.

### FastAPI

FastAPI should replace the current `BaseHTTPRequestHandler` dashboard over
time. The value is better routing, structured request parsing, test clients,
dependency injection, consistent error handling, and cleaner separation between
HTTP, application services, and persistence.

The application does not need to become async-heavy. Synchronous route handlers
and SQLite access are acceptable for the local/operator dashboard. Async can be
introduced later only where it buys real simplicity or throughput.

### SQLAlchemy and Alembic

The upcoming data model includes sources, source items, work items, links,
sync runs, work item runs, PR relationships, grouped cards, artifacts, and state
history. Raw SQL will become harder to maintain as these relationships grow.

Use SQLAlchemy 2.0 with typed models and keep ORM usage behind repository or
service boundaries. Application and workflow logic should still operate on
clear typed boundary objects rather than letting ORM models spread through the
whole codebase.

Use Alembic for schema changes. We are past the point where migrations should
be ad hoc `CREATE TABLE IF NOT EXISTS` plus opportunistic column patches.

### HTMX

HTMX is the preferred UI interaction layer because Symphony's state is
server-owned. The browser should not maintain a parallel workflow model. Each
important interaction should round-trip to the server, update SQLite in a
transaction, and return a rendered HTML fragment.

Expected HTMX patterns:

- source sync buttons update source status panels
- backlog-to-todo activation returns a server-rendered modal
- activation form submission returns updated board columns
- review-gate actions update the card, detail panel, or gate list
- Ask Symphony can update inline answers without full-page navigation
- workflow cycle/run-now buttons can return status banners or refreshed panels

For kanban drag/drop, use SortableJS to capture the drag event and then submit
the intended transition to the server. HTMX should handle the request and swap
the returned column/card fragments.

This keeps the frontend small while still making the dashboard feel responsive.
A full SPA framework can be reconsidered later if the UI becomes heavily
client-state-driven or real-time collaborative.

### Migration Strategy

Do not rewrite everything at once.

1. Introduce SQLAlchemy and Alembic for new source/work-item tables.
2. Add FastAPI alongside the current dashboard entrypoint.
3. Build the new Sources page and kanban board in the new stack.
4. Move existing dashboard pages one route at a time.
5. Keep existing store/orchestrator behavior working until the work-item pivot
   replaces the issue/attempt primary identity.
6. Retire the custom HTTP handler after equivalent FastAPI routes exist.

## Core Concepts

### Source

A source is an external location Symphony syncs from. In v1, sources are GitHub
repositories such as `dbcli/litecli`, `dbcli/pgcli`, and `dbcli/mycli`.

Each source has sync settings:

- default: include all open issues and open pull requests
- optional label include/exclude filters
- optional author include/exclude filters
- optional stale-item filters
- optional created/updated date ranges
- draft pull requests are included by default

Sources should support both timer-based sync and manual "Sync now" actions.

### Source Item

A source item is a synced GitHub issue or pull request. It mirrors external
facts and should not carry Symphony workflow ownership. Examples:

- GitHub issue `dbcli/litecli#245`
- GitHub pull request `dbcli/litecli#254`

Source items are kept fresh by source sync. Closed issues and merged/closed PRs
remain in the database for history, but no longer appear in the default backlog
view unless filters include them.

### Work Item

A work item is the Symphony-owned unit of planning, execution, review, and
dashboard display. The orchestrator should run against `work_item_id` as the
primary durable identity.

Work items link to one or more source items. A work item can start from an issue
or from a PR. If an issue later produces a PR, the same work item gains a PR
link and the board card remains the same logical item.

### Work Item Links

Work item links connect Symphony work items to source items. Links make grouped
cards possible and preserve history when one issue produces one or more PRs.

The first implementation should support many source items per work item. The UI
can optimize for the common case of one issue plus one active PR without
enforcing that shape in the schema.

### Issue-to-PR Links

Issue-to-PR links are a specific durable relationship between GitHub source
items. They should be recorded in SQLite and, for PRs created by Symphony,
reinforced by an exact hidden PR-description marker:

```text
<!-- symphony-dbcli:issue-link=https://github.com/<owner>/<repo>/issues/<number> -->
```

Symphony must not treat incidental issue mentions as durable links.

## Kanban States

The user-facing board has five columns:

- `backlog`: synced external issue/PR exists, but the user has not queued it.
- `todo`: user selected this work item for Symphony to run.
- `in_progress`: orchestrator or worker is currently handling it.
- `in_review`: Symphony produced an artifact, PR update, or operational result
  that needs human attention.
- `done`: the work item is complete.

The kanban state belongs to `work_items`, not GitHub labels.

`done` should include an outcome/reason field, for example:

- `pr_merged`
- `issue_closed_external`
- `user_completed`
- `posted_research_answer`
- `superseded`

Symphony may automatically move a work item to `done` when its linked PR is
merged or its linked issue is closed externally.

## Backlog Behavior

When a source is synced, all matching open issues and PRs appear in backlog.
Backlog cards are source items unless Symphony can safely group them.

Auto-grouping is allowed only when there is a durable DB link or exact hidden PR
marker. Otherwise, issue and PR cards stay separate to avoid false positives.

The backlog should support ignore/archive behavior for noise. Ignored or
archived source items should be hidden by default but recoverable through
filters.

## Moving Backlog to Todo

Dragging a backlog item to todo creates or reuses a work item.

Reuse rule:

- Reuse an existing non-done work item for the source item.
- Create a new work item only when the previous one is `done` or explicitly
  archived/superseded.

Activation behavior depends on the source item:

### Issue with Linked PR

If the issue already has a durable linked PR, default the work item to
review/fix existing PR. Do not ask the user to choose research vs code unless
they explicitly override the default.

### Issue without Linked PR

Ask the user to choose a task type:

- `research`
- `code`
- `operations`

The activation modal should also allow an optional user hint. The hint becomes
workflow input and should be fed to Codex prompts.

### Pull Request

If the source item is a PR, default to PR review/fix. The workflow should check
CI, review comments, inline comments, and mergeability before deciding whether
Codex needs to act.

## Task Types

### Research

Research tasks produce a written answer or support response. The result is
stored in SQLite and shown in the dashboard. GitHub posting remains a human gate
unless a workflow explicitly changes that policy.

### Code

Code tasks ask Codex to modify a workspace and produce either a PR or a
reviewable artifact. If a PR is created, Symphony records a strong DB link
between the work item, issue, and PR.

### Operations

Operations tasks are neither research nor code. They should produce a durable
summary of actions taken and relevant outputs, stored in SQLite and viewable
from a dashboard page.

Initial scope should be Codex-guided operations with recorded summaries. Shell
or external side-effect primitives can be added later and should be human-gated
unless proven safe.

## In-Progress Workflow

The orchestrator picks up `todo` work items and moves them to `in_progress`.
The workflow receives `work_item_id` as the primary identity and loads linked
source item context as artifacts.

The top-level behavior should be:

1. Refresh linked issue and PR source items.
2. If the work item has an active PR, run PR health checks.
3. If the work item has an issue with linked PRs, select the active PR and run
   PR health checks.
4. If the work item has an issue with no PR, run issue work according to task
   type.
5. Store all worker results, operation summaries, draft replies, PR updates,
   and artifacts in SQLite.
6. Move to `in_review` when human attention is needed.
7. Move to `done` automatically only for external completion signals such as PR
   merged or issue closed.

PR health checks should include:

- CI/check status
- PR review comments and inline comments
- mergeability and merge conflict detection

The checks can run in parallel and feed their combined outputs into the next
workflow step.

## Review Loop

`in_review` is a human gate. The user can:

- edit and post a research answer
- review and create a draft PR
- review and push PR follow-up changes
- mark the work item done
- move the work item back to `in_progress`

When moving `in_review` back to `in_progress`, the user may optionally choose
one or more reasons:

- address PR comments
- fix CI
- resolve merge conflicts
- continue implementation
- revise answer
- rerun from top

If no reason is selected, Symphony reruns the workflow from the top. Selected
reasons become workflow input and should bias which checks/actions run first,
but they should not bypass durable refresh of source item state.

## Multiple PRs Per Issue

The data model must not enforce one issue equals one PR.

Recommended behavior:

- A work item may link to multiple PR source items.
- The board shows a grouped card for the issue and linked PRs.
- The grouped card can be expanded from the dashboard.
- The work item has one active PR for the current run.
- The user can choose a different active PR from the expanded view.

Stacked PR support can be layered on later with:

- `pr_stack_id`
- `stack_order`
- `base_pr_source_item_id`
- `active_for_work_item`

The first implementation should display multiple PRs clearly and operate on one
active PR at a time.

## Dashboard Information Architecture

The dashboard should be an operations console, not a metrics landing page. The
default page should be the board because that is where the user decides what
Symphony should work on next.

### Top-Level Navigation

The primary navigation should expose:

- Board
- Sources
- Work Items
- Workers
- Workflow
- Ask
- Settings

### Page Hierarchy

The dashboard hierarchy should be:

1. Board shows what needs attention.
2. Work item detail explains one unit of work.
3. Workflow explains why Symphony will act a certain way.
4. Workers explains whether execution is healthy.
5. Sources explains what enters the system.
6. Settings explains environment and policy.

Metrics should be visible on the board, but they should not replace the board
as the first screen.

### Board Page

Routes:

- `/`
- `/board`

The Board page is the primary work surface. It should show kanban columns:

- backlog
- todo
- in progress
- in review
- done

Top controls and status:

- source sync status
- dry-run/live badge
- active workers
- todo count
- error count
- Sync now
- Run cycle now

Cards should represent Symphony work items once a source item has been
activated. Backlog cards may represent source items until activation.

Cards should show:

- repo and number
- source item type: issue, PR, or grouped issue plus PRs
- title
- task type when selected
- active PR when present
- CI/comment/conflict badges when present
- latest worker state
- review gate status

Interactions:

- Dragging backlog to todo opens activation unless the default is obvious.
- Dragging in_review to in_progress opens an optional reason selector.
- Clicking a card opens a detail drawer or navigates to the work item detail.
- Done should be harder to change accidentally than active columns.

### Sources Page

Route:

- `/sources`

The Sources page owns repository sync configuration and sync health.

It should include:

- source list
- add source form
- per-source filters
- include/exclude labels
- include/exclude authors
- stale item settings
- created/updated date ranges
- include drafts setting
- last sync status
- last sync error
- counts for open issues, open PRs, backlog items, and ignored items

Actions:

- Sync all
- Sync one source
- Edit filters
- Disable source

### Work Items Page

Routes:

- `/work-items`
- `/work-items/{work_item_id}`

The Work Items page is the searchable history and detail area for Symphony
work. It should include active, done, archived, and ignored work when filters
request it.

The detail page should show:

- linked source items
- active PR
- current kanban state
- task type and user hint
- selected rerun reasons
- worker runs
- workflow state
- artifacts
- operation summaries
- draft replies
- draft PR title/body
- PR health checks
- human gates
- timeline events

For issue plus multiple PRs, the detail view should show:

- grouped issue summary
- all linked PRs
- active PR selector
- each PR's draft, merge, CI, comment, and conflict state
- link provenance such as DB link, Symphony marker, created by Symphony, or
  user link

### Workers Page

Route:

- `/workers`

The Workers page owns execution health.

It should show:

- running workers
- work item
- worker id and pid
- heartbeat age
- runtime
- current primitive
- retry count
- recent crashes
- recent timeouts
- worker errors

Actions:

- stop worker
- retry work item
- inspect logs

### Workflow Page

Routes:

- `/workflow`
- `/workflow/edit`

The Workflow page owns user intent and workflow configuration.

It should include:

- state-machine visualization
- workflow editor
- accepted and rejected workflow versions
- validation errors
- primitive catalog
- setup steps
- worker preferences

The workflow editor should not be the homepage. It is important but less
frequent than board work.

### Ask Page

Route:

- `/ask`

Ask should remain simple:

- question input
- answer directly below the question
- links to source, work item, worker, workflow, and detail pages whenever
  possible
- recent useful prompts

Ask can later become a global command bar, but the initial implementation
should keep a dedicated page and inline answer components.

### Settings Page

Route:

- `/settings`

Settings owns lower-frequency operational configuration:

- GitHub app/auth status
- local/prod profile
- workspace roots
- dry-run/live mode
- worker limits
- default source filters
- dashboard preferences

## FastAPI Router Layout

FastAPI routers should mirror the dashboard hierarchy:

```text
symphony_dbcli/web/
  app.py
  dependencies.py
  routers/
    board.py
    sources.py
    work_items.py
    workers.py
    workflow.py
    ask.py
    settings.py
    api.py
  templates/
    board/
    sources/
    work_items/
    workers/
    workflow/
    ask/
    settings/
    partials/
  static/
```

Route ownership:

- `/` and `/board` belong to `board.py`.
- `/sources` belongs to `sources.py`.
- `/work-items` and `/work-items/{work_item_id}` belong to `work_items.py`.
- `/workers` belongs to `workers.py`.
- `/workflow` and `/workflow/edit` belong to `workflow.py`.
- `/ask` belongs to `ask.py`.
- `/settings` belongs to `settings.py`.
- `/api/...` is reserved for JSON endpoints that are genuinely API-shaped.

HTMX endpoints should live beside the page they update instead of being pushed
into a generic API router. Examples:

- `POST /board/cards/{card_id}/move`
- `GET /board/cards/{card_id}/activate-form`
- `POST /board/cards/{card_id}/activate`
- `GET /work-items/{work_item_id}/panel`
- `POST /work-items/{work_item_id}/move-to-in-progress`
- `POST /sources/{source_id}/sync`
- `POST /workflow/run-cycle`

This keeps page behavior close to its templates and avoids a split-brain
frontend/API model. JSON endpoints should be added only when a browser fragment
is the wrong shape for the interaction.

### Sources Page

The Sources page should allow:

- add GitHub repository source
- configure sync filters
- sync one source now
- sync all sources now
- show last sync status, duration, and errors
- show counts for open issues, open PRs, backlog items, and ignored items

### Kanban Board

The board should show:

- backlog
- todo
- in progress
- in review
- done

Cards should show:

- repo and number
- source item type: issue, PR, or grouped issue+PRs
- title
- task type when selected
- active PR when present
- CI/comment/conflict badges when present
- latest worker status
- review gate status

Drag interactions:

- backlog to todo opens activation modal unless defaultable
- in_review to in_progress opens optional reason selector
- done should be hard to move accidentally

### Work Item Detail Page

The detail page should show:

- linked source items
- active PR
- worker runs and attempts
- workflow state
- artifacts and operation summaries
- draft replies
- draft PR title/body
- PR health check results
- human gates
- timeline events

### Grouped Card Detail

For issue plus multiple PRs, the expanded view should show:

- issue summary
- all linked PRs
- active PR selector
- each PR's state, draft status, merge state, CI, comments, and latest action
- link provenance: DB link, Symphony marker, created by Symphony, user link

## Storage Sketch

The exact schema can evolve, but the first durable shape should include:

### `sources`

- `id`
- `provider`
- `repo`
- `status`
- `filters_json`
- `last_synced_at`
- `last_sync_error`
- `created_at`
- `updated_at`

### `source_sync_runs`

- `id`
- `source_id`
- `status`
- `started_at`
- `completed_at`
- `duration_ms`
- `fetched_issues`
- `fetched_pull_requests`
- `error`

### `source_items`

- `id`
- `source_id`
- `provider`
- `external_type` (`issue` or `pull_request`)
- `repo`
- `number`
- `url`
- `title`
- `body`
- `state`
- `author`
- `labels_json`
- `is_draft`
- `merged_at`
- `closed_at`
- `updated_at_external`
- `first_seen_at`
- `last_seen_at`
- `synced_at`
- `metadata_json`

Unique key: `(source_id, external_type, number)`.

### `work_items`

- `id`
- `state`
- `task_type`
- `title`
- `user_hint`
- `active_pr_source_item_id`
- `outcome`
- `created_by`
- `created_at`
- `updated_at`
- `completed_at`

### `work_item_links`

- `id`
- `work_item_id`
- `source_item_id`
- `relationship`
- `link_source`
- `created_at`

Relationships can include `primary_issue`, `active_pr`, `linked_pr`,
`source_pr`, and `related`.

### `issue_pull_request_links`

Keep or evolve the existing issue-PR table. It should reference source items
once source items exist.

Required fields:

- issue source item
- PR source item
- link source
- marker
- verified_at

### `work_item_runs`

This should either replace or wrap the current attempts table over time.

- `id`
- `work_item_id`
- `workflow_instance_id`
- `status`
- `reason_json`
- `hint`
- `worker_id`
- `started_at`
- `completed_at`
- `outcome`

## Workflow DSL Impact

Workflow transitions should be expressed around work item state and artifacts:

- primary identity: `work_item.id`
- linked issue artifacts
- linked PR artifacts
- active PR artifact
- user hint artifact
- selected rerun reasons artifact

Existing primitives can be adapted by adding work-item-aware input fields. New
source/work-item primitives are needed for source sync and kanban transitions.

Likely new primitives:

- `source.sync`
- `source.sync_all`
- `work_item.activate`
- `work_item.move`
- `work_item.link_source_item`
- `work_item.select_active_pr`
- `github.refresh_source_item`
- `github.refresh_linked_items`
- `codex.operations_task`

## Implementation Task List

- [x] Add source models and SQLite tables for `sources`, `source_sync_runs`,
  and `source_items`.
- [x] Implement GitHub source sync for open issues and PRs with default
  all-open behavior.
- [x] Add source filters for labels, authors, stale items, and date ranges.
- [x] Add Sources dashboard page with add source, sync now, and sync status.
- [x] Add source edit controls for filters and source settings.
- [x] Add work item tables, link tables, state history, and migration tests.
- [x] Build kanban board UI backed by `work_items`.
- [x] Implement backlog-to-todo activation modal with task type and optional
  user hint.
- [x] Default issue-with-linked-PR and PR cards to review/fix mode.
- [x] Implement in-review-to-in-progress reason selector with multi-select
  reasons.
- [x] Pivot orchestrator runtime identity from issue/attempt to work item.
- [x] Preserve existing attempt/worker history while introducing
  `work_item_runs`.
- [x] Adapt workflow context resolution to expose work item, linked issue,
  active PR, user hint, and rerun reasons as artifacts.
- [x] Add work-item-aware source refresh primitives.
- [x] Add operations task primitive and dashboard view for operation summaries.
- [x] Update PR creation to strongly link work item, issue, and PR in SQLite
  and PR body marker.
- [x] Group linked issue/PR cards in the board and add expandable detail view.
- [x] Support multiple linked PRs with one active PR selector.
- [x] Auto-mark work items done when linked PRs merge or linked issues close
  externally, recording outcome.
- [x] Add opt-in archive/ignore support for source items and work items.
- [x] Update Ask Symphony to answer source, work item, and board-state
  questions.
- [x] Build fast local tests for source sync, backlog-to-todo activation, and
  kanban state transitions.
- [x] Build fast local tests for grouped cards and active PR selection.
- [x] Build fast local tests for work item workflow execution.
- [x] Add GitHub-backed e2e smoke scenario for source sync through kanban
  activation.
- [x] Extend the GitHub-backed e2e scenario through PR review/fix workflow.
