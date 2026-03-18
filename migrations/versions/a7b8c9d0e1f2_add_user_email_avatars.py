"""add user email avatars

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-03-18 12:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("user_emails")}
    if "avatar_url" not in columns:
        op.add_column("user_emails", sa.Column("avatar_url", sa.String(length=1024), nullable=True))

    metadata = sa.MetaData()
    users = sa.Table(
        "users",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("avatar_url", sa.String(length=1024)),
    )
    user_emails = sa.Table(
        "user_emails",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("avatar_url", sa.String(length=1024)),
    )

    primary_rows = bind.execute(
        sa.select(user_emails.c.id, user_emails.c.user_id, user_emails.c.avatar_url).where(user_emails.c.is_primary.is_(True))
    ).fetchall()
    user_avatar_map = {
        row.id: row.avatar_url
        for row in bind.execute(sa.select(users.c.id, users.c.avatar_url))
        if row.avatar_url
    }
    for row in primary_rows:
        if row.avatar_url:
            continue
        avatar_url = user_avatar_map.get(row.user_id)
        if not avatar_url:
            continue
        bind.execute(
            user_emails.update().where(user_emails.c.id == row.id).values(avatar_url=avatar_url)
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("user_emails")}
    if "avatar_url" in columns:
        op.drop_column("user_emails", "avatar_url")
