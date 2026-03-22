"""drop subtask schema

Revision ID: d5e6f7a8b9c0
Revises: 2b3c4d5e6f7a, c1d2e3f4a5b6
Create Date: 2026-03-22 16:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "d5e6f7a8b9c0"
down_revision = ("2b3c4d5e6f7a", "c1d2e3f4a5b6")
branch_labels = None
depends_on = None


def _table_columns(inspector, table_name):
    try:
        return {column["name"] for column in inspector.get_columns(table_name)}
    except Exception:
        return set()


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "assignments" in table_names and "subtask_id" in _table_columns(inspector, "assignments"):
        with op.batch_alter_table("assignments") as batch_op:
            batch_op.drop_column("subtask_id")

    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "invites" in table_names and "subtask_id" in _table_columns(inspector, "invites"):
        with op.batch_alter_table("invites") as batch_op:
            batch_op.drop_column("subtask_id")

    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "subtasks" in table_names:
        op.drop_table("subtasks")


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "subtasks" not in table_names:
        op.create_table(
            "subtasks",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("task_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("info", sa.Text(), nullable=True),
            sa.Column("link", sa.String(length=1024), nullable=True),
            sa.Column("due_at", sa.DateTime(), nullable=True),
            sa.Column("assignee_email", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=50), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    inspector = sa.inspect(bind)
    if "assignments" in set(inspector.get_table_names()) and "subtask_id" not in _table_columns(inspector, "assignments"):
        with op.batch_alter_table("assignments") as batch_op:
            batch_op.add_column(sa.Column("subtask_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key("fk_assignments_subtask_id_subtasks", "subtasks", ["subtask_id"], ["id"])

    inspector = sa.inspect(bind)
    if "invites" in set(inspector.get_table_names()) and "subtask_id" not in _table_columns(inspector, "invites"):
        with op.batch_alter_table("invites") as batch_op:
            batch_op.add_column(sa.Column("subtask_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key("fk_invites_subtask_id_subtasks", "subtasks", ["subtask_id"], ["id"])
