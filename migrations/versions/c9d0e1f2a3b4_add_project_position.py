"""add project position

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-03-18 15:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("projects")}
    if "position" not in columns:
        op.add_column("projects", sa.Column("position", sa.Integer(), nullable=True))

    metadata = sa.MetaData()
    projects = sa.Table(
        "projects",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("division_id", sa.Integer()),
        sa.Column("position", sa.Integer()),
    )

    rows = bind.execute(
        sa.select(projects.c.id, projects.c.division_id).order_by(projects.c.division_id.asc().nullsfirst(), projects.c.id.asc())
    ).fetchall()
    counters = {}
    for row in rows:
        key = row.division_id if row.division_id is not None else "top"
        counters[key] = counters.get(key, 0) + 1
        bind.execute(
            projects.update().where(projects.c.id == row.id).values(position=counters[key])
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("projects")}
    if "position" in columns:
        op.drop_column("projects", "position")
