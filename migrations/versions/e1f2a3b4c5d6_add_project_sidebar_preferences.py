"""add project sidebar preferences

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-03-18 23:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_sidebar_preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("division_id", sa.Integer(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["division_id"], ["divisions.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "project_id", name="uq_project_sidebar_preferences_user_project"),
    )

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, owner_id, division_id, COALESCE(position, 0) FROM projects")).fetchall()
    for project_id, owner_id, division_id, position in rows:
        conn.execute(
            sa.text(
                """
                INSERT INTO project_sidebar_preferences (user_id, project_id, division_id, position)
                VALUES (:user_id, :project_id, :division_id, :position)
                """
            ),
            {
                "user_id": owner_id,
                "project_id": project_id,
                "division_id": division_id,
                "position": position or 0,
            },
        )


def downgrade():
    op.drop_table("project_sidebar_preferences")
