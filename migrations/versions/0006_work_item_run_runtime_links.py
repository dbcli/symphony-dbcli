"""add work item run runtime links

Revision ID: 0006_work_item_run_runtime_links
Revises: 0005_dispositions
Create Date: 2026-05-25 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0006_work_item_run_runtime_links"
down_revision: str | None = "0005_dispositions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "work_item_runs" not in set(inspector.get_table_names()):
        return
    _add_column_if_missing(inspector, "work_item_runs", sa.Column("attempt_id", sa.Integer(), nullable=True))
    _add_column_if_missing(
        inspector,
        "work_item_runs",
        sa.Column("workflow_instance_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "work_item_runs" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("work_item_runs")}
    for column_name in ("workflow_instance_id", "attempt_id"):
        if column_name in columns:
            op.drop_column("work_item_runs", column_name)


def _add_column_if_missing(inspector: sa.Inspector, table_name: str, column: sa.Column[Any]) -> None:
    columns = {existing["name"] for existing in inspector.get_columns(table_name)}
    if column.name not in columns:
        op.add_column(table_name, column)
