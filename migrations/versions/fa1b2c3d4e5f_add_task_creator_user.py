"""add task creator user

Revision ID: fa1b2c3d4e5f
Revises: f9a0b1c2d3e4
Create Date: 2026-03-27 00:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "fa1b2c3d4e5f"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    if "creator_user_id" not in task_columns:
        op.add_column("tasks", sa.Column("creator_user_id", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE tasks
        SET creator_user_id = (
            SELECT projects.owner_id
            FROM projects
            WHERE projects.id = tasks.project_id
        )
        WHERE creator_user_id IS NULL
        """
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    if "creator_user_id" in task_columns:
        op.drop_column("tasks", "creator_user_id")
