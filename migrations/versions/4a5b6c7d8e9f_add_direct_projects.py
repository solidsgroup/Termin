"""add direct project fields

Revision ID: 4a5b6c7d8e9f
Revises: 3d4e5f6a7b8c
Create Date: 2026-03-26 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "4a5b6c7d8e9f"
down_revision = "3d4e5f6a7b8c"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {col["name"] for col in inspector.get_columns("projects")}
    if "is_direct" not in columns:
        op.add_column("projects", sa.Column("is_direct", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "direct_user_a_id" not in columns:
        op.add_column("projects", sa.Column("direct_user_a_id", sa.Integer(), nullable=True))
    if "direct_user_b_id" not in columns:
        op.add_column("projects", sa.Column("direct_user_b_id", sa.Integer(), nullable=True))
    if inspector.bind.dialect.name != "sqlite":
        op.alter_column("projects", "is_direct", server_default=None)


def downgrade():
    op.drop_column("projects", "direct_user_b_id")
    op.drop_column("projects", "direct_user_a_id")
    op.drop_column("projects", "is_direct")
