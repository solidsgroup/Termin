"""add user notification preferences

Revision ID: j4k5l6m7n8o
Revises: i3j4k5l6m7n
Create Date: 2026-03-29 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "j4k5l6m7n8o"
down_revision = "i3j4k5l6m7n"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_notification_preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column("push_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("email_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "event_key", name="uq_user_notification_preferences_user_event"),
    )


def downgrade():
    op.drop_table("user_notification_preferences")
