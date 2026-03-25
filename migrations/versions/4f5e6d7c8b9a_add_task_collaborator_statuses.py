"""add task collaborator statuses

Revision ID: 4f5e6d7c8b9a
Revises: 3c4d5e6f7a8b
Create Date: 2026-03-25 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "4f5e6d7c8b9a"
down_revision = "3c4d5e6f7a8b"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "task_collaborator_statuses" in tables:
        return
    op.create_table(
        "task_collaborator_statuses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "email", name="uq_task_collaborator_statuses_task_email"),
    )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "task_collaborator_statuses" in set(inspector.get_table_names()):
        op.drop_table("task_collaborator_statuses")
