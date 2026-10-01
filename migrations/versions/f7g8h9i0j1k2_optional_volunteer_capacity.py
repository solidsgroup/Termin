"""Make volunteer capacity optional; confirmation mode is stored in task metadata.

Revision ID: f7g8h9i0j1k2
Revises: e6f7g8h9i0j1
"""
import json

from alembic import op
import sqlalchemy as sa

revision = "f7g8h9i0j1k2"
down_revision = "e6f7g8h9i0j1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("tasks") as batch:
        batch.alter_column("volunteers_required", existing_type=sa.Integer(), nullable=True, server_default=None)
    # Old normal tasks inherited 1 even though they never requested volunteers.
    # Keep existing volunteer limits, but remove that unused default elsewhere.
    connection = op.get_bind()
    for row in connection.execute(sa.text("SELECT id, info FROM tasks")).mappings().all():
        try:
            info = json.loads(row["info"] or "{}")
        except (ValueError, TypeError):
            info = {}
        meta = info.get("meta") if isinstance(info, dict) else None
        if not isinstance(meta, dict) or meta.get("assignee_mode") != "volunteer":
            connection.execute(sa.text("UPDATE tasks SET volunteers_required = NULL WHERE id = :id"), {"id": row["id"]})


def downgrade():
    op.execute("UPDATE tasks SET volunteers_required = 1 WHERE volunteers_required IS NULL")
    with op.batch_alter_table("tasks") as batch:
        batch.alter_column("volunteers_required", existing_type=sa.Integer(), nullable=False, server_default="1")
