"""add email verifications

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-03-18 09:45:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "email_verifications" in inspector.get_table_names():
        return
    op.create_table(
        "email_verifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("source_user_id", sa.Integer(), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["source_user_id"], ["users.id"]),
        sa.UniqueConstraint("token"),
    )


def downgrade():
    op.drop_table("email_verifications")
