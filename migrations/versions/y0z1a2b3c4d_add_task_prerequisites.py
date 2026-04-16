"""add task prerequisites

Revision ID: y0z1a2b3c4d
Revises: x9y0z1a2b3c
Create Date: 2026-04-15 15:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "y0z1a2b3c4d"
down_revision = "x9y0z1a2b3c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_prerequisites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("prerequisite_task_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["prerequisite_task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "prerequisite_task_id", name="uq_task_prerequisites_task_prerequisite"),
    )
    op.create_index("ix_task_prerequisites_task_id", "task_prerequisites", ["task_id"], unique=False)
    op.create_index(
        "ix_task_prerequisites_prerequisite_task_id",
        "task_prerequisites",
        ["prerequisite_task_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_prerequisites_prerequisite_task_id", table_name="task_prerequisites")
    op.drop_index("ix_task_prerequisites_task_id", table_name="task_prerequisites")
    op.drop_table("task_prerequisites")
