"""add group template task descriptions

Revision ID: x9y0z1a2b3c
Revises: w8x9y0z1a2b
Create Date: 2026-04-13 14:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "x9y0z1a2b3c"
down_revision = "w8x9y0z1a2b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("group_template_tasks", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("group_template_tasks", "description")
