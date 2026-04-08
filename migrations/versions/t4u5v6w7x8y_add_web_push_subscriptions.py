"""add web push subscriptions

Revision ID: t4u5v6w7x8y
Revises: s3t4u5v6w7x
Create Date: 2026-04-01 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "t4u5v6w7x8y"
down_revision = "s3t4u5v6w7x"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "web_push_subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("device_label", sa.String(length=255), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("endpoint"),
    )


def downgrade():
    op.drop_table("web_push_subscriptions")
