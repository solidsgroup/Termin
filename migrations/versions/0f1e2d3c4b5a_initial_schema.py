"""initial schema

Revision ID: 0f1e2d3c4b5a
Revises:
Create Date: 2026-03-17 14:55:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0f1e2d3c4b5a"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("avatar_url", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("default_owner_calendar_opt_in", sa.Boolean(), nullable=False),
        sa.Column("default_invitee_calendar_opt_in", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "calendar_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_user_id", sa.String(length=255), nullable=True),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("color", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "project_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "group_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("link", sa.String(length=1024), nullable=True),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("assignee_email", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("owner_calendar_opt_in", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "external_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("calendar_id", sa.String(length=255), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "subtasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("link", sa.String(length=1024), nullable=True),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("assignee_email", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=False),
        sa.Column("channel_id", sa.String(length=255), nullable=True),
        sa.Column("expiration", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "assignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("subtask_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["subtask_id"], ["subtasks.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "invites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("subtask_id", sa.Integer(), nullable=True),
        sa.Column("assignment_id", sa.Integer(), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("calendar_opt_in", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"]),
        sa.ForeignKeyConstraint(["subtask_id"], ["subtasks.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )


def downgrade():
    op.drop_table("invites")
    op.drop_table("assignments")
    op.drop_table("webhook_subscriptions")
    op.drop_table("subtasks")
    op.drop_table("external_events")
    op.drop_table("tasks")
    op.drop_table("group_members")
    op.drop_table("project_members")
    op.drop_table("groups")
    op.drop_table("calendar_accounts")
    op.drop_table("projects")
    op.drop_table("users")
