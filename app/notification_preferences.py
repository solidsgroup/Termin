from app.models import Assignment, TaskFollower, UserNotificationPreference


TASK_NOTIFICATION_SCOPES = [
    {"key": "assigned", "label": "All tasks I am assigned to"},
    {"key": "following", "label": "All tasks I am following"},
    {"key": "all", "label": "All other tasks"},
]

TASK_NOTIFICATION_SCOPE_KEYS = {item["key"] for item in TASK_NOTIFICATION_SCOPES}

GENERAL_NOTIFICATION_DEFINITIONS = [
    {"key": "project_added", "label": "You are added to a project"},
    {"key": "project_removed", "label": "You are removed from a project"},
    {"key": "project_message", "label": "Message is sent on a Project"},
    {"key": "group_message", "label": "Message is sent on a Group"},
]

TASK_NOTIFICATION_DEFINITIONS = [
    {"key": "task_message", "label": "Message is sent on a Task"},
    {"key": "task_assigned", "label": "You are assigned to a task"},
    {"key": "task_completed", "label": "A task is completed"},
    {"key": "task_due_changed", "label": "A task due date changes"},
    {"key": "task_status_changed", "label": "A task status changes"},
    {"key": "task_renamed", "label": "A task title changes"},
    {"key": "task_moved", "label": "A task is moved"},
    {"key": "task_created", "label": "A new task is created"},
    {"key": "task_updated", "label": "Other task fields are updated"},
]

NOTIFICATION_EVENT_DEFINITIONS = GENERAL_NOTIFICATION_DEFINITIONS + TASK_NOTIFICATION_DEFINITIONS
NOTIFICATION_EVENT_KEYS = {item["key"] for item in NOTIFICATION_EVENT_DEFINITIONS}
TASK_NOTIFICATION_EVENT_KEYS = {item["key"] for item in TASK_NOTIFICATION_DEFINITIONS}
GENERAL_NOTIFICATION_EVENT_KEYS = {item["key"] for item in GENERAL_NOTIFICATION_DEFINITIONS}


def notification_event_key_for_kind(kind: str, *, task_id=None, project_id=None, group_id=None) -> str | None:
    normalized = str(kind or "").strip().lower()
    if normalized == "project_shared":
        return "project_added"
    if normalized == "project_removed":
        return "project_removed"
    if normalized == "comment":
        if group_id:
            return "group_message"
        if project_id and not task_id:
            return "project_message"
        return "task_message"
    if normalized == "assignment_added":
        return "task_assigned"
    if normalized == "task_completed":
        return "task_completed"
    if normalized in {"task_due_changed", "task_due_cleared", "task_due_asap"}:
        return "task_due_changed"
    if normalized == "task_status_changed":
        return "task_status_changed"
    if normalized == "task_renamed":
        return "task_renamed"
    if normalized == "task_moved":
        return "task_moved"
    if normalized == "task_created":
        return "task_created"
    if normalized == "task_update":
        return "task_updated"
    return None


def user_notification_preference_map(user_id: int) -> dict[str, dict]:
    rows = UserNotificationPreference.query.filter_by(user_id=user_id).all()
    preferences: dict[str, dict] = {}
    for row in rows:
        if row.event_key not in NOTIFICATION_EVENT_KEYS:
            continue
        if row.event_key in TASK_NOTIFICATION_EVENT_KEYS:
            scope_key = row.scope_key if row.scope_key in TASK_NOTIFICATION_SCOPE_KEYS else "all"
        else:
            scope_key = "default"
        event_preferences = preferences.setdefault(row.event_key, {})
        event_preferences[scope_key] = {
            "push_enabled": bool(row.push_enabled),
            "email_enabled": bool(row.email_enabled),
        }
    return preferences


def _task_notification_scope_match(user_id: int, task, scope_key: str) -> bool:
    if not task:
        return False
    normalized_scope = scope_key if scope_key in TASK_NOTIFICATION_SCOPE_KEYS else "all"
    if normalized_scope == "all":
        return True
    if normalized_scope == "assigned":
        return Assignment.query.filter_by(task_id=task.id, user_id=user_id).first() is not None
    if normalized_scope == "following":
        return TaskFollower.query.filter_by(task_id=task.id, user_id=user_id).first() is not None
    return False


def user_notification_channel_enabled(user_id: int, event_key: str | None, channel: str, *, task=None) -> bool:
    if not event_key or event_key not in NOTIFICATION_EVENT_KEYS:
        return True if channel == "push" else False
    if event_key in TASK_NOTIFICATION_EVENT_KEYS:
        matching_scopes = [scope["key"] for scope in TASK_NOTIFICATION_SCOPES if _task_notification_scope_match(user_id, task, scope["key"])]
        if not matching_scopes:
            matching_scopes = ["all"]
        rows = UserNotificationPreference.query.filter_by(user_id=user_id, event_key=event_key).all()
        scoped_rows = {}
        for row in rows:
            scope_key = row.scope_key if row.scope_key in TASK_NOTIFICATION_SCOPE_KEYS else "all"
            scoped_rows[scope_key] = row
        values = []
        for scope_key in matching_scopes:
            row = scoped_rows.get(scope_key)
            if row is None:
                values.append(True if channel == "push" else False)
            elif channel == "email":
                values.append(bool(row.email_enabled))
            else:
                values.append(bool(row.push_enabled))
        return any(values)
    row = UserNotificationPreference.query.filter_by(user_id=user_id, event_key=event_key).first()
    if not row:
        return True if channel == "push" else False
    if channel == "email":
        return bool(row.email_enabled)
    return bool(row.push_enabled)
