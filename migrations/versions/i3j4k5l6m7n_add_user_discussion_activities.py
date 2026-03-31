"""add user discussion activities

Revision ID: i3j4k5l6m7n
Revises: h2i3j4k5l6m
Create Date: 2026-03-30 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "i3j4k5l6m7n"
down_revision = "h2i3j4k5l6m"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user_discussion_activities" in inspector.get_table_names():
        return
    op.create_table(
        "user_discussion_activities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=16), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("last_comment_id", sa.Integer(), nullable=False),
        sa.Column("last_actor_user_id", sa.Integer(), nullable=True),
        sa.Column("last_actor_collaborator_id", sa.Integer(), nullable=True),
        sa.Column("last_preview", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"]),
        sa.ForeignKeyConstraint(["last_actor_collaborator_id"], ["collaborator_profiles.id"]),
        sa.ForeignKeyConstraint(["last_actor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "entity_type", "entity_id", name="uq_user_discussion_activities_user_entity"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user_discussion_activities" in inspector.get_table_names():
        op.drop_table("user_discussion_activities")
