from __future__ import annotations

import re

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .models import SourceItem

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def ensure_source_item_search_schema(session: Session) -> None:
    session.execute(
        text(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS source_item_search
            USING fts5(source_item_id UNINDEXED, source_id UNINDEXED, title, body)
            """
        )
    )


def rebuild_source_item_search(session: Session, source_id: int) -> None:
    delete_source_item_search(session, source_id)
    rows = [
        {
            "source_item_id": item.id,
            "source_id": item.source_id,
            "title": item.title,
            "body": item.body,
        }
        for item in session.scalars(select(SourceItem).where(SourceItem.source_id == source_id))
    ]
    if rows:
        session.execute(
            text(
                """
                INSERT INTO source_item_search(source_item_id, source_id, title, body)
                VALUES(:source_item_id, :source_id, :title, :body)
                """
            ),
            rows,
        )


def delete_source_item_search(session: Session, source_id: int) -> None:
    ensure_source_item_search_schema(session)
    session.execute(
        text("DELETE FROM source_item_search WHERE source_id = :source_id"),
        {"source_id": source_id},
    )


def matching_source_item_ids(session: Session, source_id: int, query: str) -> set[int]:
    fts_query = _fts_prefix_query(query)
    if not fts_query:
        return set()
    rebuild_source_item_search(session, source_id)
    rows = session.execute(
        text(
            """
            SELECT source_item_id
            FROM source_item_search
            WHERE source_id = :source_id
              AND source_item_search MATCH :query
            """
        ),
        {"source_id": source_id, "query": fts_query},
    ).all()
    return {int(row[0]) for row in rows}


def _fts_prefix_query(query: str) -> str:
    tokens = [token for token in _TOKEN_RE.findall(query.lower()) if token]
    return " ".join(_quoted_prefix(token) for token in tokens)


def _quoted_prefix(token: str) -> str:
    escaped = token.replace('"', '""')
    return f'"{escaped}"*'
