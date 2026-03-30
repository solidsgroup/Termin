"""Add description fields for projects, groups, and tasks

Revision ID: g1h2i3j4k5l
Revises: f9a0b1c2d3e4
Create Date: 2026-03-29 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "g1h2i3j4k5l"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("description", sa.Text(), nullable=True))
    op.add_column(
        "projects",
        sa.Column("description_format", sa.String(length=32), nullable=False, server_default="markdown"),
    )
    op.add_column("groups", sa.Column("description", sa.Text(), nullable=True))
    op.add_column(
        "groups",
        sa.Column("description_format", sa.String(length=32), nullable=False, server_default="markdown"),
    )
    op.add_column(
        "tasks",
        sa.Column("description_format", sa.String(length=32), nullable=False, server_default="plain"),
    )


def downgrade() -> None:
    op.drop_column("tasks", "description_format")
    op.drop_column("groups", "description_format")
    op.drop_column("groups", "description")
    op.drop_column("projects", "description_format")
    op.drop_column("projects", "description")
