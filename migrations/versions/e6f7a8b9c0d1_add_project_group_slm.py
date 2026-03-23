"""add project/group slm

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-03-25 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("info", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("link", sa.String(length=1024), nullable=True))

    with op.batch_alter_table("groups", schema=None) as batch_op:
        batch_op.add_column(sa.Column("info", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("link", sa.String(length=1024), nullable=True))

    op.create_table(
        "project_comments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "group_comments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("group_comments")
    op.drop_table("project_comments")

    with op.batch_alter_table("groups", schema=None) as batch_op:
        batch_op.drop_column("link")
        batch_op.drop_column("info")

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("link")
        batch_op.drop_column("info")
