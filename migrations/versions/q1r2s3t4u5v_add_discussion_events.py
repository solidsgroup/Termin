"""add discussion events

Revision ID: q1r2s3t4u5v
Revises: p0q1r2s3t4u
Create Date: 2026-04-02 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "q1r2s3t4u5v"
down_revision = "p0q1r2s3t4u"
branch_labels = None
depends_on = None


def _sqlite_timestamp_expr(column_sql: str) -> str:
    return f"CASE WHEN {column_sql} IS NOT NULL THEN strftime('%Y-%m-%d %H:%M:%f', {column_sql}) ELSE CURRENT_TIMESTAMP END"


def upgrade():
    op.create_table(
        "discussion_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=16), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_discussion_events_entity", "discussion_events", ["entity_type", "entity_id"], unique=False)
    op.create_index("ix_discussion_events_task_id", "discussion_events", ["task_id"], unique=False)
    op.create_index("ix_discussion_events_project_id", "discussion_events", ["project_id"], unique=False)
    op.create_index("ix_discussion_events_group_id", "discussion_events", ["group_id"], unique=False)
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO discussion_events (entity_type, entity_id, task_id, project_id, group_id, actor_user_id, kind, body, created_at)
            SELECT
                'project',
                p.id,
                NULL,
                p.id,
                NULL,
                p.owner_id,
                'created',
                COALESCE(u.display_name, u.email, 'Someone') || ' created project "' || COALESCE(NULLIF(TRIM(p.name), ''), 'Project') || '".',
                """
                + _sqlite_timestamp_expr("p.created_at")
                + """
            FROM projects p
            LEFT JOIN users u ON u.id = p.owner_id
            """
        )
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO discussion_events (entity_type, entity_id, task_id, project_id, group_id, actor_user_id, kind, body, created_at)
            SELECT
                'group',
                g.id,
                NULL,
                g.project_id,
                g.id,
                p.owner_id,
                'created',
                COALESCE(u.display_name, u.email, 'Someone') || ' created group "' || COALESCE(NULLIF(TRIM(g.name), ''), 'Group') || '".',
                """
                + _sqlite_timestamp_expr("g.created_at")
                + """
            FROM groups g
            LEFT JOIN projects p ON p.id = g.project_id
            LEFT JOIN users u ON u.id = p.owner_id
            """
        )
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO discussion_events (entity_type, entity_id, task_id, project_id, group_id, actor_user_id, kind, body, created_at)
            SELECT
                'task',
                t.id,
                t.id,
                t.project_id,
                t.group_id,
                t.creator_user_id,
                'created',
                COALESCE(u.display_name, u.email, 'Someone') || ' created task "' || COALESCE(NULLIF(TRIM(t.title), ''), 'Task') || '".',
                """
                + _sqlite_timestamp_expr("t.created_at")
                + """
            FROM tasks t
            LEFT JOIN users u ON u.id = t.creator_user_id
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
            ),
            deduped_task_history AS (
                SELECT DISTINCT
                    task_id,
                    project_id,
                    group_id,
                    actor_user_id,
                    kind,
                    body,
                    created_at
                FROM task_notification_history
                WHERE body IS NOT NULL
            )
            INSERT INTO discussion_events (entity_type, entity_id, task_id, project_id, group_id, actor_user_id, kind, body, created_at)
            SELECT
                'task',
                task_id,
                task_id,
                project_id,
                group_id,
                actor_user_id,
                kind,
                body,
                created_at
            FROM deduped_task_history
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
            ),
            deduped_task_history AS (
                SELECT DISTINCT
                    task_id,
                    project_id,
                    group_id,
                    actor_user_id,
                    kind,
                    body,
                    created_at
                FROM task_notification_history
                WHERE body IS NOT NULL
            )
            INSERT INTO discussion_events (entity_type, entity_id, task_id, project_id, group_id, actor_user_id, kind, body, created_at)
            SELECT
                'project',
                project_id,
                task_id,
                project_id,
                group_id,
                actor_user_id,
                kind,
                body,
                created_at
            FROM deduped_task_history
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
            ),
            deduped_task_history AS (
                SELECT DISTINCT
                    task_id,
                    project_id,
                    group_id,
                    actor_user_id,
                    kind,
                    body,
                    created_at
                FROM task_notification_history
                WHERE body IS NOT NULL
                  AND group_id IS NOT NULL
            )
            INSERT INTO discussion_events (entity_type, entity_id, task_id, project_id, group_id, actor_user_id, kind, body, created_at)
            SELECT
                'group',
                group_id,
                task_id,
                project_id,
                group_id,
                actor_user_id,
                kind,
                body,
                created_at
            FROM deduped_task_history
            """
        )
    )


def downgrade():
    op.drop_index("ix_discussion_events_group_id", table_name="discussion_events")
    op.drop_index("ix_discussion_events_project_id", table_name="discussion_events")
    op.drop_index("ix_discussion_events_task_id", table_name="discussion_events")
    op.drop_index("ix_discussion_events_entity", table_name="discussion_events")
    op.drop_table("discussion_events")
