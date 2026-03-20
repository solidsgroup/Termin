"""add collaborator task notification preferences

Revision ID: a0b1c2d3e4f5
Revises: 9e0f1a2b3c4d
Create Date: 2026-03-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a0b1c2d3e4f5"
down_revision = "9e0f1a2b3c4d"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    column_names = {column["name"] for column in inspector.get_columns("collaborator_profiles")}

    if "new_task_notification_preference" not in column_names:
        op.add_column(
            "collaborator_profiles",
            sa.Column("new_task_notification_preference", sa.String(length=24), nullable=False, server_default="email"),
        )
    if "update_notification_preference" not in column_names:
        op.add_column(
            "collaborator_profiles",
            sa.Column("update_notification_preference", sa.String(length=24), nullable=False, server_default="email"),
        )

    op.execute(
        "UPDATE collaborator_profiles "
        "SET new_task_notification_preference = COALESCE(notification_preference, 'email'), "
        "update_notification_preference = COALESCE(notification_preference, 'email')"
    )


def downgrade():
    op.drop_column("collaborator_profiles", "update_notification_preference")
    op.drop_column("collaborator_profiles", "new_task_notification_preference")
