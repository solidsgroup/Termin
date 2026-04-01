from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
import json

from flask import current_app, url_for
from sqlalchemy import or_

from app.emailer import _render_email_template, send_email
from app.extensions import db
from app.models import Group, Project, Task, TaskComment, TaskNotification, User
from app.notification_preferences import notification_event_key_for_kind, user_notification_channel_enabled
from app.utils import display_name_for_user


EMAIL_FREQUENCY_OPTIONS = [
    {"seconds": 0, "label": "0 seconds"},
    {"seconds": 10, "label": "10 seconds"},
    {"seconds": 60, "label": "1 minute"},
    {"seconds": 300, "label": "5 minutes"},
    {"seconds": 600, "label": "10 minutes"},
    {"seconds": 3600, "label": "1 hour"},
    {"seconds": 21600, "label": "6 hours"},
    {"seconds": 43200, "label": "12 hours"},
    {"seconds": 86400, "label": "1 day"},
]

EMAIL_FREQUENCY_SECONDS = {item["seconds"] for item in EMAIL_FREQUENCY_OPTIONS}
_worker_started = False


def normalize_notification_email_frequency(value) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return 0
    return seconds if seconds in EMAIL_FREQUENCY_SECONDS else 0


def _notification_detail(row: TaskNotification) -> dict:
    raw = getattr(row, "notification_data", None)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _notification_task(row: TaskNotification) -> Task | None:
    return Task.query.get(row.task_id) if row.task_id else None


def _notification_project(row: TaskNotification, detail: dict, task: Task | None) -> Project | None:
    project_id = detail.get("project_id")
    if project_id in ("", None) and task:
        project_id = task.project_id
    try:
        project_id = int(project_id) if project_id not in ("", None) else None
    except (TypeError, ValueError):
        project_id = None
    return Project.query.get(project_id) if project_id else None


def _notification_group(row: TaskNotification, detail: dict, task: Task | None) -> Group | None:
    group_id = detail.get("group_id")
    if group_id in ("", None) and task and task.group_id:
        group_id = task.group_id
    try:
        group_id = int(group_id) if group_id not in ("", None) else None
    except (TypeError, ValueError):
        group_id = None
    return Group.query.get(group_id) if group_id else None


def _notification_preview(row: TaskNotification) -> dict:
    detail = _notification_detail(row)
    task = _notification_task(row)
    project = _notification_project(row, detail, task)
    group = _notification_group(row, detail, task)
    actor_name = str(detail.get("actor_name") or "").strip()
    project_name = str(detail.get("project_name") or (project.name if project else "") or "").strip()
    task_label = str((task.title if task else project_name) or "Task").strip()

    if row.kind == "comment":
        preview = str(detail.get("preview") or detail.get("comment_preview") or "").strip()
        if not preview and row.comment_id:
            comment = TaskComment.query.get(row.comment_id)
            preview = str(getattr(comment, "body", "") or "").strip()
        if len(preview) > 180:
            preview = preview[:180].rstrip() + "…"
        title = task_label
        message = preview or "New comment"
    elif row.kind == "task_completed":
        title = task_label
        message = ((actor_name + " completed this task") if actor_name else "Task completed")
    elif row.kind == "task_due_changed":
        new_due = str(detail.get("new_due_label") or detail.get("new_due_at") or "").strip()
        title = task_label
        if actor_name and new_due:
            message = actor_name + " changed the due date to " + new_due
        elif actor_name:
            message = actor_name + " changed the due date"
        else:
            message = ("Due date changed to " + new_due) if new_due else "Due date changed"
    elif row.kind == "task_due_cleared":
        title = task_label
        message = (actor_name + " removed the due date") if actor_name else "Due date removed"
    elif row.kind == "task_due_asap":
        title = task_label
        message = (actor_name + " marked this task ASAP") if actor_name else "Marked ASAP"
    elif row.kind == "task_status_changed":
        new_status = str(detail.get("new_status") or "").strip()
        title = task_label
        if actor_name and new_status:
            message = actor_name + " changed the status to " + new_status
        elif actor_name:
            message = actor_name + " changed the status"
        else:
            message = ("Status changed to " + new_status) if new_status else "Status changed"
    elif row.kind == "task_renamed":
        new_title = str(detail.get("new_title") or "").strip()
        title = task_label
        if actor_name and new_title:
            message = actor_name + " renamed this task to " + new_title
        elif actor_name:
            message = actor_name + " renamed this task"
        else:
            message = ("Renamed to " + new_title) if new_title else "Task renamed"
    elif row.kind == "assignment_added":
        title = task_label
        recipient_phrase = str(detail.get("recipient_assignment_phrase") or "").strip()
        if detail.get("group_assign"):
            other_count = max(int(detail.get("group_assign_other_count") or 0), 0)
            if actor_name:
                message = (
                    actor_name + " assigned this to you and " + str(other_count) + " other(s)"
                    if other_count
                    else actor_name + " assigned this to you"
                )
            else:
                message = (
                    "Assigned to you and " + str(other_count) + " other(s)"
                    if other_count
                    else "Assigned to you"
                )
        elif actor_name and recipient_phrase:
            message = actor_name + " assigned this to " + recipient_phrase
        elif recipient_phrase:
            message = "Assigned to " + recipient_phrase
        else:
            message = "Assigned to task"
    elif row.kind == "task_moved":
        title = task_label
        message = (actor_name + " moved this task") if actor_name else "Task moved"
    elif row.kind == "task_created":
        title = task_label
        message = (actor_name + " created this task") if actor_name else "Task created"
    elif row.kind == "project_shared":
        title = project_name or "Project"
        message = ((actor_name + " added you to this project") if actor_name else "Added to this project")
    elif row.kind == "project_removed":
        title = project_name or "Project"
        message = ((actor_name + " removed you from this project") if actor_name else "Removed from this project")
    else:
        changed_fields = [str(value).strip() for value in (detail.get("changed_fields") or []) if str(value).strip()]
        title = task_label
        if actor_name and changed_fields:
            message = actor_name + " updated " + ", ".join(changed_fields)
        elif changed_fields:
            message = "Updated " + ", ".join(changed_fields)
        else:
            message = actor_name + " updated this task" if actor_name else "Task updated"

    return {
        "title": title,
        "message": message,
        "created_at": row.created_at,
        "project_name": project_name,
        "group_name": str((group.name if group else "") or "").strip(),
    }


