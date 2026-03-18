"""add division color

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-03-18 15:35:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("divisions")}
    if "color" not in columns:
        op.add_column("divisions", sa.Column("color", sa.String(length=32), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("divisions")}
    if "color" in columns:
        op.drop_column("divisions", "color")
