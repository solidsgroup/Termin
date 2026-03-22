"""add task notification pins

Revision ID: 1c2d3e4f5a6b
Revises: 0a1b2c3d4e5f
Create Date: 2026-03-19 16:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "1c2d3e4f5a6b"
down_revision = "0a1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("task_notifications", schema=None) as batch_op:
        batch_op.add_column(sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()))

    with op.batch_alter_table("task_notifications", schema=None) as batch_op:
        batch_op.alter_column("pinned", server_default=None)


def downgrade():
    with op.batch_alter_table("task_notifications", schema=None) as batch_op:
        batch_op.drop_column("pinned")
