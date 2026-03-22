"""add dev mailbox transport logging

Revision ID: 2b3c4d5e6f7a
Revises: b1c2d3e4f5a, 1c2d3e4f5a6b
Create Date: 2026-03-20 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "2b3c4d5e6f7a"
down_revision = ("b1c2d3e4f5a", "1c2d3e4f5a6b")
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("dev_mailbox_messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("transport_status", sa.String(length=32), nullable=False, server_default="captured"))
        batch_op.add_column(sa.Column("provider_message_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("transport_details", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("smtp_host", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("smtp_port", sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table("dev_mailbox_messages", schema=None) as batch_op:
        batch_op.drop_column("smtp_port")
        batch_op.drop_column("smtp_host")
        batch_op.drop_column("transport_details")
        batch_op.drop_column("provider_message_id")
        batch_op.drop_column("transport_status")
