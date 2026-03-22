"""purge legacy subtask data

Revision ID: c1d2e3f4a5b6
Revises: ae1b90b08d51
Create Date: 2026-03-22 16:15:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "c1d2e3f4a5b6"
down_revision = "ae1b90b08d51"
branch_labels = None
depends_on = None


assignments = sa.table(
    "assignments",
    sa.column("id", sa.Integer),
    sa.column("subtask_id", sa.Integer),
)

invites = sa.table(
    "invites",
    sa.column("id", sa.Integer),
    sa.column("subtask_id", sa.Integer),
    sa.column("assignment_id", sa.Integer),
)

subtasks = sa.table(
    "subtasks",
    sa.column("id", sa.Integer),
)


def upgrade():
    bind = op.get_bind()

    subtask_assignment_ids = [
        row[0]
        for row in bind.execute(
            sa.select(assignments.c.id).where(assignments.c.subtask_id.is_not(None))
        ).fetchall()
    ]

    bind.execute(invites.delete().where(invites.c.subtask_id.is_not(None)))
    if subtask_assignment_ids:
        bind.execute(
            invites.delete().where(invites.c.assignment_id.in_(subtask_assignment_ids))
        )
    bind.execute(assignments.delete().where(assignments.c.subtask_id.is_not(None)))
    bind.execute(subtasks.delete())


def downgrade():
    pass
