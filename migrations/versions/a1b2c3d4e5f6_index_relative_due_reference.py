"""Index relative due-date references.

Revision ID: a1b2c3d4e5f6
Revises: f7g8h9i0j1k2
"""

from alembic import op
import sqlalchemy as sa

from app.info_utils import relative_due_reference_id


revision = "a1b2c3d4e5f6"
down_revision = "f7g8h9i0j1k2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("relative_due_reference_id", sa.BigInteger()))
    tasks = sa.table(
        "tasks",
        sa.column("id", sa.Integer),
        sa.column("info", sa.Text),
        sa.column("relative_due_reference_id", sa.BigInteger),
    )
    connection = op.get_bind()
    last_id = 0
    while True:
        rows = connection.execute(
            sa.select(tasks.c.id, tasks.c.info)
            .where(tasks.c.id > last_id)
            .order_by(tasks.c.id)
            .limit(1000)
        ).all()
        if not rows:
            break
        updates = []
        for row in rows:
            reference_id = relative_due_reference_id(row.info)
            if reference_id is not None:
                updates.append({"task_id": row.id, "reference_id": reference_id})
        if updates:
            connection.execute(
                tasks.update()
                .where(tasks.c.id == sa.bindparam("task_id"))
                .values(relative_due_reference_id=sa.bindparam("reference_id")),
                updates,
            )
        last_id = rows[-1].id
    op.create_index("ix_tasks_relative_due_reference_id", "tasks", ["relative_due_reference_id"])


def downgrade():
    op.drop_index("ix_tasks_relative_due_reference_id", table_name="tasks")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("relative_due_reference_id")
