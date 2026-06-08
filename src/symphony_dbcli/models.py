from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import (
    Boolean,
    Engine,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    inspect,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


@dataclass(frozen=True)
class SQLiteColumnRepair:
    table_name: str
    column_name: str
    column_definition: str
    backfill_sql: str = ""


_SQLITE_COLUMN_REPAIRS = (
    SQLiteColumnRepair("work_items", "active_pr_source_item_id", "active_pr_source_item_id INTEGER"),
    SQLiteColumnRepair(
        "source_items",
        "disposition",
        "disposition VARCHAR(32)",
        "UPDATE source_items SET disposition = 'active' WHERE disposition IS NULL",
    ),
    SQLiteColumnRepair(
        "source_items",
        "disposition_note",
        "disposition_note TEXT",
        "UPDATE source_items SET disposition_note = '' WHERE disposition_note IS NULL",
    ),
    SQLiteColumnRepair("source_items", "disposition_at", "disposition_at VARCHAR(32)"),
    SQLiteColumnRepair(
        "work_items",
        "disposition",
        "disposition VARCHAR(32)",
        "UPDATE work_items SET disposition = 'active' WHERE disposition IS NULL",
    ),
    SQLiteColumnRepair(
        "work_items",
        "disposition_note",
        "disposition_note TEXT",
        "UPDATE work_items SET disposition_note = '' WHERE disposition_note IS NULL",
    ),
    SQLiteColumnRepair("work_items", "disposition_at", "disposition_at VARCHAR(32)"),
    SQLiteColumnRepair("work_item_runs", "attempt_id", "attempt_id INTEGER"),
    SQLiteColumnRepair("work_item_runs", "workflow_instance_id", "workflow_instance_id INTEGER"),
    SQLiteColumnRepair("work_item_runs", "source_attempt_id", "source_attempt_id INTEGER"),
)


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("repo", name="uq_sources_repo"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="github_repo")
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    filters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_status: Mapped[str] = mapped_column(String(32), nullable=False, default="never")
    last_synced_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)
    sync_runs: Mapped[list[SourceSyncRun]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
    )
    items: Mapped[list[SourceItem]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
    )


class SourceSyncRun(Base):
    __tablename__ = "source_sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    issue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pull_request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[str] = mapped_column(String(32), nullable=False)
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[Source] = relationship(back_populates="sync_runs")


class SourceItem(Base):
    __tablename__ = "source_items"
    __table_args__ = (
        UniqueConstraint("source_id", "kind", "number", name="uq_source_items_identity"),
        Index("ix_source_items_source_kind_state", "source_id", "kind", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    author: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    labels_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    github_updated_at: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    disposition: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    disposition_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    disposition_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    synced_at: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[Source] = relationship(back_populates="items")


class SourceItemLink(Base):
    __tablename__ = "source_item_links"
    __table_args__ = (
        UniqueConstraint(
            "source_item_id",
            "linked_source_item_id",
            "relationship",
            name="uq_source_item_links_identity",
        ),
        Index("ix_source_item_links_source", "source_id"),
        Index("ix_source_item_links_linked", "linked_source_item_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    source_item_id: Mapped[int] = mapped_column(
        ForeignKey("source_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    linked_source_item_id: Mapped[int] = mapped_column(
        ForeignKey("source_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    relationship: Mapped[str] = mapped_column(String(32), nullable=False)
    link_source: Mapped[str] = mapped_column(String(64), nullable=False)
    marker: Mapped[str] = mapped_column(Text, nullable=False, default="")
    verified_at: Mapped[str] = mapped_column(String(32), nullable=False)


class WorkItem(Base):
    __tablename__ = "work_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    primary_source_item_id: Mapped[int] = mapped_column(
        ForeignKey("source_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    active_pr_source_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    user_hint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    outcome: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    disposition: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    disposition_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    disposition_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class WorkItemLink(Base):
    __tablename__ = "work_item_links"
    __table_args__ = (
        UniqueConstraint(
            "work_item_id", "source_item_id", "relationship", name="uq_work_item_links_identity"
        ),
        Index("ix_work_item_links_source_item", "source_item_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_item_id: Mapped[int] = mapped_column(ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False)
    source_item_id: Mapped[int] = mapped_column(
        ForeignKey("source_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    relationship: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class WorkItemStateEvent(Base):
    __tablename__ = "work_item_state_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_item_id: Mapped[int] = mapped_column(ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False)
    from_state: Mapped[str] = mapped_column(String(32), nullable=False)
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class WorkItemRun(Base):
    __tablename__ = "work_item_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_item_id: Mapped[int] = mapped_column(ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False)
    attempt_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    workflow_instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_attempt_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    user_hint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


def create_model_tables(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    repair_sqlite_model_tables(engine)


def repair_sqlite_model_tables(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    column_names_by_table = {
        table_name: {column["name"] for column in inspector.get_columns(table_name)}
        for table_name in inspector.get_table_names()
    }
    with engine.begin() as connection:
        for repair in _SQLITE_COLUMN_REPAIRS:
            column_names = column_names_by_table.get(repair.table_name)
            if column_names is None:
                continue
            if repair.column_name not in column_names:
                connection.execute(
                    text(f"ALTER TABLE {repair.table_name} ADD COLUMN {repair.column_definition}")
                )
                column_names.add(repair.column_name)
            if repair.backfill_sql:
                connection.execute(text(repair.backfill_sql))
