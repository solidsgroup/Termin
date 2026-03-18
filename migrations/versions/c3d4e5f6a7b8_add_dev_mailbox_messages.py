"""add dev mailbox messages

Revision ID: c3d4e5f6a7b8
Revises: b7c8d9e0f1a2
Create Date: 2026-03-17 18:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "c3d4e5f6a7b8"
down_revision = "ae1b90b08d51"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "dev_mailbox_messages" in inspector.get_table_names():
        return
    op.create_table(
        "dev_mailbox_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("to_email", sa.String(length=255), nullable=False),
        sa.Column("from_email", sa.String(length=255), nullable=False),
        sa.Column("from_name", sa.String(length=255), nullable=True),
        sa.Column("reply_to", sa.String(length=255), nullable=True),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("text_body", sa.Text(), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=True),
        sa.Column("delivery_mode", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("dev_mailbox_messages")
