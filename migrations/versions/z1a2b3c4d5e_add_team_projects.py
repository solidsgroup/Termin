"""add team project flag

Revision ID: z1a2b3c4d5e
Revises: y0z1a2b3c4d
Create Date: 2026-06-12 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "z1a2b3c4d5e"
down_revision = "y0z1a2b3c4d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("is_team", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("is_team")
