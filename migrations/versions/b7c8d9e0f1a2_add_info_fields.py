"""add info fields

Revision ID: b7c8d9e0f1a2
Revises: 6a7b8c9d0e1f
Create Date: 2026-03-17 16:10:00.000000
"""

from alembic import op
import sqlalchemy as sa
import json


revision = "b7c8d9e0f1a2"
down_revision = "6a7b8c9d0e1f"
branch_labels = None
depends_on = None


def _info_from_link(link):
    if not link:
        return None
    return json.dumps(
        {
            "html": f'<p><a href="{link}">{link}</a></p>',
            "attachments": [],
        },
        separators=(",", ":"),
    )


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    subtask_columns = {column["name"] for column in inspector.get_columns("subtasks")}

    if "info" not in task_columns:
        op.add_column("tasks", sa.Column("info", sa.Text(), nullable=True))
    if "info" not in subtask_columns:
        op.add_column("subtasks", sa.Column("info", sa.Text(), nullable=True))

    metadata = sa.MetaData()
    tasks = sa.Table(
        "tasks",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("info", sa.Text(), nullable=True),
        sa.Column("link", sa.String(length=1024), nullable=True),
    )
    subtasks = sa.Table(
        "subtasks",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("info", sa.Text(), nullable=True),
        sa.Column("link", sa.String(length=1024), nullable=True),
    )

    for row in bind.execute(sa.select(tasks.c.id, tasks.c.info, tasks.c.link)):
        if not row.info and row.link:
            bind.execute(tasks.update().where(tasks.c.id == row.id).values(info=_info_from_link(row.link)))

    for row in bind.execute(sa.select(subtasks.c.id, subtasks.c.info, subtasks.c.link)):
        if not row.info and row.link:
            bind.execute(subtasks.update().where(subtasks.c.id == row.id).values(info=_info_from_link(row.link)))


def downgrade():
    op.drop_column("subtasks", "info")
    op.drop_column("tasks", "info")
