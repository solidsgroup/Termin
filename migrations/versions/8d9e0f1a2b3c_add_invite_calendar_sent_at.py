"""add invite calendar sent at

Revision ID: 8d9e0f1a2b3c
Revises: 7c8d9e0f1a2b
Create Date: 2026-03-20 10:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "8d9e0f1a2b3c"
down_revision = "7c8d9e0f1a2b"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("invites", schema=None) as batch_op:
        batch_op.add_column(sa.Column("calendar_invite_sent_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("invites", schema=None) as batch_op:
        batch_op.drop_column("calendar_invite_sent_at")
