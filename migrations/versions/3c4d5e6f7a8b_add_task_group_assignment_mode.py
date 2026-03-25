"""add task group assignment mode

Revision ID: 3c4d5e6f7a8b
Revises: 2d3e4f5a6b7c
Create Date: 2026-03-23 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "3c4d5e6f7a8b"
down_revision = "2d3e4f5a6b7c"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {col["name"] for col in inspector.get_columns("tasks")}
    if "assign_group_members" not in columns:
        op.add_column("tasks", sa.Column("assign_group_members", sa.Boolean(), nullable=False, server_default=sa.false()))
        op.alter_column("tasks", "assign_group_members", server_default=None)


def downgrade():
    op.drop_column("tasks", "assign_group_members")
