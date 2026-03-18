"""add collaborator profiles

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-03-17 21:40:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "collaborator_profiles" in inspector.get_table_names():
        return
    op.create_table(
        "collaborator_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("access_token", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("default_calendar_opt_in", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("access_token"),
    )


def downgrade():
    op.drop_table("collaborator_profiles")
