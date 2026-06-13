from __future__ import annotations

import sqlite3

from .config import WorkflowConfig
from .review_actions import PullRequestSourceContext, pull_request_source_marker
from .store import ATTEMPT_ADJUSTMENT_RELATIONSHIP


def build_worker_prompt(
    config: WorkflowConfig,
    repo: str,
    issue_number: int,
    task_type: str,
    title: str,
    follow_up_context: str = "",
    task_context: str = "",
    primitive_guidance: list[str] | None = None,
    source_context: PullRequestSourceContext | None = None,
) -> str:
    context = source_context or PullRequestSourceContext(title=title)
    follow_up_section = f"\nFollow-up context:\n{follow_up_context}\n" if follow_up_context else ""
    task_context_section = f"\nTask context:\n{task_context}\n" if task_context else ""
    guidance_section = _guidance_section(primitive_guidance or [])
    code_pr_section = _code_pr_section(repo, issue_number, context) if task_type == "code" else ""
    research_response_section = _research_response_section(task_type)
    return f"""\
You are a Symphony worker for {repo}.

Task type: {task_type}
{_source_section(repo, issue_number, title, context)}
{follow_up_section}
{task_context_section}
{guidance_section}

Follow this workflow:
{config.instructions}

{research_response_section}
Before finishing, provide:
- a succinct work summary, no more than 5 bullets total
- tests or checks run, if any
- remaining risks or blockers
{code_pr_section}
"""


def build_pull_request_prompt(
    config: WorkflowConfig,
    repo: str,
    issue_number: int,
    title: str,
    *,
    worktree_path: str,
    branch: str,
    commit_sha: str,
    worker_result: str,
    issue_link_marker: str,
    primitive_guidance: list[str] | None = None,
    source_context: PullRequestSourceContext | None = None,
) -> str:
    context = source_context or PullRequestSourceContext(title=title)
    guidance_section = _guidance_section(primitive_guidance or [])
    return f"""\
Create a draft pull request for this completed Symphony code task.

Repository: {repo}
{_pull_request_source_section(repo, issue_number, title, issue_link_marker, context)}
Worktree: {worktree_path}
Branch: {branch}
Last recorded commit: {commit_sha or "unknown"}
{guidance_section}

Worker result:
{worker_result.strip() or "No worker result was recorded."}

Follow this workflow:
{config.instructions}

PR creation requirements:
- Inspect the final diff and commit any uncommitted code changes.
- Push the current branch to the GitHub repository.
- Create a draft pull request.
- Write a specific pull request title that names the actual code change, not just the issue number.
- Write a reviewable pull request description with concrete change details and tests or checks run.
- Do not make the pull request description only a closing issue line, source marker, or issue URL.
- {_pull_request_marker_requirement(repo, issue_number, issue_link_marker, context)}
- Do not create a second pull request if one already exists for this branch.
- Before finishing, print a line exactly in this form: Pull request: https://github.com/{repo}/pull/NUMBER
"""


def _guidance_section(items: list[str]) -> str:
    cleaned = [item.strip() for item in items if item.strip()]
    if not cleaned:
        return ""
    lines = "\n".join(f"- {item}" for item in cleaned)
    return f"""\
Primitive guidance:
{lines}
"""


def _research_response_section(task_type: str) -> str:
    if task_type != "research":
        return ""
    return """\
Research response requirements:
- Include the complete user-facing draft reply in your final response, preferably under a `Draft reply:` heading.
- Do not save the draft only to a filesystem path; dashboard users cannot access VM-local files.
- If you create local notes while investigating, still paste the complete draft reply in the final response.
"""


def _source_section(
    repo: str,
    issue_number: int,
    title: str,
    source_context: PullRequestSourceContext,
) -> str:
    if _is_internal_source(source_context):
        return f"""\
{_internal_source_prefix(source_context)}: {_internal_source_label(source_context)}
{_internal_source_title_label(source_context)}: {title}"""
    return f"""\
GitHub issue: https://github.com/{repo}/issues/{issue_number}
Issue title: {title}"""


