"""add project divisions

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-03-18 14:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "divisions" not in tables:
        op.create_table(
            "divisions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("owner_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        )

    project_columns = {column["name"] for column in inspector.get_columns("projects")}
    if "division_id" not in project_columns:
        op.add_column("projects", sa.Column("division_id", sa.Integer(), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    project_columns = {column["name"] for column in inspector.get_columns("projects")}
    if "division_id" in project_columns:
        op.drop_column("projects", "division_id")
    if "divisions" in inspector.get_table_names():
        op.drop_table("divisions")
