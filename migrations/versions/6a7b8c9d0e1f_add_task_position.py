"""add task position

Revision ID: 6a7b8c9d0e1f
Revises: 0f1e2d3c4b5a
Create Date: 2026-03-17 15:25:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "6a7b8c9d0e1f"
down_revision = "0f1e2d3c4b5a"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    task_columns = {column["name"] for column in inspector.get_columns("tasks")}

    if "position" not in task_columns:
        op.add_column("tasks", sa.Column("position", sa.Integer(), nullable=True))

    metadata = sa.MetaData()
    tasks = sa.Table(
        "tasks",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    rows = bind.execute(
        sa.select(tasks.c.id, tasks.c.project_id, tasks.c.group_id).order_by(
            tasks.c.project_id.asc(),
            tasks.c.created_at.asc(),
            tasks.c.id.asc(),
        )
    ).fetchall()

    counters = {}
    for row in rows:
        key = (row.project_id, row.group_id)
        counters[key] = counters.get(key, 0) + 1
        bind.execute(
            tasks.update().where(tasks.c.id == row.id).values(position=counters[key])
        )

    bind.execute(tasks.update().where(tasks.c.position.is_(None)).values(position=0))


def downgrade():
    op.drop_column("tasks", "position")
