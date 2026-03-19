"""add github assignees json

Revision ID: 2c3d4e5f6a7b
Revises: 1b2c3d4e5f6a
Create Date: 2026-03-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "2c3d4e5f6a7b"
down_revision = "1b2c3d4e5f6a"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("github_issue_links", schema=None) as batch_op:
        batch_op.add_column(sa.Column("assignees_json", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("github_issue_links", schema=None) as batch_op:
        batch_op.drop_column("assignees_json")