def _pull_request_source_section(
    repo: str,
    issue_number: int,
    title: str,
    issue_link_marker: str,
    source_context: PullRequestSourceContext,
) -> str:
    if _is_internal_source(source_context):
        marker = pull_request_source_marker(repo, issue_number, source_context)
        return f"""\
{_internal_source_prefix(source_context)}: {_internal_source_label(source_context)}
{_internal_source_title_label(source_context)}: {title}
{_internal_source_marker_label(source_context)}: {marker}"""
    return f"""\
GitHub issue: https://github.com/{repo}/issues/{issue_number}
Issue title: {title}
Issue link marker: {issue_link_marker}"""


def _code_pr_section(repo: str, issue_number: int, source_context: PullRequestSourceContext) -> str:
    if _is_internal_source(source_context):
        marker = pull_request_source_marker(repo, issue_number, source_context)
        return f"""\
- a `PR title:` line that names the actual code change, not just the {_internal_source_kind_name(source_context)} number
- a `PR body:` section with `## Changes` and, when checks were run, `## Tests`
- at least one concrete change detail in the PR body; do not make it only a marker or generic summary
- the hidden Symphony {_internal_source_kind_name(source_context)} marker `{marker}` in the PR body
- no GitHub issue closing keyword unless a real GitHub issue is associated with the work
"""
    return f"""\
- a `PR title:` line that names the actual code change, not just the issue number
- a `PR body:` section with `## Changes` and, when checks were run, `## Tests`
- at least one concrete change detail in the PR body; do not make it only a `Fixes` line or issue URL
- the issue URL `https://github.com/{repo}/issues/{issue_number}` in the PR body
"""


def _pull_request_marker_requirement(
    repo: str,
    issue_number: int,
    issue_link_marker: str,
    source_context: PullRequestSourceContext,
) -> str:
    if _is_internal_source(source_context):
        marker = pull_request_source_marker(repo, issue_number, source_context)
        return (
            f"Include the hidden Symphony {_internal_source_kind_name(source_context)} marker "
            f"exactly as shown above in the pull request description: {marker}. Do not add a GitHub "
            f"issue URL or closing keyword for this {_internal_source_kind_name(source_context)}."
        )
    return (
        "Include the GitHub issue URL and the issue link marker exactly as shown above in the "
        f"pull request description: {issue_link_marker}."
    )


def _internal_source_heading(source_context: PullRequestSourceContext) -> str:
    return "Conversation" if source_context.kind == "conversation" else "Ticket"


def _internal_source_prefix(source_context: PullRequestSourceContext) -> str:
    return "Conversation" if source_context.kind == "conversation" else "Local ticket"


def _internal_source_title_label(source_context: PullRequestSourceContext) -> str:
    return "Conversation title" if source_context.kind == "conversation" else "Ticket title"


def _internal_source_marker_label(source_context: PullRequestSourceContext) -> str:
    return (
        "Symphony conversation marker" if source_context.kind == "conversation" else "Symphony ticket marker"
    )


def _internal_source_kind_name(source_context: PullRequestSourceContext) -> str:
    return "conversation" if source_context.kind == "conversation" else "ticket"


def _internal_source_label(source_context: PullRequestSourceContext) -> str:
    label = _internal_source_heading(source_context)
    if source_context.source_item_number is not None:
        return f"{label} #{source_context.source_item_number}"
    if source_context.source_item_id is not None:
        return f"{label} source item #{source_context.source_item_id}"
    return label


def _is_internal_source(source_context: PullRequestSourceContext) -> bool:
    return source_context.kind in {"local_ticket", "conversation"}


def format_follow_up_context(source_result: sqlite3.Row | None) -> str:
    if not source_result:
        return ""
    body = str(source_result["body"]).strip()
    if not body:
        return ""
    if str(source_result["relationship"]) == ATTEMPT_ADJUSTMENT_RELATIONSHIP:
        return f"""\
This task is a follow-up adjustment to attempt #{source_result["source_attempt_id"]}.
Use the prior attempt result as context, but keep the new changes focused on the operator hint.

Prior attempt result:
{body}
"""
    return f"""\
This code task was created from research attempt #{source_result["source_attempt_id"]}.
Use the research findings as implementation guidance, but verify them against the code before editing.

Research result:
{body}
"""


def result_type(task_type: str) -> str:
    if task_type == "operations":
        return "operations_summary"
    if task_type == "code":
        return "code_summary"
    return "research_answer"


def result_title(task_type: str) -> str:
    if task_type == "operations":
        return "Operations Summary"
    if task_type == "code":
        return "Code Worker Summary"
    return "Research Answer"
