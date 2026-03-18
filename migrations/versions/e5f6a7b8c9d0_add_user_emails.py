"""add user emails

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-03-18 09:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user_emails" not in inspector.get_table_names():
        op.create_table(
            "user_emails",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.Column("is_primary", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.UniqueConstraint("email"),
        )

    metadata = sa.MetaData()
    users = sa.Table(
        "users",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    user_emails = sa.Table(
        "user_emails",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    existing = {row.email.lower() for row in bind.execute(sa.select(user_emails.c.email))}
    for row in bind.execute(sa.select(users.c.id, users.c.email, users.c.created_at)):
        email = (row.email or "").strip().lower()
        if not email or email in existing:
            continue
        bind.execute(
            user_emails.insert().values(
                user_id=row.id,
                email=email,
                is_primary=True,
                created_at=row.created_at,
            )
        )
        existing.add(email)


def downgrade():
    op.drop_table("user_emails")
