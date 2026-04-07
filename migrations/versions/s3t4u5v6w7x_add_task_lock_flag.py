"""add task lock flag

Revision ID: s3t4u5v6w7x
Revises: r2s3t4u5v6w
Create Date: 2026-03-31 14:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "s3t4u5v6w7x"
down_revision = "r2s3t4u5v6w"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("locked")
