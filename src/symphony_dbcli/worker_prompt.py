from __future__ import annotations

import sqlite3

from .config import WorkflowConfig


def build_worker_prompt(
    config: WorkflowConfig,
    repo: str,
    issue_number: int,
    task_type: str,
    title: str,
    follow_up_context: str = "",
) -> str:
    follow_up_section = f"\nFollow-up context:\n{follow_up_context}\n" if follow_up_context else ""
    return f"""\
You are a Symphony worker for {repo}.

Task type: {task_type}
GitHub issue: https://github.com/{repo}/issues/{issue_number}
Issue title: {title}
{follow_up_section}

Follow this workflow:
{config.instructions}

Before finishing, provide:
- a concise summary of what you did
- tests or checks run, if any
- remaining risks or blockers
"""


def format_follow_up_context(source_result: sqlite3.Row | None) -> str:
    if not source_result:
        return ""
    body = str(source_result["body"]).strip()
    if not body:
        return ""
    return f"""\
This code task was created from research attempt #{source_result["source_attempt_id"]}.
Use the research findings as implementation guidance, but verify them against the code before editing.

Research result:
{body}
"""


def result_type(task_type: str) -> str:
    if task_type == "code":
        return "code_summary"
    return "research_answer"


def result_title(task_type: str) -> str:
    if task_type == "code":
        return "Code Worker Summary"
    return "Research Answer"
