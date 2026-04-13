"""add private group templates

Revision ID: w8x9y0z1a2b
Revises: v7w8x9y0z1a
Create Date: 2026-04-13 14:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "w8x9y0z1a2b"
down_revision = "v7w8x9y0z1a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "group_template_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_template_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["group_template_id"], ["group_templates.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("group_template_tasks")
    op.drop_table("group_templates")

