"""add collaborator notification preference

Revision ID: 9e0f1a2b3c4d
Revises: 8d9e0f1a2b3c
Create Date: 2026-03-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "9e0f1a2b3c4d"
down_revision = "8d9e0f1a2b3c"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    column_names = {column["name"] for column in inspector.get_columns("collaborator_profiles")}
    if "notification_preference" not in column_names:
        op.add_column(
            "collaborator_profiles",
            sa.Column("notification_preference", sa.String(length=24), nullable=False, server_default="email"),
        )
    op.execute(
        "UPDATE collaborator_profiles "
        "SET notification_preference = CASE "
        "WHEN default_calendar_opt_in = 1 THEN 'calendar' "
        "ELSE 'email' END"
    )
    if "notification_preference" not in column_names:
        op.alter_column("collaborator_profiles", "notification_preference", server_default=None)


def downgrade():
    op.drop_column("collaborator_profiles", "notification_preference")
