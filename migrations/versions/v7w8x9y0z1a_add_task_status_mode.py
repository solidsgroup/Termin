"""add canonical task status_mode column

Revision ID: v7w8x9y0z1a
Revises: u6v7w8x9y0z
Create Date: 2026-04-11 10:45:00.000000
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "v7w8x9y0z1a"
down_revision = "u6v7w8x9y0z"
branch_labels = None
depends_on = None


def _normalize_status_mode(value: str | None, *, default: str = "single") -> str:
    text = str(value or "").strip().lower()
    if text in {"per_user", "multi"}:
        return "multi"
    if text in {"percentage", "percent"}:
        return "percent"
    if text == "single":
        return "single"
    return default


def _infer_task_status_mode(info_value: str | None, per_user_enabled: bool) -> str:
    payload = {}
    if info_value:
        try:
            payload = json.loads(info_value)
        except Exception:
            payload = {}
    meta = payload.get("meta") if isinstance(payload, dict) else {}
    if not isinstance(meta, dict):
        meta = {}
    mode = _normalize_status_mode(meta.get("status_mode"), default="")
    if mode in {"single", "multi", "percent"}:
        return mode
    if str(meta.get("status_percentage") or "").strip():
        return "percent"
    if per_user_enabled:
        return "multi"
    return "single"


def upgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("status_mode", sa.String(length=32), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, info, per_user_status_enabled FROM tasks")).fetchall()
    for row in rows:
        mode = _infer_task_status_mode(row.info, bool(row.per_user_status_enabled))
        bind.execute(
            sa.text("UPDATE tasks SET status_mode = :status_mode, per_user_status_enabled = :per_user_status_enabled WHERE id = :task_id"),
            {
                "status_mode": mode,
                "per_user_status_enabled": 1 if mode == "multi" else 0,
                "task_id": row.id,
            },
        )

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.alter_column("status_mode", nullable=False, server_default="single")


def downgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("status_mode")

