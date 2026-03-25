"""add per-user task statuses

Revision ID: 2d3e4f5a6b7c
Revises: f8a9b0c1d2e3
Create Date: 2026-03-24 00:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "2d3e4f5a6b7c"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "tasks" in inspector.get_table_names():
        task_columns = {column["name"] for column in inspector.get_columns("tasks")}
        if "per_user_status_enabled" not in task_columns:
            with op.batch_alter_table("tasks", schema=None) as batch_op:
                batch_op.add_column(sa.Column("per_user_status_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
            with op.batch_alter_table("tasks", schema=None) as batch_op:
                batch_op.alter_column("per_user_status_enabled", server_default=None)

    if "task_user_statuses" not in inspector.get_table_names():
        op.create_table(
            "task_user_statuses",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("task_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=50), nullable=False, server_default="open"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("task_id", "user_id", name="uq_task_user_statuses_task_user"),
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "task_user_statuses" in inspector.get_table_names():
        op.drop_table("task_user_statuses")

    if "tasks" in inspector.get_table_names():
        task_columns = {column["name"] for column in inspector.get_columns("tasks")}
        if "per_user_status_enabled" in task_columns:
            with op.batch_alter_table("tasks", schema=None) as batch_op:
                batch_op.drop_column("per_user_status_enabled")
