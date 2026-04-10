"""add updated_at timestamps to core dashboard entities

Revision ID: u6v7w8x9y0z
Revises: t4u5v6w7x8y
Create Date: 2026-04-08 14:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "u6v7w8x9y0z"
down_revision = "t4u5v6w7x8y"
branch_labels = None
depends_on = None


def _backfill_updated_at(table_name: str) -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            f"""
            UPDATE {table_name}
            SET updated_at = created_at
            WHERE updated_at IS NULL
            """
        )
    )


def upgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))
    _backfill_updated_at("projects")
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.alter_column("updated_at", nullable=False)

    with op.batch_alter_table("divisions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))
    _backfill_updated_at("divisions")
    with op.batch_alter_table("divisions", schema=None) as batch_op:
        batch_op.alter_column("updated_at", nullable=False)

    with op.batch_alter_table("groups", schema=None) as batch_op:
        batch_op.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))
    _backfill_updated_at("groups")
    with op.batch_alter_table("groups", schema=None) as batch_op:
        batch_op.alter_column("updated_at", nullable=False)

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))
    _backfill_updated_at("tasks")
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.alter_column("updated_at", nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("updated_at")
    with op.batch_alter_table("groups", schema=None) as batch_op:
        batch_op.drop_column("updated_at")
    with op.batch_alter_table("divisions", schema=None) as batch_op:
        batch_op.drop_column("updated_at")
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("updated_at")
