"""add task followers

Revision ID: k5l6m7n8o9p
Revises: j4k5l6m7n8o
Create Date: 2026-03-29 00:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "k5l6m7n8o9p"
down_revision = "j4k5l6m7n8o"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_followers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "user_id", name="uq_task_followers_task_user"),
    )


def downgrade():
    op.drop_table("task_followers")
