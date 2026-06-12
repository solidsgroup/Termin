"""add team invites

Revision ID: b3c4d5e6f7g8
Revises: a2b3c4d5e6f7
Create Date: 2026-06-12 14:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "b3c4d5e6f7g8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "team_invites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_project_id", sa.Integer(), nullable=False),
        sa.Column("inviter_user_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("accepted_user_id", sa.Integer(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["accepted_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["inviter_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["team_project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )


def downgrade() -> None:
    op.drop_table("team_invites")
