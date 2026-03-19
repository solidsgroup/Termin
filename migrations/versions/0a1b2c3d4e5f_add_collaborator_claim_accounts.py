"""add collaborator claim accounts

Revision ID: 0a1b2c3d4e5f
Revises: f7a8b9c0d1e2
Create Date: 2026-03-19 15:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0a1b2c3d4e5f"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("password_hash", sa.String(length=255), nullable=True))

    with op.batch_alter_table("collaborator_profiles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_collaborator_profiles_user_id_users", "users", ["user_id"], ["id"])

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, email FROM collaborator_profiles")).fetchall()
    for collaborator_id, email in rows:
        user = conn.execute(
            sa.text("SELECT id FROM users WHERE lower(email) = :email"),
            {"email": (email or "").strip().lower()},
        ).fetchone()
        if user:
            conn.execute(
                sa.text("UPDATE collaborator_profiles SET user_id = :user_id WHERE id = :collaborator_id"),
                {"user_id": user.id, "collaborator_id": collaborator_id},
            )


def downgrade():
    with op.batch_alter_table("collaborator_profiles", schema=None) as batch_op:
        batch_op.drop_constraint("fk_collaborator_profiles_user_id_users", type_="foreignkey")
        batch_op.drop_column("user_id")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("password_hash")
