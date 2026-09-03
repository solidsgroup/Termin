"""add Canvas specialty groups

Revision ID: c4d5e6f7g8h9
Revises: b3c4d5e6f7g8
Create Date: 2026-09-02 21:50:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "c4d5e6f7g8h9"
down_revision = "b3c4d5e6f7g8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("groups", schema=None) as batch_op:
        batch_op.add_column(sa.Column("specialty_type", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("specialty_source_url", sa.String(length=2048), nullable=True))
        batch_op.add_column(sa.Column("specialty_last_synced_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("specialty_last_sync_attempt_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("specialty_sync_error", sa.Text(), nullable=True))

    op.create_table(
        "canvas_assignment_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("canvas_assignment_id", sa.String(length=255), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "canvas_assignment_id", name="uq_canvas_assignment_links_group_assignment"),
        sa.UniqueConstraint("task_id"),
    )


def downgrade() -> None:
    op.drop_table("canvas_assignment_links")
    with op.batch_alter_table("groups", schema=None) as batch_op:
        batch_op.drop_column("specialty_sync_error")
        batch_op.drop_column("specialty_last_sync_attempt_at")
        batch_op.drop_column("specialty_last_synced_at")
        batch_op.drop_column("specialty_source_url")
        batch_op.drop_column("specialty_type")
