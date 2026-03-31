"""add task notification payloads

Revision ID: h2i3j4k5l6m
Revises: fa1b2c3d4e5f, g1h2i3j4k5l
Create Date: 2026-03-29 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "h2i3j4k5l6m"
down_revision = ("fa1b2c3d4e5f", "g1h2i3j4k5l")
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    try:
        return column_name in {col["name"] for col in inspector.get_columns(table_name)}
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "task_notifications" not in inspector.get_table_names():
        return
    with op.batch_alter_table("task_notifications", schema=None) as batch_op:
        if not _has_column(inspector, "task_notifications", "notification_data"):
            batch_op.add_column(sa.Column("notification_data", sa.Text(), nullable=True))
        batch_op.alter_column("task_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "task_notifications" not in inspector.get_table_names():
        return
    with op.batch_alter_table("task_notifications", schema=None) as batch_op:
        batch_op.alter_column("task_id", existing_type=sa.Integer(), nullable=False)
        if _has_column(inspector, "task_notifications", "notification_data"):
            batch_op.drop_column("notification_data")
