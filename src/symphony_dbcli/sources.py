from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .clock import utc_now
from .db import SessionFactory
from .models import Source

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class SourceValidationError(ValueError):
    """Raised when a source cannot be created from user input."""


@dataclass(frozen=True)
class SourceCreate:
    repo: str


@dataclass(frozen=True)
class SourceView:
    id: int
    kind: str
    repo: str
    display_name: str
    enabled: bool
    sync_status: str
    last_synced_at: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_model(cls, source: Source) -> SourceView:
        return cls(
            id=source.id,
            kind=source.kind,
            repo=source.repo,
            display_name=source.display_name,
            enabled=source.enabled,
            sync_status=source.sync_status,
            last_synced_at=source.last_synced_at,
            created_at=source.created_at,
            updated_at=source.updated_at,
        )


class SourceRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def list_sources(self) -> list[SourceView]:
        with self._session_factory() as session:
            rows = session.scalars(select(Source).order_by(Source.repo.asc())).all()
            return [SourceView.from_model(row) for row in rows]

    def get_source(self, source_id: int) -> SourceView | None:
        with self._session_factory() as session:
            source = session.get(Source, source_id)
            return SourceView.from_model(source) if source else None

    def create_source(self, source: SourceCreate) -> SourceView:
        repo = normalize_repo(source.repo)
        now = utc_now()
        model = Source(
            kind="github_repo",
            repo=repo,
            display_name=repo,
            filters_json="{}",
            enabled=True,
            sync_status="never",
            created_at=now,
            updated_at=now,
        )
        with self._session_factory() as session:
            session.add(model)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.scalar(select(Source).where(Source.repo == repo))
                if existing is None:
                    raise
                return SourceView.from_model(existing)
            session.refresh(model)
            return SourceView.from_model(model)


def normalize_repo(repo: str) -> str:
    normalized = repo.strip()
    if not REPO_RE.match(normalized):
        raise SourceValidationError("Use a GitHub repository in owner/name format.")
    return normalized
