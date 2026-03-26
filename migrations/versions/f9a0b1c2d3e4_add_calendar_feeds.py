"""add calendar feeds

Revision ID: f9a0b1c2d3e4
Revises: f8a9b0c1d2e3
Create Date: 2026-03-26 15:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "f9a0b1c2d3e4"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "calendar_feeds" in inspector.get_table_names():
        return
    op.create_table(
        "calendar_feeds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("token", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("email", name="uq_calendar_feeds_email"),
        sa.UniqueConstraint("token", name="uq_calendar_feeds_token"),
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "calendar_feeds" not in inspector.get_table_names():
        return
    op.drop_table("calendar_feeds")