def _notification_row_html(item: dict) -> str:
    meta_parts = [part for part in [item.get("project_name"), item.get("group_name")] if part]
    meta_html = (
        '<div style="margin-top:4px;font-size:12px;color:#5d6b7d;">' + " • ".join(escape(part) for part in meta_parts) + "</div>"
        if meta_parts
        else ""
    )
    return (
        '<div style="padding:14px 16px;border:1px solid #dbe4ee;border-radius:16px;background:#f8fbff;">'
        + '<div style="font-size:14px;font-weight:700;color:#17202b;">' + escape(item["title"]) + "</div>"
        + '<div style="margin-top:4px;font-size:13px;line-height:1.45;color:#314256;">' + escape(item["message"]) + "</div>"
        + meta_html
        + "</div>"
    )


def _notification_rows_text(items: list[dict]) -> str:
    lines: list[str] = []
    for item in items:
        line = "- " + item["title"] + ": " + item["message"]
        meta_parts = [part for part in [item.get("project_name"), item.get("group_name")] if part]
        if meta_parts:
            line += " [" + " / ".join(meta_parts) + "]"
        lines.append(line)
    return "\n".join(lines)


def send_notification_digest_email(to_email: str, *, recipient_name: str, notifications: list[dict]) -> None:
    if not notifications:
        return
    public_base_url = str(current_app.config.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    try:
        path = url_for("ui.account_notifications")
    except Exception:
        path = "/account/notifications"
    if public_base_url:
        manage_url = public_base_url + path
    else:
        try:
            manage_url = url_for("ui.account_notifications", _external=True)
        except Exception:
            manage_url = path
    title = "Notification update"
    intro = "Here are your latest Termin notifications."
    if len(notifications) == 1:
        title = "1 new notification"
        intro = "Here is your latest Termin notification."
    else:
        title = f"{len(notifications)} new notifications"
    body_html = '<div style="display:grid;gap:12px;">' + "".join(_notification_row_html(item) for item in notifications) + "</div>"
    body_html += (
        '<div style="margin-top:22px;">'
        + f'<a href="{manage_url}" style="display:inline-block;padding:11px 16px;border-radius:999px;'
        + 'background:#1677c8;color:#ffffff;text-decoration:none;font-weight:700;font-size:13px;">Manage notification settings</a>'
        + "</div>"
    )
    html_body = _render_email_template(
        eyebrow="Notifications",
        title=title,
        intro="Hi " + escape(recipient_name) + ", " + intro.lower(),
        body_html=body_html,
    )
    text_body = f"{title}\n\nHi {recipient_name}, {intro.lower()}\n\n{_notification_rows_text(notifications)}\n\nManage settings: {manage_url}"
    send_email(
        to_email=to_email,
        subject="Termin: " + title,
        text_body=text_body,
        html_body=html_body,
    )


def _pending_email_notifications_for_user(user: User) -> list[TaskNotification]:
    rows = (
        TaskNotification.query.filter_by(user_id=user.id, emailed_at=None)
        .filter(or_(TaskNotification.read_at.is_(None), TaskNotification.pinned.is_(True)))
        .order_by(TaskNotification.created_at.asc(), TaskNotification.id.asc())
        .all()
    )
    pending: list[TaskNotification] = []
    for row in rows:
        task = _notification_task(row)
        detail = _notification_detail(row)
        event_key = notification_event_key_for_kind(
            row.kind,
            task_id=row.task_id,
            project_id=detail.get("project_id"),
            group_id=detail.get("group_id"),
        )
        if not user_notification_channel_enabled(user.id, event_key, "email", task=task):
            continue
        pending.append(row)
    return pending


def deliver_due_notification_emails_for_user(user_id: int) -> bool:
    user = User.query.get(user_id)
    if not user or not user.email:
        return False
    pending_rows = _pending_email_notifications_for_user(user)
    if not pending_rows:
        return False
    frequency_seconds = normalize_notification_email_frequency(user.notification_email_frequency_seconds)
    now = datetime.utcnow()
    if frequency_seconds > 0:
        if user.notification_email_last_sent_at:
            due_at = user.notification_email_last_sent_at + timedelta(seconds=frequency_seconds)
        else:
            due_at = pending_rows[0].created_at + timedelta(seconds=frequency_seconds)
        if now < due_at:
            return False
    items = [_notification_preview(row) for row in pending_rows]
    try:
        send_notification_digest_email(
            user.email,
            recipient_name=display_name_for_user(user) or user.email,
            notifications=items,
        )
    except Exception:
        current_app.logger.exception("notification email delivery failed", extra={"user_id": user.id})
        db.session.rollback()
        return False
    sent_at = datetime.utcnow()
    for row in pending_rows:
        row.emailed_at = sent_at
    user.notification_email_last_sent_at = sent_at
    db.session.commit()
    return True


def deliver_due_notification_emails(limit_users: int = 100) -> int:
    candidate_rows = (
        db.session.query(TaskNotification.user_id)
        .filter(TaskNotification.emailed_at.is_(None))
        .filter(or_(TaskNotification.read_at.is_(None), TaskNotification.pinned.is_(True)))
        .distinct()
        .limit(limit_users)
        .all()
    )
    delivered = 0
    for (user_id,) in candidate_rows:
        if user_id and deliver_due_notification_emails_for_user(int(user_id)):
            delivered += 1
    return delivered


def build_test_notification_email_items(user: User, preference_map: dict[str, dict]) -> list[dict]:
    now = datetime.utcnow()
    items: list[dict] = []
    actor_name = "Alice Example"
    for event_key, scopes in preference_map.items():
        for scope_key, pref in scopes.items():
            if not pref.get("email_enabled"):
                continue
            scope_suffix = ""
            if scope_key == "assigned":
                scope_suffix = " (assigned)"
            elif scope_key == "following":
                scope_suffix = " (following)"
            elif scope_key == "all":
                scope_suffix = " (other tasks)"
            title = "Project Orion"
            message = "sample notification"
            project_name = "Research Projects"
            group_name = "Weekly Planning"
            if event_key == "project_added":
                title = "Project Orion"
                message = actor_name + " added you to this project"
                group_name = ""
            elif event_key == "project_removed":
                title = "Project Orion"
                message = actor_name + " removed you from this project"
                group_name = ""
            elif event_key == "project_message":
                title = "Project chat"
                message = "New project message from " + actor_name
                group_name = ""
            elif event_key == "group_message":
                title = "Weekly Planning chat"
                message = "New group message from " + actor_name
            elif event_key == "task_message":
                title = "Prepare dataset" + scope_suffix
                message = "New task message from " + actor_name
            elif event_key == "task_assigned":
                title = "Prepare dataset" + scope_suffix
                message = actor_name + " assigned this to you"
            elif event_key == "task_completed":
                title = "Prepare dataset" + scope_suffix
                message = actor_name + " completed this task"
            elif event_key == "task_due_changed":
                title = "Prepare dataset" + scope_suffix
                message = actor_name + " changed the due date to tomorrow"
            elif event_key == "task_status_changed":
                title = "Prepare dataset" + scope_suffix
                message = actor_name + " changed the status to Open"
            elif event_key == "task_renamed":
                title = "Prepare dataset" + scope_suffix
                message = actor_name + " renamed this task to Prepare final dataset"
            elif event_key == "task_moved":
                title = "Prepare dataset" + scope_suffix
                message = actor_name + " moved this task"
            elif event_key == "task_created":
                title = "Prepare dataset" + scope_suffix
                message = actor_name + " created this task"
            elif event_key == "task_updated":
                title = "Prepare dataset" + scope_suffix
                message = actor_name + " updated description, links"
            items.append(
                {
                    "title": title,
                    "message": message,
                    "created_at": now,
                    "project_name": project_name,
                    "group_name": group_name,
                }
            )
    return items


def start_notification_email_worker(app, socketio, interval_seconds: int = 10) -> None:
    global _worker_started
    if _worker_started:
        return
    _worker_started = True

    def _runner():
        while True:
            try:
                with app.app_context():
                    deliver_due_notification_emails(limit_users=200)
            except Exception:
                app.logger.exception("notification email worker failed")
            socketio.sleep(interval_seconds)

    socketio.start_background_task(_runner)
