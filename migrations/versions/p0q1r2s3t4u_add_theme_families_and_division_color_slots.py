"""add theme families and division color slots

Revision ID: p0q1r2s3t4u
Revises: o9p0q1r2s3t
Create Date: 2026-04-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "p0q1r2s3t4u"
down_revision = "o9p0q1r2s3t"
branch_labels = None
depends_on = None


TERMIN_PALETTE = (
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
    "#17becf",
)


def upgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("theme_name", sa.String(length=32), nullable=False, server_default="termin"))

    with op.batch_alter_table("divisions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("color_slot", sa.Integer(), nullable=True))

    connection = op.get_bind()
    divisions = connection.execute(sa.text("SELECT id, color FROM divisions")).fetchall()
    for division_id, color in divisions:
        normalized = (color or "").strip().lower()
        slot_index = None
        for index, palette_color_value in enumerate(TERMIN_PALETTE, start=1):
            if normalized == palette_color_value.lower():
                slot_index = index
                break
        if slot_index is not None:
            connection.execute(
                sa.text("UPDATE divisions SET color_slot = :slot_index, color = NULL WHERE id = :division_id"),
                {"slot_index": slot_index, "division_id": division_id},
            )

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.alter_column("theme_name", server_default=None)


def downgrade():
    connection = op.get_bind()
    divisions = connection.execute(sa.text("SELECT id, color, color_slot FROM divisions")).fetchall()
    for division_id, color, color_slot in divisions:
        if color:
            continue
        if color_slot and 1 <= int(color_slot) <= len(TERMIN_PALETTE):
            connection.execute(
                sa.text("UPDATE divisions SET color = :color WHERE id = :division_id"),
                {"color": TERMIN_PALETTE[int(color_slot) - 1], "division_id": division_id},
            )

    with op.batch_alter_table("divisions", schema=None) as batch_op:
        batch_op.drop_column("color_slot")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("theme_name")
