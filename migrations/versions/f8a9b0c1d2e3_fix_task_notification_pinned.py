"""fix task notification pinned

Revision ID: f8a9b0c1d2e3
Revises: e6f7a8b9c0d1
Create Date: 2026-03-26 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "f8a9b0c1d2e3"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    try:
        return column_name in {col["name"] for col in inspector.get_columns(table_name)}
    except Exception:
        return False


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "task_notifications" not in inspector.get_table_names():
        return
    if _has_column(inspector, "task_notifications", "pinned"):
        return
    with op.batch_alter_table("task_notifications", schema=None) as batch_op:
        batch_op.add_column(sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table("task_notifications", schema=None) as batch_op:
        batch_op.alter_column("pinned", server_default=None)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "task_notifications" not in inspector.get_table_names():
        return
    if not _has_column(inspector, "task_notifications", "pinned"):
        return
    with op.batch_alter_table("task_notifications", schema=None) as batch_op:
        batch_op.drop_column("pinned")
