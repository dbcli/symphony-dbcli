from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

SUMMARY_MARKER_RE = re.compile(r"\*\*\s*summary\s*\*\*|^summary:\s*", re.IGNORECASE | re.MULTILINE)
PROGRESS_SENTENCE_RE = re.compile(r"(?<=[.!?])\s*(?=(?:[`A-Z]|\*\*|I[’']))")
HEADING_RE = re.compile(r"^\*\*(?P<bold>[^*]{1,80})\*\*:?\s*(?P<bold_rest>.*)$")
NAMED_SECTION_RE = re.compile(
    r"^(?P<title>Summary|Checks run|Verification|Risks/blockers|Risks|Blockers|Notes|Result):\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
BULLET_RE = re.compile(r"^[-*]\s+(?P<body>.+)$")
ResultSectionKind = Literal["summary", "section"]


@dataclass(frozen=True)
class FormattedResult:
    updates: list[str]
    sections: list[ResultSection]
    has_summary: bool


@dataclass(frozen=True)
class ResultSection:
    title: str
    kind: ResultSectionKind
    paragraphs: list[str]
    bullets: list[str]


def format_result_body(body: str) -> FormattedResult:
    stripped = body.strip()
    if not stripped:
        return FormattedResult(updates=[], sections=[], has_summary=False)
    summary_match = SUMMARY_MARKER_RE.search(stripped)
    if summary_match is None:
        return FormattedResult(
            updates=[],
            sections=_result_sections(stripped, default_title="Result", first_kind="section"),
            has_summary=False,
        )
    progress_text = stripped[: summary_match.start()].strip()
    summary_text = stripped[summary_match.end() :].strip()
    return FormattedResult(
        updates=_progress_updates(progress_text),
        sections=_result_sections(summary_text, default_title="Summary", first_kind="summary"),
        has_summary=True,
    )


def _progress_updates(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", stripped) if paragraph.strip()]
    if len(paragraphs) > 1:
        return paragraphs
    sentences = [sentence.strip() for sentence in PROGRESS_SENTENCE_RE.split(stripped) if sentence.strip()]
    if len(sentences) <= 1:
        return [stripped]
    updates: list[str] = []
    current: list[str] = []
    for sentence in sentences:
        current.append(sentence)
        current_text = " ".join(current)
        if len(current) >= 2 or len(current_text) >= 260:
            updates.append(current_text)
            current = []
    if current:
        updates.append(" ".join(current))
    return updates


def _result_sections(
    text: str,
    *,
    default_title: str,
    first_kind: ResultSectionKind,
) -> list[ResultSection]:
    current_title = default_title
    current_kind = first_kind
    paragraphs: list[str] = []
    bullets: list[str] = []
    paragraph_lines: list[str] = []
    sections: list[ResultSection] = []

    def flush_paragraph() -> None:
        if paragraph_lines:
            paragraphs.append(" ".join(paragraph_lines).strip())
            paragraph_lines.clear()

    def flush_section() -> None:
        flush_paragraph()
        if paragraphs or bullets:
            sections.append(
                ResultSection(
                    title=current_title,
                    kind=current_kind,
                    paragraphs=list(paragraphs),
                    bullets=list(bullets),
                )
            )
        paragraphs.clear()
        bullets.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        heading = _result_heading(line)
        if heading is not None:
            title, rest = heading
            flush_section()
            current_title = title
            current_kind = "summary" if title.lower() == "summary" else "section"
            if rest:
                paragraph_lines.append(rest)
            continue
        bullet = BULLET_RE.match(line)
        if bullet:
            flush_paragraph()
            bullets.append(bullet.group("body").strip())
            continue
        paragraph_lines.append(line)

    flush_section()
    if sections:
        return sections
    return [
        ResultSection(
            title=default_title,
            kind=first_kind,
            paragraphs=[text.strip()],
            bullets=[],
        )
    ]


def _result_heading(line: str) -> tuple[str, str] | None:
    bold_heading = HEADING_RE.match(line)
    if bold_heading is not None:
        title = bold_heading.group("bold").strip().rstrip(":")
        return title, bold_heading.group("bold_rest").strip()
    named_heading = NAMED_SECTION_RE.match(line)
    if named_heading is not None:
        return _section_title(named_heading.group("title")), named_heading.group("rest").strip()
    return None


def _section_title(value: str) -> str:
    if value.lower() == "risks/blockers":
        return "Risks/blockers"
    return value[:1].upper() + value[1:].lower()
