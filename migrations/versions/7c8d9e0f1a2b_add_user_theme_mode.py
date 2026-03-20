"""add user theme mode

Revision ID: 7c8d9e0f1a2b
Revises: 6b7c8d9e0f1a
Create Date: 2026-03-19 18:45:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "7c8d9e0f1a2b"
down_revision = "6b7c8d9e0f1a"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("theme_mode", sa.String(length=16), nullable=False, server_default="dark"))


def downgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("theme_mode")
