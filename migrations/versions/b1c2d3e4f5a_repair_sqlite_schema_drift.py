"""repair sqlite schema drift

Revision ID: b1c2d3e4f5a
Revises: a0b1c2d3e4f5
Create Date: 2026-03-20 00:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "b1c2d3e4f5a"
down_revision = "a0b1c2d3e4f5"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    subtask_columns = {column["name"] for column in inspector.get_columns("subtasks")}

    if "position" not in task_columns:
        op.add_column("tasks", sa.Column("position", sa.Integer(), nullable=False, server_default="0"))

    if "info" not in task_columns:
        op.add_column("tasks", sa.Column("info", sa.Text(), nullable=True))

    if "info" not in subtask_columns:
        op.add_column("subtasks", sa.Column("info", sa.Text(), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    subtask_columns = {column["name"] for column in inspector.get_columns("subtasks")}

    if "info" in subtask_columns:
        op.drop_column("subtasks", "info")

    if "info" in task_columns:
        op.drop_column("tasks", "info")

    if "position" in task_columns:
        op.drop_column("tasks", "position")
