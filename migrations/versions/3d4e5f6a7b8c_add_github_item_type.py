"""add github item type

Revision ID: 3d4e5f6a7b8c
Revises: 2c3d4e5f6a7b
Create Date: 2026-03-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "3d4e5f6a7b8c"
down_revision = "2c3d4e5f6a7b"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("github_issue_links", schema=None) as batch_op:
        batch_op.add_column(sa.Column("item_type", sa.String(length=32), nullable=True))

    op.execute("UPDATE github_issue_links SET item_type = 'issue' WHERE item_type IS NULL")

    with op.batch_alter_table("github_issue_links", schema=None) as batch_op:
        batch_op.alter_column("item_type", existing_type=sa.String(length=32), nullable=False)


def downgrade():
    with op.batch_alter_table("github_issue_links", schema=None) as batch_op:
        batch_op.drop_column("item_type")
