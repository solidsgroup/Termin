"""add Google Drive specialty groups

Revision ID: d5e6f7g8h9i0
Revises: 4a5b6c7d8e9f, 4f5e6d7c8b9a, c4d5e6f7g8h9
Create Date: 2026-09-05 18:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "d5e6f7g8h9i0"
down_revision = ("4a5b6c7d8e9f", "4f5e6d7c8b9a", "c4d5e6f7g8h9")
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("calendar_accounts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("scopes", sa.Text(), nullable=True))

    op.create_table(
        "google_drive_integrations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("authorized_user_id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.String(length=255), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("web_view_link", sa.String(length=2048), nullable=True),
        sa.Column("last_full_synced_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["authorized_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id"),
    )
    op.create_table(
        "google_drive_comment_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("comment_id", sa.String(length=255), nullable=False),
        sa.Column("comment_modified_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "comment_id", name="uq_google_drive_comment_links_group_comment"),
        sa.UniqueConstraint("task_id"),
    )


def downgrade() -> None:
    op.drop_table("google_drive_comment_links")
    op.drop_table("google_drive_integrations")
    with op.batch_alter_table("calendar_accounts", schema=None) as batch_op:
        batch_op.drop_column("scopes")
