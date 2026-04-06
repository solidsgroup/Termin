"""repair discussion event timestamps

Revision ID: r2s3t4u5v6w
Revises: q1r2s3t4u5v
Create Date: 2026-04-02 00:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "r2s3t4u5v6w"
down_revision = "q1r2s3t4u5v"
branch_labels = None
depends_on = None


def _sqlite_timestamp_expr(column_sql: str) -> str:
    return f"strftime('%Y-%m-%d %H:%M:%f', {column_sql})"


def upgrade():
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE discussion_events
            SET created_at = (
                SELECT """
                + _sqlite_timestamp_expr("p.created_at")
                + """
                FROM projects p
                WHERE discussion_events.kind = 'created'
                  AND discussion_events.entity_type = 'project'
                  AND discussion_events.entity_id = p.id
            )
            WHERE kind = 'created'
              AND entity_type = 'project'
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE discussion_events
            SET created_at = (
                SELECT """
                + _sqlite_timestamp_expr("g.created_at")
                + """
                FROM groups g
                WHERE discussion_events.kind = 'created'
                  AND discussion_events.entity_type = 'group'
                  AND discussion_events.entity_id = g.id
            )
            WHERE kind = 'created'
              AND entity_type = 'group'
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE discussion_events
            SET created_at = (
                SELECT """
                + _sqlite_timestamp_expr("t.created_at")
                + """
                FROM tasks t
                WHERE discussion_events.kind = 'created'
                  AND discussion_events.entity_type = 'task'
                  AND discussion_events.entity_id = t.id
            )
            WHERE kind = 'created'
              AND entity_type = 'task'
            """
        )
    )
    bind.execute(
        sa.text(
            """
            WITH task_notification_history AS (
                SELECT
                    tn.task_id AS task_id,
                    t.project_id AS project_id,
                    t.group_id AS group_id,
                    COALESCE(CAST(json_extract(tn.notification_data, '$.actor_user_id') AS INTEGER), t.creator_user_id) AS actor_user_id,
                    tn.kind AS kind,
                    """
                    + _sqlite_timestamp_expr("tn.created_at")
                    + """ AS created_at,
                    CASE
                        WHEN tn.kind = 'task_moved' THEN
                            COALESCE(json_extract(tn.notification_data, '$.actor_name'), u.display_name, u.email, 'Someone') || ' moved task "' || COALESCE(NULLIF(TRIM(t.title), ''), 'Task') || '".'
                        WHEN tn.kind = 'task_renamed' THEN
                            COALESCE(json_extract(tn.notification_data, '$.actor_name'), u.display_name, u.email, 'Someone') || ' updated task "' || COALESCE(NULLIF(TRIM(t.title), ''), 'Task') || '" (title).'
                        WHEN tn.kind = 'task_due_changed' THEN
                            COALESCE(json_extract(tn.notification_data, '$.actor_name'), u.display_name, u.email, 'Someone') || ' updated task "' || COALESCE(NULLIF(TRIM(t.title), ''), 'Task') || '" (due date).'
                        WHEN tn.kind = 'task_due_cleared' THEN
                            COALESCE(json_extract(tn.notification_data, '$.actor_name'), u.display_name, u.email, 'Someone') || ' updated task "' || COALESCE(NULLIF(TRIM(t.title), ''), 'Task') || '" (due date).'
                        WHEN tn.kind = 'task_due_asap' THEN
                            COALESCE(json_extract(tn.notification_data, '$.actor_name'), u.display_name, u.email, 'Someone') || ' updated task "' || COALESCE(NULLIF(TRIM(t.title), ''), 'Task') || '" (due date).'
                        WHEN tn.kind = 'task_status_changed' THEN
                            COALESCE(json_extract(tn.notification_data, '$.actor_name'), u.display_name, u.email, 'Someone') || ' updated task "' || COALESCE(NULLIF(TRIM(t.title), ''), 'Task') || '" (status).'
                        WHEN tn.kind = 'task_completed' THEN
                            COALESCE(json_extract(tn.notification_data, '$.actor_name'), u.display_name, u.email, 'Someone') || ' updated task "' || COALESCE(NULLIF(TRIM(t.title), ''), 'Task') || '" (status).'
                        WHEN tn.kind = 'assignment_added' THEN
                            COALESCE(json_extract(tn.notification_data, '$.actor_name'), u.display_name, u.email, 'Someone') || ' updated task "' || COALESCE(NULLIF(TRIM(t.title), ''), 'Task') || '" (assignees).'
                        WHEN tn.kind = 'task_update' THEN
                            COALESCE(json_extract(tn.notification_data, '$.actor_name'), u.display_name, u.email, 'Someone') || ' updated task "' || COALESCE(NULLIF(TRIM(t.title), ''), 'Task') || '".'
                        ELSE NULL
                    END AS body
                FROM task_notifications tn
                JOIN tasks t ON t.id = tn.task_id
                LEFT JOIN users u ON u.id = COALESCE(CAST(json_extract(tn.notification_data, '$.actor_user_id') AS INTEGER), t.creator_user_id)
                WHERE tn.task_id IS NOT NULL
                  AND tn.kind IN (
                    'task_moved',
                    'task_renamed',
                    'task_due_changed',
                    'task_due_cleared',
                    'task_due_asap',
                    'task_status_changed',
                    'task_completed',
                    'assignment_added',
                    'task_update'
                  )
            )
            UPDATE discussion_events
            SET created_at = (
                SELECT MIN(tnh.created_at)
                FROM task_notification_history tnh
                WHERE tnh.task_id = discussion_events.task_id
                  AND tnh.kind = discussion_events.kind
                  AND COALESCE(tnh.actor_user_id, 0) = COALESCE(discussion_events.actor_user_id, 0)
                  AND tnh.body = discussion_events.body
                  AND (
                    (discussion_events.entity_type = 'task' AND discussion_events.entity_id = tnh.task_id)
                    OR (discussion_events.entity_type = 'project' AND discussion_events.entity_id = tnh.project_id)
                    OR (discussion_events.entity_type = 'group' AND discussion_events.entity_id = tnh.group_id)
                  )
            )
            WHERE kind IN (
                'task_moved',
                'task_renamed',
                'task_due_changed',
                'task_due_cleared',
                'task_due_asap',
                'task_status_changed',
                'task_completed',
                'assignment_added',
                'task_update'
            )
            """
        )
    )


def downgrade():
    pass
