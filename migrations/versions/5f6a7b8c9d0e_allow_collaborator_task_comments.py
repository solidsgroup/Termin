"""allow collaborator task comments

Revision ID: 5f6a7b8c9d0e
Revises: 4e5f6a7b8c9d
Create Date: 2026-03-19 17:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "5f6a7b8c9d0e"
down_revision = "4e5f6a7b8c9d"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("task_comments", schema=None) as batch_op:
        batch_op.alter_column("user_id", existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column("collaborator_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_task_comments_collaborator_id", "collaborator_profiles", ["collaborator_id"], ["id"])


def downgrade():
    with op.batch_alter_table("task_comments", schema=None) as batch_op:
        batch_op.drop_constraint("fk_task_comments_collaborator_id", type_="foreignkey")
        batch_op.drop_column("collaborator_id")
        batch_op.alter_column("user_id", existing_type=sa.Integer(), nullable=False)
