"""add scope to user notification preferences

Revision ID: l6m7n8o9p0q
Revises: k5l6m7n8o9p
Create Date: 2026-03-29 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "l6m7n8o9p0q"
down_revision = "k5l6m7n8o9p"
branch_labels = None
depends_on = None


TASK_EVENT_KEYS = (
    "task_message",
    "task_assigned",
    "task_completed",
    "task_due_changed",
    "task_status_changed",
    "task_renamed",
    "task_moved",
    "task_created",
    "task_updated",
)


def upgrade():
    with op.batch_alter_table("user_notification_preferences") as batch_op:
        batch_op.add_column(sa.Column("scope_key", sa.String(length=32), nullable=True))

    op.execute("UPDATE user_notification_preferences SET scope_key = 'default' WHERE scope_key IS NULL")
    for event_key in TASK_EVENT_KEYS:
        op.execute(
            "UPDATE user_notification_preferences SET scope_key = 'all' "
            f"WHERE event_key = '{event_key}' AND scope_key = 'default'"
        )

    with op.batch_alter_table("user_notification_preferences") as batch_op:
        batch_op.alter_column("scope_key", nullable=False, server_default="default")
        batch_op.drop_constraint("uq_user_notification_preferences_user_event", type_="unique")
        batch_op.create_unique_constraint(
            "uq_user_notification_preferences_user_event_scope",
            ["user_id", "event_key", "scope_key"],
        )


def downgrade():
    with op.batch_alter_table("user_notification_preferences") as batch_op:
        batch_op.drop_constraint("uq_user_notification_preferences_user_event_scope", type_="unique")
        batch_op.create_unique_constraint(
            "uq_user_notification_preferences_user_event",
            ["user_id", "event_key"],
        )
        batch_op.drop_column("scope_key")
