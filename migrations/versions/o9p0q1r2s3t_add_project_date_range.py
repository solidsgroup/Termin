"""add project date range

Revision ID: o9p0q1r2s3t
Revises: n8o9p0q1r2s
Create Date: 2026-03-30 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "o9p0q1r2s3t"
down_revision = "n8o9p0q1r2s"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("start_date", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("end_date", sa.Date(), nullable=True))


def downgrade():
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("end_date")
        batch_op.drop_column("start_date")
