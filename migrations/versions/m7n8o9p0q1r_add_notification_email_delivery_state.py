"""add notification email delivery state

Revision ID: m7n8o9p0q1r
Revises: l6m7n8o9p0q
Create Date: 2026-03-29 01:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "m7n8o9p0q1r"
down_revision = "l6m7n8o9p0q"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("notification_email_frequency_seconds", sa.Integer(), nullable=True, server_default="0"))
        batch_op.add_column(sa.Column("notification_email_last_sent_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE users SET notification_email_frequency_seconds = 0 WHERE notification_email_frequency_seconds IS NULL")
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("notification_email_frequency_seconds", nullable=False, server_default="0")

    with op.batch_alter_table("task_notifications") as batch_op:
        batch_op.add_column(sa.Column("emailed_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("task_notifications") as batch_op:
        batch_op.drop_column("emailed_at")

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("notification_email_last_sent_at")
        batch_op.drop_column("notification_email_frequency_seconds")
