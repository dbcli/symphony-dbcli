# AGENTS.md

## Project Style

This project should feel like a careful Python package, not a script dump.

- Use `uv` and `pyproject.toml` as the package and tooling source of truth.
- Keep modules small, cohesive, and named after their responsibility.
- Prefer boring, explicit interfaces over clever abstractions.
- Write code that is easy to review: short functions, clear names, and direct
  data flow.
- Do not add framework or service dependencies unless they remove real
  complexity.

## Typed Python

Use clean typed Python throughout the project.

- Put type boundaries at external inputs: config parsing, HTTP/API responses,
  CLI args, database rows, subprocess output, and template context.
- Convert untyped data into typed dataclasses or narrow internal structures
  before passing it deeper into the system.
- Trust the types inside the core logic instead of sprinkling `isinstance()`
  checks throughout the code.
- Avoid `Any` except at true boundaries where data is coming from TOML, JSON,
  SQLite, or third-party protocols.
- Prefer `Literal`, dataclasses, and small value objects for domain concepts
  such as task type, attempt status, worker metrics, and workflow versions.
- Keep return types explicit for public functions and methods.

## Control Flow

- Limit branching so the code remains readable.
- Prefer early returns for invalid or terminal cases.
- Keep validation near the boundary and keep business logic straightforward.
- Avoid broad catch-all exception handling except around long-running service
  loops where the process must stay alive.

## Tests

- Do not add tests whose purpose is to assert that a removed feature or old
  implementation detail is absent. Remove obsolete coverage instead, and keep
  tests focused on the intended current behavior.

## Dashboard UI

- Use Jinja templates for HTML.
- Keep CSS in static `.css` files, not embedded in templates or Python strings.
- Keep the dashboard operational and dense: status, queues, timing, attempts,
  worker health, and issue details should be easy to scan.
- Avoid marketing-style layouts, decorative flourishes, and oversized hero UI.

## Quality Gates

Before considering work complete, run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

The repo uses pre-commit with Ruff:

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```
