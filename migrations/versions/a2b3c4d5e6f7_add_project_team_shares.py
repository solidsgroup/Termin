"""add project team shares

Revision ID: a2b3c4d5e6f7
Revises: z1a2b3c4d5e
Create Date: 2026-06-12 12:45:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "a2b3c4d5e6f7"
down_revision = "z1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_team_shares",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("team_project_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["team_project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "team_project_id", name="uq_project_team_shares_project_team"),
    )


def downgrade() -> None:
    op.drop_table("project_team_shares")
