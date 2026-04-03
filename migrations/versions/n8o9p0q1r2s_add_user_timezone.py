"""add user timezone

Revision ID: n8o9p0q1r2s
Revises: m7n8o9p0q1r
Create Date: 2026-03-29 02:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "n8o9p0q1r2s"
down_revision = "m7n8o9p0q1r"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("timezone", sa.String(length=64), nullable=True, server_default="UTC"))
    op.execute("UPDATE users SET timezone = 'UTC' WHERE timezone IS NULL OR timezone = ''")
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("timezone", nullable=False, server_default="UTC")


def downgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("timezone")
