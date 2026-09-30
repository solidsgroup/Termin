"""Add volunteer request capacity and confirmed assignments.

Revision ID: e6f7g8h9i0j1
Revises: d5e6f7g8h9i0
"""
from alembic import op
import sqlalchemy as sa

revision = "e6f7g8h9i0j1"
down_revision = "d5e6f7g8h9i0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("volunteers_required", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("assignments", sa.Column("volunteered_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("assignments") as batch:
        batch.drop_column("volunteered_at")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("volunteers_required")
