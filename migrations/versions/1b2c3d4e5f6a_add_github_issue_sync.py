"""add github issue sync

Revision ID: 1b2c3d4e5f6a
Revises: 0a1b2c3d4e5f
Create Date: 2026-03-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "1b2c3d4e5f6a"
down_revision = "0a1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("external_identities", schema=None) as batch_op:
        batch_op.add_column(sa.Column("access_token", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("refresh_token", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("token_expires_at", sa.DateTime(), nullable=True))

    op.create_table(
        "github_sync_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_table(
        "github_issue_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("github_issue_id", sa.String(length=255), nullable=False),
        sa.Column("repository_full_name", sa.String(length=255), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("issue_url", sa.String(length=1024), nullable=False),
        sa.Column("issue_state", sa.String(length=32), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id"),
        sa.UniqueConstraint("user_id", "github_issue_id", name="uq_github_issue_links_user_issue"),
    )


def downgrade():
    op.drop_table("github_issue_links")
    op.drop_table("github_sync_states")

    with op.batch_alter_table("external_identities", schema=None) as batch_op:
        batch_op.drop_column("token_expires_at")
        batch_op.drop_column("refresh_token")
        batch_op.drop_column("access_token")
