"""add collaborator task reads

Revision ID: 6b7c8d9e0f1a
Revises: 5f6a7b8c9d0e
Create Date: 2026-03-19 18:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "6b7c8d9e0f1a"
down_revision = "5f6a7b8c9d0e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "collaborator_task_reads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("collaborator_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("last_read_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["collaborator_id"], ["collaborator_profiles.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("collaborator_id", "task_id", name="uq_collaborator_task_reads_collaborator_task"),
    )


def downgrade():
    op.drop_table("collaborator_task_reads")
