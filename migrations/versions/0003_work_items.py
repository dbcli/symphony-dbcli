"""add work item tables

Revision ID: 0003_work_items
Revises: 0002_source_sync
Create Date: 2026-05-25 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_work_items"
down_revision: str | None = "0002_source_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "work_items" not in tables:
        op.create_table(
            "work_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("source_id", sa.Integer(), nullable=False),
            sa.Column("primary_source_item_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=500), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("task_type", sa.String(length=32), nullable=False),
            sa.Column("user_hint", sa.Text(), nullable=False),
            sa.Column("outcome", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.String(length=32), nullable=False),
            sa.Column("updated_at", sa.String(length=32), nullable=False),
            sa.ForeignKeyConstraint(["primary_source_item_id"], ["source_items.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    if "work_item_links" not in tables:
        op.create_table(
            "work_item_links",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("work_item_id", sa.Integer(), nullable=False),
            sa.Column("source_item_id", sa.Integer(), nullable=False),
            sa.Column("relationship", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.String(length=32), nullable=False),
            sa.ForeignKeyConstraint(["source_item_id"], ["source_items.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "work_item_id",
                "source_item_id",
                "relationship",
                name="uq_work_item_links_identity",
            ),
        )
        op.create_index("ix_work_item_links_source_item", "work_item_links", ["source_item_id"])
    if "work_item_state_events" not in tables:
        op.create_table(
            "work_item_state_events",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("work_item_id", sa.Integer(), nullable=False),
            sa.Column("from_state", sa.String(length=32), nullable=False),
            sa.Column("to_state", sa.String(length=32), nullable=False),
            sa.Column("reasons_json", sa.Text(), nullable=False),
            sa.Column("note", sa.Text(), nullable=False),
            sa.Column("created_at", sa.String(length=32), nullable=False),
            sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    if "work_item_runs" not in tables:
        op.create_table(
            "work_item_runs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("work_item_id", sa.Integer(), nullable=False),
            sa.Column("task_type", sa.String(length=32), nullable=False),
            sa.Column("trigger", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("reasons_json", sa.Text(), nullable=False),
            sa.Column("user_hint", sa.Text(), nullable=False),
            sa.Column("started_at", sa.String(length=32), nullable=True),
            sa.Column("completed_at", sa.String(length=32), nullable=True),
            sa.Column("created_at", sa.String(length=32), nullable=False),
            sa.Column("updated_at", sa.String(length=32), nullable=False),
            sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "work_item_runs" in tables:
        op.drop_table("work_item_runs")
    if "work_item_state_events" in tables:
        op.drop_table("work_item_state_events")
    if "work_item_links" in tables:
        op.drop_index("ix_work_item_links_source_item", table_name="work_item_links")
        op.drop_table("work_item_links")
    if "work_items" in tables:
        op.drop_table("work_items")
