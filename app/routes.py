from datetime import datetime
from html import escape
import json
from pathlib import Path
import re
from urllib.parse import urlparse
from sqlalchemy import or_

from flask import Blueprint, current_app, request, send_from_directory

from markdown import markdown as render_markdown
from app.auth import login_required
from app.collaborators import get_or_create_collaborator_profile
from app.emailer import MailDeliveryError, send_magic_link_digest_email, send_magic_link_email
from app.extensions import db
from app.discussion_activity import (
    group_discussion_user_ids,
    project_discussion_user_ids,
    task_discussion_user_ids,
    upsert_discussion_activity,
)
from app.github_sync import GitHubSyncError, github_identity_for_user, should_sync_github_issues, sync_github_issues_for_user
from app.group_assignments import group_assignment_candidate_users, serialize_group_assignment_members, sync_group_task_assignments
from app.identity import find_user_by_email, search_users_by_identity
from app.info_utils import load_info_payload, normalize_info_payload, save_uploaded_file, sanitize_info_html
from app.notification_preferences import user_notification_channel_enabled
from app.realtime import (
    _serialize_group_payload,
    _serialize_project_payload_for_user,
    emit_assignment_updated,
    emit_division_created,
    emit_division_deleted,
    emit_division_updated,
    emit_group_created,
    emit_discussion_activity_updated,
    emit_group_updated,
    emit_group_deleted,
    emit_group_members_updated,
    emit_group_comment_created,
    emit_group_comment_deleted,
    emit_group_comment_updated,
    emit_group_reordered,
    emit_notification_state,
    emit_notification_preview_dismissed,
    emit_project_created,
    emit_project_deleted,
    emit_project_updated,
    emit_project_comment_created,
    emit_project_comment_deleted,
    emit_project_comment_updated,
    emit_project_created,
    emit_project_deleted,
    emit_project_members_updated,
    emit_sidebar_reordered,
    is_user_viewing_project,
    is_user_viewing_task,
    project_access_map,
    emit_task_comment_created,
    emit_task_comment_deleted,
    emit_task_comment_updated,
    emit_task_notification_updates,
    emit_task_updated,
    notification_payload_for_user,
    emit_user_notification_preview,
    is_user_viewing_discussion,
    queue_user_notification,
    queue_task_notifications,
)
from app.sidebar_layout import (
    apply_top_level_project_order,
    ensure_sidebar_preference,
    insert_project_position,
    resequence_division_projects,
    resequence_top_level_items,
    sidebar_preference_map,
    top_level_sidebar_items,
)
from app.task_status import effective_task_status_for_user, set_task_user_status, task_status_meta, task_status_meta_map
import secrets

from app.models import (
    Division,
    Task,
    Project,
    ProjectSidebarPreference,
    Assignment,
    Invite,
    User,
    Group,
    ProjectMember,
    GroupMember,
    TaskComment,
    TaskFollower,
    TaskNotification,
    CollaboratorProfile,
    ProjectComment,
    GroupComment,
)
from app.utils import current_user, display_name_for_user, is_admin as user_is_admin


api_bp = Blueprint("api", __name__)


URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
DESCRIPTION_FORMAT_OPTIONS = {"plain", "markdown", "restructuredtext", "html"}
DEFAULT_PROJECT_DESCRIPTION_FORMAT = "markdown"
DEFAULT_GROUP_DESCRIPTION_FORMAT = "markdown"
DEFAULT_TASK_DESCRIPTION_FORMAT = "markdown"
TASK_DUE_MODE_OPTIONS = {"none", "date", "asap"}


def _is_complete_status_value(status: str | None) -> bool:
    value = (status or "").strip().lower()
    return value in {"complete", "completed", "done", "closed", "pr closed", "pr merged"}


def _task_update_notification_kind(
    *,
    old_title: str | None,
    new_title: str | None,
    old_status: str | None,
    new_status: str | None,
    old_due_at,
    new_due_at,
    old_due_mode: str | None,
    new_due_mode: str | None,
    user_status: str | None,
) -> str:
    effective_status = (user_status if user_status is not None else new_status) or ""
    if _is_complete_status_value(effective_status) and not _is_complete_status_value(old_status):
        return "task_completed"
    if (old_due_mode or "none") != (new_due_mode or "none") or bool(old_due_at) != bool(new_due_at) or (old_due_at and new_due_at and old_due_at != new_due_at):
        if (new_due_mode or "none") == "asap":
            return "task_due_asap"
        if (new_due_mode or "none") == "none":
            return "task_due_cleared"
        return "task_due_changed"
    if (old_status or "").strip() != (new_status or "").strip() or user_status is not None:
        return "task_status_changed"
    if (old_title or "").strip() != (new_title or "").strip():
        return "task_renamed"
    return "task_update"


def _task_notification_actor_payload(user) -> dict:
    if not user:
        return {}
    return {
        "actor_user_id": user.id,
        "actor_name": display_name_for_user(user) or user.email or "Someone",
        "actor_avatar_url": user.avatar_url or "",
    }


def _task_changed_field_labels(
    *,
    old_title: str | None,
    new_title: str | None,
    old_status: str | None,
    new_status: str | None,
    old_due_at,
    new_due_at,
    old_due_mode: str | None,
    new_due_mode: str | None,
    old_info_payload: dict | None,
    new_info_payload: dict | None,
    old_per_user_status_enabled: bool,
    new_per_user_status_enabled: bool,
    old_assign_group_members: bool,
    new_assign_group_members: bool,
    old_follow_project_members: bool,
    new_follow_project_members: bool,
    old_description: str | None,
    new_description: str | None,
    old_description_format: str | None,
    new_description_format: str | None,
) -> list[str]:
    labels: list[str] = []
    if (old_title or "").strip() != (new_title or "").strip():
        labels.append("title")
    if (old_status or "").strip() != (new_status or "").strip():
        labels.append("status")
    if (old_due_mode or "none") != (new_due_mode or "none") or bool(old_due_at) != bool(new_due_at) or (old_due_at and new_due_at and old_due_at != new_due_at):
        labels.append("due date")
    old_links = list((old_info_payload or {}).get("links") or [])
    new_links = list((new_info_payload or {}).get("links") or [])
    if old_links != new_links:
        labels.append("links")
    old_html = str((old_info_payload or {}).get("html") or "")
    new_html = str((new_info_payload or {}).get("html") or "")
    if old_html != new_html:
        labels.append("notes")
    if bool(old_per_user_status_enabled) != bool(new_per_user_status_enabled):
        labels.append("per-user status")
    if bool(old_assign_group_members) != bool(new_assign_group_members):
        labels.append("assignment mode")
    if bool(old_follow_project_members) != bool(new_follow_project_members):
        labels.append("follower mode")
    if (old_description or "") != (new_description or ""):
        labels.append("description")
    if (old_description_format or "") != (new_description_format or ""):
        labels.append("description format")
    return labels


def _queue_project_shared_notification(member_user, actor_user, project) -> bool:
    if not member_user or not actor_user or not project:
        return False
    if int(actor_user.id) == int(member_user.id):
        return False
    preview_payload = {
        "id": "live-project-shared:" + str(project.id) + ":" + str(actor_user.id),
        "task_id": "",
        "project_id": project.id,
        "project_name": _project_display_name_for_user(project, member_user.id),
        "group_id": "",
        "group_name": "",
        "task_title": _project_display_name_for_user(project, member_user.id),
        "kind": "project_shared",
        "summary": "Added to project",
        "preview": (_task_notification_actor_payload(actor_user).get("actor_name") or "Someone") + " added you to " + _project_display_name_for_user(project, member_user.id),
        "comment_preview": "",
        "created_at": datetime.utcnow().isoformat(),
        "created_at_iso": datetime.utcnow().isoformat(),
        "read": False,
        "pinned": False,
        "detail_payload": {
            **_task_notification_actor_payload(actor_user),
            "project_id": project.id,
            "project_name": _project_display_name_for_user(project, member_user.id),
        },
    }
    push_enabled = user_notification_channel_enabled(member_user.id, "project_added", "push")
    email_enabled = user_notification_channel_enabled(member_user.id, "project_added", "email")
    if push_enabled:
        emit_user_notification_preview(member_user.id, preview_payload)
    try:
        if not push_enabled and not email_enabled:
            return False
        queue_user_notification(
            user_id=member_user.id,
            kind="project_shared",
            detail_payload={
                **_task_notification_actor_payload(actor_user),
                "project_id": project.id,
                "project_name": _project_display_name_for_user(project, member_user.id),
            },
        )
        db.session.commit()
        emit_notification_state(member_user.id)
        return True
    except Exception:
        db.session.rollback()
        current_app.logger.exception("project_shared notification failed")
        return False


def _queue_project_removed_notification(member_user, actor_user, project) -> bool:
    if not member_user or not actor_user or not project:
        return False
    if int(actor_user.id) == int(member_user.id):
        return False
    project_name = _project_display_name_for_user(project, member_user.id)
    preview_payload = {
        "id": "live-project-removed:" + str(project.id) + ":" + str(actor_user.id),
        "task_id": "",
        "project_id": project.id,
        "project_name": project_name,
        "group_id": "",
        "group_name": "",
        "task_title": project_name,
        "kind": "project_removed",
        "summary": "Removed from project",
        "preview": (_task_notification_actor_payload(actor_user).get("actor_name") or "Someone") + " removed you from " + project_name,
        "comment_preview": "",
        "created_at": datetime.utcnow().isoformat(),
        "created_at_iso": datetime.utcnow().isoformat(),
        "read": False,
        "pinned": False,
        "detail_payload": {
            **_task_notification_actor_payload(actor_user),
            "project_id": project.id,
            "project_name": project_name,
        },
    }
    push_enabled = user_notification_channel_enabled(member_user.id, "project_removed", "push")
    email_enabled = user_notification_channel_enabled(member_user.id, "project_removed", "email")
    if push_enabled:
        emit_user_notification_preview(member_user.id, preview_payload)
    try:
        if not push_enabled and not email_enabled:
            return False
        queue_user_notification(
            user_id=member_user.id,
            kind="project_removed",
            detail_payload={
                **_task_notification_actor_payload(actor_user),
                "project_id": project.id,
                "project_name": project_name,
            },
        )
        db.session.commit()
        emit_notification_state(member_user.id)
        return True
    except Exception:
        db.session.rollback()
        current_app.logger.exception("project_removed notification failed")
        return False


def _queue_group_assignment_notifications(task: Task, assignments: list[Assignment], actor_user) -> None:
    if not task or not assignments:
        return
    total_assignees = len(group_assignment_candidate_users(task))
    recipient_ids: set[int] = set()
    project_label = task.project.name if getattr(task, "project", None) and task.project.name else (task.title or "Task")
    for assignment in assignments:
        if not assignment.user_id:
            continue
        if actor_user and int(assignment.user_id) == int(actor_user.id):
            continue
        recipient_ids.add(int(assignment.user_id))
        queue_user_notification(
            user_id=assignment.user_id,
            kind="assignment_added",
            task_id=task.id,
            detail_payload={
                **_task_notification_actor_payload(actor_user),
                "assignee_user_id": assignment.user_id,
                "group_assign": True,
                "group_assign_total": total_assignees,
                "group_assign_other_count": max(total_assignees - 1, 0),
                "project_name": project_label,
                "task_title": task.title or "Task",
            },
        )
    if not recipient_ids:
        return
    db.session.commit()
    for recipient_id in sorted(recipient_ids):
        emit_notification_state(recipient_id)


def _assignment_recipient_phrase(
    task: Task,
    *,
    recipient_user_id: int,
    fallback_label: str = "",
) -> str:
    assignment_rows = (
        Assignment.query.filter_by(task_id=task.id)
        .order_by(Assignment.created_at.asc(), Assignment.id.asc())
        .all()
    )
    assigned_users: list[User] = []
    seen_ids: set[int] = set()
    for row in assignment_rows:
        if not row.user_id or row.user_id in seen_ids:
            continue
        assignee = User.query.get(row.user_id)
        if not assignee:
            continue
        seen_ids.add(row.user_id)
        assigned_users.append(assignee)
    if not assigned_users:
        return fallback_label.strip() or "you"

    if any(int(assignee.id) == int(recipient_user_id) for assignee in assigned_users):
        other_labels = [
            display_name_for_user(assignee) or assignee.email or "Someone"
            for assignee in assigned_users
            if int(assignee.id) != int(recipient_user_id)
        ]
        if not other_labels:
            return "you"
        if len(other_labels) == 1:
            return "you and " + other_labels[0]
        return "you and " + str(len(other_labels)) + " others"

    labels = [display_name_for_user(assignee) or assignee.email or "Someone" for assignee in assigned_users]
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return labels[0] + " and " + labels[1]
    return labels[0] + " and " + str(len(labels) - 1) + " others"


def _queue_assignment_added_notifications(task: Task, *, actor_user, fallback_assignee_label: str = "") -> None:
    if not task:
        return
    recipient_ids = {
        int(recipient_id)
        for recipient_id in _task_notification_user_ids(task)
        if recipient_id and (not actor_user or int(recipient_id) != int(actor_user.id))
    }
    if not recipient_ids:
        return
    for recipient_id in sorted(recipient_ids):
        queue_user_notification(
            user_id=recipient_id,
            kind="assignment_added",
            task_id=task.id,
            detail_payload={
                **_task_notification_actor_payload(actor_user),
                "task_title": task.title or "Task",
                "recipient_assignment_phrase": _assignment_recipient_phrase(
                    task,
                    recipient_user_id=recipient_id,
                    fallback_label=fallback_assignee_label,
                ),
            },
        )
    db.session.commit()
    for recipient_id in sorted(recipient_ids):
        emit_notification_state(recipient_id)


def _emit_comment_notification_previews(
    *,
    recipient_ids: set[int] | list[int],
    actor_user,
    task_id: int | None = None,
    project_id: int | None = None,
    group_id: int | None = None,
    task_title: str = "",
    project_name: str = "",
    preview: str = "",
    exclude_user_id: int | None = None,
) -> None:
    actor_name = (_task_notification_actor_payload(actor_user).get("actor_name") or "New message") if actor_user else "New message"
    actor_avatar_url = (_task_notification_actor_payload(actor_user).get("actor_avatar_url") or "") if actor_user else ""
    created_at = datetime.utcnow().isoformat()
    task = Task.query.get(task_id) if task_id else None
    for recipient_id in sorted({int(user_id) for user_id in (recipient_ids or []) if user_id}):
        if exclude_user_id is not None and int(recipient_id) == int(exclude_user_id):
            continue
        event_key = "task_message"
        if group_id:
            event_key = "group_message"
        elif project_id and not task_id:
            event_key = "project_message"
        if not user_notification_channel_enabled(recipient_id, event_key, "push", task=task):
            continue
        if task_id and is_user_viewing_discussion(recipient_id, "task", int(task_id)):
            continue
        if group_id and is_user_viewing_discussion(recipient_id, "group", int(group_id)):
            continue
        if project_id and not group_id and not task_id and is_user_viewing_discussion(recipient_id, "project", int(project_id)):
            continue
        live_id_parts = ["live-comment", str(task_id or ""), str(project_id or ""), str(group_id or ""), created_at]
        emit_user_notification_preview(
            recipient_id,
            {
                "id": ":".join(live_id_parts),
                "task_id": task_id or "",
                "project_id": project_id or "",
                "project_name": project_name or "",
                "group_id": group_id or "",
                "group_name": "",
                "task_title": task_title or "Task",
                "kind": "comment",
                "summary": actor_name + " commented",
                "preview": preview or "New comment",
                "comment_preview": preview or "New comment",
                "created_at": created_at,
                "created_at_iso": created_at,
                "read": False,
                "pinned": False,
                "sender_name": actor_name,
                "unread_count": 1,
                "detail_payload": {
                    "actor_name": actor_name,
                    "actor_avatar_url": actor_avatar_url,
                },
            },
        )


def _coerce_description_format(value: str | None, default: str) -> str | None:
    if value is None:
        return default
    normalized = (str(value) or "").strip().lower()
    if not normalized:
        return default
    if normalized not in DESCRIPTION_FORMAT_OPTIONS:
        return None
    return normalized


def _render_description(description: str | None, description_format: str | None, default_format: str) -> str:
    if not description:
        return ""
    fmt = (description_format or default_format or "").strip().lower()
    if fmt == "markdown":
        rendered = render_markdown(description, extensions=["extra", "sane_lists"])
        return sanitize_info_html(rendered)
    if fmt == "html":
        return sanitize_info_html(description)
    escaped = escape(description)
    paragraphs = []
    for block in re.split(r"\\n\\s*\\n", escaped.strip()):
        if not block:
            continue
        paragraphs.append(f"<p>{block.replace('\\n', '<br />')}</p>")
    return "".join(paragraphs)


def _render_comment_body(body: str | None) -> str:
    if not body:
        return ""
    rendered = render_markdown(body, extensions=["extra", "sane_lists"])
    return sanitize_info_html(rendered)


def _task_due_mode(task: Task) -> str:
    info = load_info_payload(getattr(task, "info", None), getattr(task, "link", None))
    mode = str(info.get("meta", {}).get("due_mode") or "").strip().lower()
    if mode in {"asap", "date"}:
        return mode
    if task.due_at:
        return "date"
    return "none"


def _normalize_due_mode(value: str | None, *, fallback: str = "date") -> str:
    mode = (str(value or fallback).strip().lower() or fallback)
    return mode if mode in TASK_DUE_MODE_OPTIONS else fallback


def _apply_task_due_payload(task: Task, *, due_at_raw, due_mode_raw) -> tuple[bool, str | None]:
    info_payload = load_info_payload(task.info, task.link)
    meta = dict(info_payload.get("meta") or {})
    mode = _normalize_due_mode(due_mode_raw, fallback="date" if due_at_raw else "none")
    if mode == "none":
        task.due_at = None
        meta.pop("due_mode", None)
    elif mode == "asap":
        task.due_at = None
        meta["due_mode"] = "asap"
    else:
        if due_at_raw == "":
            task.due_at = None
        elif due_at_raw:
            try:
                task.due_at = datetime.fromisoformat(due_at_raw)
            except ValueError:
                return False, "Invalid due_at format"
        meta["due_mode"] = "date"
    info_payload["meta"] = meta
    task.info = normalize_info_payload(info_payload, task.link)
    return True, None

@api_bp.get("/me")
@login_required
def me():
    user = current_user()
    return {"user": {"id": user.id, "email": user.email, "display_name": user.display_name}}


@api_bp.get("/notifications")
@login_required
def notifications_feed():
    user = current_user()
    return notification_payload_for_user(user.id)


@api_bp.post("/notifications/<int:notification_id>/dismiss")
@login_required
def dismiss_notification(notification_id: int):
    user = current_user()
    notification = TaskNotification.query.filter_by(id=notification_id, user_id=user.id).first()
    if not notification:
        return {"error": "notification not found"}, 404
    notification.read_at = datetime.utcnow()
    notification.pinned = False
    db.session.commit()
    emit_notification_state(user.id)
    return {"status": "ok"}, 200


@api_bp.post("/notifications/preview/dismiss")
@login_required
def dismiss_notification_preview():
    user = current_user()
    payload = request.get_json(silent=True) or {}
    notification_id = str(payload.get("notification_id") or "").strip()
    if not notification_id:
        return {"error": "notification_id is required"}, 400
    emit_notification_preview_dismissed(user.id, notification_id)
    return {"status": "ok"}, 200


@api_bp.post("/notifications/<int:notification_id>/pin")
@login_required
def pin_notification(notification_id: int):
    user = current_user()
    notification = TaskNotification.query.filter_by(id=notification_id, user_id=user.id).first()
    if not notification:
        return {"error": "notification not found"}, 404
    notification.pinned = True
    db.session.commit()
    emit_notification_state(user.id)
    return {"status": "ok"}, 200


@api_bp.post("/notifications/<int:notification_id>/unpin")
@login_required
def unpin_notification(notification_id: int):
    user = current_user()
    notification = TaskNotification.query.filter_by(id=notification_id, user_id=user.id).first()
    if not notification:
        return {"error": "notification not found"}, 404
    notification.pinned = False
    db.session.commit()
    emit_notification_state(user.id)
    return {"status": "ok"}, 200


@api_bp.post("/github/sync")
@login_required
def sync_github():
    user = current_user()
    identity = github_identity_for_user(user.id)
    if not identity or not identity.access_token:
        return {"error": "github not connected"}, 400
    if not should_sync_github_issues(user.id):
        return {"ok": True, "skipped": True}
    try:
        result = sync_github_issues_for_user(user)
    except GitHubSyncError as exc:
        current_app.logger.warning("Background GitHub sync skipped for user %s: %s", user.id, exc)
        return {"error": str(exc)}, 400
    return {"ok": True, "result": result}


def _can_access_task(user, task: Task) -> bool:
    if not task:
        return False
    return _can_access_project(user, task.project_id)


def _task_notification_user_ids(task: Task) -> set[int]:
    user_ids: set[int] = set()
    if task.creator_user_id:
        user_ids.add(task.creator_user_id)
    user_ids.update(
        row.user_id
        for row in Assignment.query.filter_by(task_id=task.id).all()
        if row.user_id
    )
    user_ids.update(
        row.user_id
        for row in TaskFollower.query.filter_by(task_id=task.id).all()
        if row.user_id
    )
    return {user_id for user_id in user_ids if user_id}


def _serialize_task_comment(comment: TaskComment, author: User | None, collaborator: CollaboratorProfile | None = None) -> dict:
    if collaborator:
        label = collaborator.display_name or collaborator.email
        author_payload = {
            "id": collaborator.id,
            "display_name": label,
            "email": collaborator.email,
            "avatar_url": None,
            "kind": "collaborator",
        }
    else:
        label = display_name_for_user(author) or "Unknown user"
        author_payload = {
            "id": author.id if author else None,
            "display_name": label,
            "email": author.email if author else None,
            "avatar_url": author.avatar_url if author else None,
            "kind": "user",
        }
    return {
        "id": comment.id,
        "task_id": comment.task_id,
        "user_id": comment.user_id,
        "collaborator_id": comment.collaborator_id,
        "body": comment.body,
        "rendered_body": _render_comment_body(comment.body),
        "created_at": comment.created_at.isoformat(),
        "updated_at": comment.updated_at.isoformat(),
        "author": author_payload,
    }


def _serialize_simple_comment(comment, author: User | None, *, project_id: int | None = None, group_id: int | None = None) -> dict:
    label = display_name_for_user(author) or "Unknown user"
    author_payload = {
        "id": author.id if author else None,
        "display_name": label,
        "email": author.email if author else None,
        "avatar_url": author.avatar_url if author else None,
        "kind": "user",
    }
    payload = {
        "id": comment.id,
        "user_id": comment.user_id,
        "body": comment.body,
        "rendered_body": _render_comment_body(comment.body),
        "created_at": comment.created_at.isoformat(),
        "updated_at": comment.updated_at.isoformat(),
        "author": author_payload,
    }
    if project_id is not None:
        payload["project_id"] = project_id
    if group_id is not None:
        payload["group_id"] = group_id
    return payload


def _serialize_assignment_row(assignment: Assignment) -> dict:
    account_user = User.query.get(assignment.user_id) if assignment.user_id else None
    return {
        "id": assignment.id,
        "task_id": assignment.task_id,
        "user_id": assignment.user_id,
        "email": assignment.email,
        "status": assignment.status,
        "display_name": account_user.display_name if account_user else None,
        "display_email": account_user.email if account_user else assignment.email,
        "avatar_url": account_user.avatar_url if account_user else None,
    }


def _serialize_follower_row(follower: TaskFollower) -> dict:
    account_user = User.query.get(follower.user_id) if follower.user_id else None
    return {
        "id": follower.id,
        "task_id": follower.task_id,
        "user_id": follower.user_id,
        "display_name": account_user.display_name if account_user else None,
        "display_email": account_user.email if account_user else None,
        "avatar_url": account_user.avatar_url if account_user else None,
    }


def _project_access_user_ids(project_id: int) -> list[int]:
    return sorted(
        int(user_id)
        for user_id in project_access_map(project_id).keys()
        if user_id
    )


def _task_follow_project_members(task: Task) -> bool:
    info_payload = load_info_payload(task.info, task.link)
    meta = info_payload.get("meta") if isinstance(info_payload, dict) else {}
    return str((meta or {}).get("follow_project_members") or "").strip().lower() in {"1", "true", "yes", "on"}


def _set_task_follow_project_members(task: Task, enabled: bool) -> None:
    info_payload = load_info_payload(task.info, task.link)
    meta = dict(info_payload.get("meta") or {})
    if enabled:
        meta["follow_project_members"] = "1"
    else:
        meta.pop("follow_project_members", None)
    info_payload["meta"] = meta
    task.info = normalize_info_payload(info_payload, task.link)


def _serialize_project_follower_members(task: Task) -> list[dict]:
    members = list(project_access_map(task.project_id).values())
    members.sort(key=lambda item: ((item.get("display_name") or item.get("email") or "").lower(), item.get("id") or 0))
    return members


def _account_user_has_project_access(project_id: int, account_user_id: int | None) -> bool:
    if not account_user_id:
        return False
    project = Project.query.get(project_id)
    if project and int(project.owner_id or 0) == int(account_user_id):
        return True
    if ProjectMember.query.filter_by(project_id=project_id, user_id=account_user_id).first():
        return True
    return (
        db.session.query(Group.id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(Group.project_id == project_id, GroupMember.user_id == account_user_id)
        .first()
        is not None
    )


def _assignment_requires_project_share_payload(project: Project | None, account_user: User | None) -> dict | None:
    if not project or not account_user:
        return None
    if _account_user_has_project_access(project.id, account_user.id):
        return None
    return {
        "error": "project share required",
        "requires_project_member": True,
        "project": {
            "id": project.id,
            "name": project.name,
        },
        "user": {
            "id": account_user.id,
            "email": account_user.email,
            "display_name": account_user.display_name,
            "avatar_url": account_user.avatar_url,
        },
    }


def _merge_comment_links(item, body: str) -> bool:
    candidates = []
    for match in URL_PATTERN.findall(body or ""):
        link = match.rstrip('.,;:!?)]}\'"')
        parsed = urlparse(link)
        if parsed.scheme not in {"http", "https"}:
            continue
        if link not in candidates:
            candidates.append(link)
    if not candidates:
        return False
    info_payload = load_info_payload(getattr(item, "info", None), getattr(item, "link", None))
    links = list(info_payload.get("links", []))
    changed = False
    for link in candidates:
        if link in links:
            continue
        links.append(link)
        changed = True
    if not changed:
        return False
    info_payload["links"] = links
    item.link = links[0] if links else None
    item.info = normalize_info_payload(info_payload, item.link)
    return True


def _serialize_task_row(task: Task, *, viewer_user_id: int | None = None) -> dict:
    info = load_info_payload(task.info)
    assignments = (
        Assignment.query.filter_by(task_id=task.id)
        .order_by(Assignment.created_at.asc(), Assignment.id.asc())
        .all()
    )
    followers = (
        TaskFollower.query.filter_by(task_id=task.id)
        .order_by(TaskFollower.created_at.asc(), TaskFollower.id.asc())
        .all()
    )
    status_meta = task_status_meta(task, viewer_user_id=viewer_user_id)
    creator = User.query.get(task.creator_user_id) if task.creator_user_id else None
    return {
        "id": task.id,
        "project_id": task.project_id,
        "group_id": task.group_id,
        "creator": {
            "id": creator.id,
            "display_name": creator.display_name,
            "email": creator.email,
            "avatar_url": creator.avatar_url,
        } if creator else None,
        "position": task.position,
        "title": task.title,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "due_mode": _task_due_mode(task),
        "status": task.status,
        "per_user_status_enabled": bool(task.per_user_status_enabled),
        "assign_group_members": bool(task.assign_group_members),
        "group_assignment_members": serialize_group_assignment_members(task) if task.assign_group_members else [],
        "follow_project_members": _task_follow_project_members(task),
        "project_follower_members": _serialize_project_follower_members(task) if _task_follow_project_members(task) else [],
        "status_meta": status_meta,
        "description": task.description,
        "description_format": task.description_format or DEFAULT_TASK_DESCRIPTION_FORMAT,
        "rendered_description": _render_description(
            task.description,
            task.description_format,
            DEFAULT_TASK_DESCRIPTION_FORMAT,
        ),
        "info": info,
        "assignments": [_serialize_assignment_row(row) for row in assignments],
        "followers": [_serialize_follower_row(row) for row in followers],
    }


def _can_manage_project(user, project_id: int) -> bool:
    if Project.query.filter_by(id=project_id, owner_id=user.id).first() is not None:
        return True
    if ProjectMember.query.filter_by(project_id=project_id, user_id=user.id).first() is not None:
        return True
    return (
        db.session.query(Group.id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(Group.project_id == project_id, GroupMember.user_id == user.id)
        .first()
        is not None
    )


def _can_manage_task_bucket(user, project_id: int, group_id: int | None) -> bool:
    return _can_manage_project(user, project_id)


def _can_access_project(user, project_id: int) -> bool:
    if Project.query.filter_by(id=project_id, owner_id=user.id).first():
        return True
    if ProjectMember.query.filter_by(project_id=project_id, user_id=user.id).first():
        return True
    if (
        db.session.query(Group.id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(Group.project_id == project_id, GroupMember.user_id == user.id)
        .first()
        is not None
    ):
        return True
    return False


def _can_access_group(user, group_id: int) -> bool:
    group = Group.query.get(group_id)
    if not group:
        return False
    return _can_access_project(user, group.project_id)


def _accessible_projects_for_user(user) -> list[Project]:
    owned_projects = Project.query.filter_by(owner_id=user.id).all()
    member_project_ids = [row.project_id for row in ProjectMember.query.filter_by(user_id=user.id).all()]
    member_group_project_ids = [
        row.project_id
        for row in db.session.query(Group.project_id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(GroupMember.user_id == user.id)
        .all()
    ]
    accessible_project_ids = list(
        {project.id for project in owned_projects}
        .union(member_project_ids)
        .union(member_group_project_ids)
    )
    return Project.query.filter(Project.id.in_(accessible_project_ids)).all() if accessible_project_ids else []


def _project_display_name_for_user(project: Project, user_id: int) -> str:
    if project and getattr(project, "is_direct", False):
        peer_id = None
        if project.direct_user_a_id and int(project.direct_user_a_id) != int(user_id):
            peer_id = project.direct_user_a_id
        elif project.direct_user_b_id and int(project.direct_user_b_id) != int(user_id):
            peer_id = project.direct_user_b_id
        if peer_id:
            peer = User.query.get(peer_id)
            if peer:
                return peer.display_name or peer.email or project.name
    return project.name if project else ""


def _sidebar_order_tokens(user) -> list[str]:
    accessible_projects = _accessible_projects_for_user(user)
    divisions = Division.query.filter_by(owner_id=user.id).order_by(Division.position.asc(), Division.id.asc()).all()
    pref_map = sidebar_preference_map(user.id, accessible_projects, divisions)
    items = top_level_sidebar_items(accessible_projects, divisions, pref_map)
    return [f"{item['type']}:{item['id']}" for item in items]


def _task_bucket_query(project_id: int, group_id: int | None):
    query = Task.query.filter_by(project_id=project_id)
    if group_id is None:
        query = query.filter(Task.group_id.is_(None))
    else:
        query = query.filter_by(group_id=group_id)
    return query.order_by(Task.position.asc(), Task.id.asc())


def _next_task_position(project_id: int, group_id: int | None) -> int:
    query = db.session.query(db.func.max(Task.position)).filter(Task.project_id == project_id)
    if group_id is None:
        query = query.filter(Task.group_id.is_(None))
    else:
        query = query.filter(Task.group_id == group_id)
    return (query.scalar() or 0) + 1


def _resequence_tasks(project_id: int, group_id: int | None, exclude_task_id: int | None = None) -> None:
    tasks = _task_bucket_query(project_id, group_id).all()
    pos = 1
    for task in tasks:
        if exclude_task_id and task.id == exclude_task_id:
            continue
        task.position = pos
        pos += 1


def _info_payload_for(item) -> dict:
    return load_info_payload(getattr(item, "info", None), getattr(item, "link", None))


def _upload_root() -> Path:
    return Path(current_app.instance_path) / "uploads"


def _existing_assignment_for_target(
    *,
    task_id: int | None = None,
    account_user_id: int | None = None,
    email: str | None = None,
) -> Assignment | None:
    query = Assignment.query
    if task_id is None:
        query = query.filter(Assignment.task_id.is_(None))
    else:
        query = query.filter_by(task_id=task_id)
    normalized_email = (email or "").strip().lower()
    if account_user_id:
        existing = query.filter_by(user_id=account_user_id).first()
        if existing:
            return existing
    if normalized_email:
        return query.filter(db.func.lower(Assignment.email) == normalized_email).first()
    return None


def _reset_existing_assignment(existing: Assignment) -> Assignment:
    existing.status = "assigned" if existing.user_id else "draft"
    Invite.query.filter_by(assignment_id=existing.id).delete(synchronize_session=False)
    return existing


def _matching_assignments_for_target(
    *,
    task_id: int,
    account_user_id: int | None = None,
    email: str | None = None,
) -> list[Assignment]:
    query = Assignment.query.filter_by(task_id=task_id)
    normalized_email = (email or "").strip().lower()
    if account_user_id:
        return query.filter_by(user_id=account_user_id).order_by(Assignment.id.asc()).all()
    if normalized_email:
        return query.filter(db.func.lower(Assignment.email) == normalized_email).order_by(Assignment.id.asc()).all()
    return []


def _collapse_duplicate_task_assignments(
    *,
    task_id: int,
    account_user_id: int | None = None,
    email: str | None = None,
) -> Assignment | None:
    matches = _matching_assignments_for_target(task_id=task_id, account_user_id=account_user_id, email=email)
    if not matches:
        return None
    primary = matches[0]
    for duplicate in matches[1:]:
        Invite.query.filter_by(assignment_id=duplicate.id).delete(synchronize_session=False)
        db.session.delete(duplicate)
    return primary


@api_bp.post("/tasks")
@login_required
def create_task():
    payload = request.get_json(silent=True) or {}
    user = current_user()
    title = (payload.get("title") or "").strip()
    project_id = payload.get("project_id")
    group_id = payload.get("group_id")
    due_at = payload.get("due_at")
    due_mode = payload.get("due_mode")
    assignee_email = payload.get("assignee_email")

    if not title or not project_id:
        return {"error": "title and project_id are required"}, 400

    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404

    group = None
    target_group_id = None if group_id in (None, "", "null") else group_id
    if target_group_id is not None:
        try:
            target_group_id = int(target_group_id)
        except (TypeError, ValueError):
            return {"error": "invalid group id"}, 400
        group = Group.query.filter_by(id=target_group_id, project_id=project.id).first()
        if not group:
            return {"error": "group not found"}, 404

    if not _can_manage_task_bucket(user, project.id, group.id if group else None):
        return {"error": "unauthorized"}, 403

    task = Task(
        project_id=project.id,
        group_id=group.id if group else None,
        creator_user_id=user.id,
        position=_next_task_position(project.id, group.id if group else None),
        title=title,
        description=payload.get("description"),
        info=normalize_info_payload(payload.get("info"), payload.get("link")),
        due_at=None,
        owner_calendar_opt_in=project.default_owner_calendar_opt_in,
    )
    ok, error = _apply_task_due_payload(task, due_at_raw=due_at, due_mode_raw=due_mode)
    if not ok:
        return {"error": error}, 400
    db.session.add(task)
    db.session.commit()
    assignment_payload = None
    if assignee_email:
        email = assignee_email.strip().lower()
        account_user = find_user_by_email(email)
        project_share_required = _assignment_requires_project_share_payload(project, account_user)
        if project_share_required:
            return project_share_required, 200
        existing_assignment = _existing_assignment_for_target(
            task_id=task.id,
            account_user_id=account_user.id if account_user else None,
            email=email,
        )
        if existing_assignment:
            _reset_existing_assignment(existing_assignment)
            db.session.commit()
            task = Task.query.get(task.id) or task
            task_payload = _serialize_task_row(task, viewer_user_id=user.id)
            emit_task_updated(task, action="created", actor_user_id=user.id)
            return {
                "task": task_payload,
                "id": task.id,
                "title": task.title,
                "assignment": {
                    "id": existing_assignment.id,
                    "user_id": existing_assignment.user_id,
                    "email": existing_assignment.email,
                    "status": existing_assignment.status,
                    "display_name": account_user.display_name if account_user else None,
                    "display_email": account_user.email if account_user else existing_assignment.email,
                },
                "info": _info_payload_for(task),
            }, 200
        assignment = Assignment(
            task_id=task.id,
            user_id=account_user.id if account_user else None,
            email=email if not account_user else None,
            status="assigned" if account_user else "draft",
        )
        db.session.add(assignment)
        db.session.commit()
        assignment_payload = {
            "id": assignment.id,
            "user_id": assignment.user_id,
            "email": assignment.email,
            "status": assignment.status,
            "display_name": account_user.display_name if account_user else None,
            "display_email": account_user.email if account_user else assignment.email,
        }

    queue_task_notifications(
        task,
        exclude_user_id=user.id,
        kind="task_created",
        detail_payload=_task_notification_actor_payload(user),
    )
    db.session.commit()
    task = Task.query.get(task.id) or task
    task_payload = _serialize_task_row(task, viewer_user_id=user.id)
    emit_task_updated(task, action="created", actor_user_id=user.id)
    emit_task_notification_updates(task, exclude_user_id=user.id)
    return {
        "task": task_payload,
        "id": task.id,
        "title": task.title,
        "assignment": assignment_payload,
        "info": _info_payload_for(task),
    }, 201


@api_bp.patch("/tasks/<int:task_id>")
@login_required
def update_task(task_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()

    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404

    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    old_title = task.title
    old_status = task.status
    old_due_at = task.due_at
    old_due_mode = _task_due_mode(task)
    old_info_payload = _info_payload_for(task)
    old_per_user_status_enabled = bool(task.per_user_status_enabled)
    old_assign_group_members = bool(task.assign_group_members)
    old_follow_project_members = _task_follow_project_members(task)
    old_description = task.description
    old_description_format = task.description_format or DEFAULT_TASK_DESCRIPTION_FORMAT

    title = payload.get("title")
    link = payload.get("link")
    links = payload.get("links")
    status = payload.get("status")
    user_status = payload.get("user_status")
    status_user_id = payload.get("status_user_id")
    due_at = payload.get("due_at")
    due_mode = payload.get("due_mode")
    info = payload.get("info")
    per_user_status_enabled = payload.get("per_user_status_enabled")
    assign_group_members = payload.get("assign_group_members")
    follow_project_members = payload.get("follow_project_members")
    description = payload.get("description")
    description_format = payload.get("description_format")

    if title is not None:
        task.title = title.strip()
    info_payload = dict(old_info_payload)
    if links is None and link is not None:
        stripped_link = link.strip()
        links = [stripped_link] if stripped_link else []
    if links is not None:
        info_payload["links"] = links
        task.link = (info_payload.get("links") or [None])[0]
    if status is not None:
        task.status = status.strip() or task.status
    if per_user_status_enabled is not None:
        task.per_user_status_enabled = bool(per_user_status_enabled)
    if assign_group_members is not None:
        next_group_mode = bool(assign_group_members)
        task.assign_group_members = next_group_mode
        if next_group_mode:
            sync_group_task_assignments(task)
    if user_status is not None:
        target_user_id = user.id
        if status_user_id is not None:
            try:
                target_user_id = int(status_user_id)
            except (TypeError, ValueError):
                return {"error": "invalid status_user_id"}, 400
            is_assigned_target = Assignment.query.filter_by(task_id=task.id, user_id=target_user_id).first() is not None
            if not is_assigned_target:
                return {"error": "status user must be an assigned account"}, 400
        personal_row = set_task_user_status(task.id, target_user_id, user_status)
        if per_user_status_enabled is False and personal_row.status:
            task.status = personal_row.status
    if info is not None:
        if isinstance(info, dict):
            info_payload["html"] = info.get("html")
            info_payload["attachments"] = info.get("attachments", info_payload.get("attachments", []))
        else:
            info_payload["html"] = info
        task.info = normalize_info_payload(info_payload, task.link)
    elif links is not None:
        task.info = normalize_info_payload(info_payload, task.link)
    if due_at is not None or due_mode is not None:
        ok, error = _apply_task_due_payload(task, due_at_raw=due_at, due_mode_raw=due_mode)
        if not ok:
            return {"error": error}, 400
    if description is not None:
        task.description = description
    if description_format is not None:
        normalized = _coerce_description_format(description_format, DEFAULT_TASK_DESCRIPTION_FORMAT)
        if normalized is None:
            return {"error": "invalid description_format"}, 400
        task.description_format = normalized
    if follow_project_members is not None:
        _set_task_follow_project_members(task, bool(follow_project_members))
        if bool(follow_project_members):
            desired_user_ids = set(_project_access_user_ids(task.project_id))
            existing_rows = TaskFollower.query.filter_by(task_id=task.id).all()
            existing_by_user_id = {int(row.user_id): row for row in existing_rows if row.user_id}
            for row in existing_rows:
                if row.user_id and int(row.user_id) not in desired_user_ids:
                    db.session.delete(row)
            for follower_user_id in desired_user_ids:
                if follower_user_id not in existing_by_user_id:
                    db.session.add(TaskFollower(task_id=task.id, user_id=follower_user_id))
    db.session.commit()
    new_due_mode = _task_due_mode(task)
    notification_kind = _task_update_notification_kind(
        old_title=old_title,
        new_title=task.title,
        old_status=old_status,
        new_status=task.status,
        old_due_at=old_due_at,
        new_due_at=task.due_at,
        old_due_mode=old_due_mode,
        new_due_mode=new_due_mode,
        user_status=user_status,
    )
    detail_payload = _task_notification_actor_payload(user)
    if notification_kind == "task_renamed":
        detail_payload.update({"old_title": old_title or "", "new_title": task.title or ""})
    elif notification_kind in {"task_due_changed", "task_due_cleared", "task_due_asap"}:
        detail_payload.update({
            "old_due_mode": old_due_mode or "none",
            "new_due_mode": new_due_mode or "none",
            "old_due_at": old_due_at.isoformat() if old_due_at else "",
            "new_due_at": task.due_at.isoformat() if task.due_at else "",
            "old_due_label": old_due_at.strftime("%Y-%m-%d") if old_due_at else ("ASAP" if old_due_mode == "asap" else ""),
            "new_due_label": task.due_at.strftime("%Y-%m-%d") if task.due_at else ("ASAP" if new_due_mode == "asap" else ""),
        })
    elif notification_kind in {"task_status_changed", "task_completed"}:
        detail_payload.update({
            "old_status": old_status or "",
            "new_status": user_status or task.status or "",
        })
    elif notification_kind == "task_update":
        detail_payload.update({
            "changed_fields": _task_changed_field_labels(
                old_title=old_title,
                new_title=task.title,
                old_status=old_status,
                new_status=task.status,
                old_due_at=old_due_at,
                new_due_at=task.due_at,
                old_due_mode=old_due_mode,
                new_due_mode=new_due_mode,
                old_info_payload=old_info_payload,
                new_info_payload=_info_payload_for(task),
                old_per_user_status_enabled=old_per_user_status_enabled,
                new_per_user_status_enabled=bool(task.per_user_status_enabled),
                old_assign_group_members=old_assign_group_members,
                new_assign_group_members=bool(task.assign_group_members),
                old_follow_project_members=old_follow_project_members,
                new_follow_project_members=_task_follow_project_members(task),
                old_description=old_description,
                new_description=task.description,
                old_description_format=old_description_format,
                new_description_format=task.description_format or DEFAULT_TASK_DESCRIPTION_FORMAT,
            ),
        })
    queue_task_notifications(task, exclude_user_id=user.id, kind=notification_kind, detail_payload=detail_payload)
    db.session.commit()
    emit_task_updated(task, actor_user_id=user.id)
    emit_task_notification_updates(task, exclude_user_id=user.id)
    info_payload = _info_payload_for(task)
    status_meta = task_status_meta(task, viewer_user_id=user.id)
    assignments = Assignment.query.filter_by(task_id=task.id).all()
    followers = TaskFollower.query.filter_by(task_id=task.id).all()
    return {
        "id": task.id,
        "title": task.title,
        "creator": _serialize_task_row(task, viewer_user_id=user.id).get("creator"),
        "status": task.status,
        "per_user_status_enabled": bool(task.per_user_status_enabled),
        "assign_group_members": bool(task.assign_group_members),
        "group_assignment_members": serialize_group_assignment_members(task) if task.assign_group_members else [],
        "follow_project_members": _task_follow_project_members(task),
        "project_follower_members": _serialize_project_follower_members(task) if _task_follow_project_members(task) else [],
        "status_meta": status_meta,
        "user_status": status_meta.get("my_status"),
        "info": info_payload,
        "link": task.link,
        "links": info_payload.get("links", []),
        "assignments": [_serialize_assignment_row(row) for row in assignments],
        "followers": [_serialize_follower_row(row) for row in followers],
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "due_mode": _task_due_mode(task),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "description": task.description,
        "description_format": task.description_format or DEFAULT_TASK_DESCRIPTION_FORMAT,
        "rendered_description": _render_description(task.description, task.description_format, DEFAULT_TASK_DESCRIPTION_FORMAT),
    }, 200


@api_bp.get("/tasks/<int:task_id>")
@login_required
def get_task(task_id: int):
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403
    assignments = Assignment.query.filter_by(task_id=task.id).all()
    followers = TaskFollower.query.filter_by(task_id=task.id).all()
    return {
        "id": task.id,
        "project_id": task.project_id,
        "group_id": task.group_id,
        "group_name": (Group.query.get(task.group_id).name if task.group_id else None),
        "title": task.title,
        "creator": _serialize_task_row(task, viewer_user_id=user.id).get("creator"),
        "link": task.link,
        "links": _info_payload_for(task).get("links", []),
        "status": task.status,
        "per_user_status_enabled": bool(task.per_user_status_enabled),
        "assign_group_members": bool(task.assign_group_members),
        "group_assignment_members": serialize_group_assignment_members(task) if task.assign_group_members else [],
        "follow_project_members": _task_follow_project_members(task),
        "project_follower_members": _serialize_project_follower_members(task) if _task_follow_project_members(task) else [],
        "status_meta": task_status_meta(task, viewer_user_id=user.id),
        "description": task.description,
        "description_format": task.description_format or DEFAULT_TASK_DESCRIPTION_FORMAT,
        "rendered_description": _render_description(task.description, task.description_format, DEFAULT_TASK_DESCRIPTION_FORMAT),
        "assignments": [_serialize_assignment_row(row) for row in assignments],
        "followers": [_serialize_follower_row(row) for row in followers],
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "rendered_description": _render_description(task.description, task.description_format, DEFAULT_TASK_DESCRIPTION_FORMAT),
    }, 200


@api_bp.get("/projects/<int:project_id>")
@login_required
def get_project(project_id: int):
    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if not _can_access_project(user, project.id):
        return {"error": "unauthorized"}, 403
    return {
        "id": project.id,
        "name": project.name,
        "links": _info_payload_for(project).get("links", []),
        "description": project.description,
        "description_format": project.description_format or DEFAULT_PROJECT_DESCRIPTION_FORMAT,
        "rendered_description": _render_description(project.description, project.description_format, DEFAULT_PROJECT_DESCRIPTION_FORMAT),
        "is_direct": bool(project.is_direct),
        "created_at": project.created_at.isoformat() if project.created_at else None,
    }, 200


@api_bp.get("/projects/<int:project_id>/tree_snapshot")
@login_required
def get_project_tree_snapshot(project_id: int):
    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if not _can_access_project(user, project.id):
        return {"error": "unauthorized"}, 403

    show_completed = (request.args.get("show_completed") or "1") == "1"
    groups = Group.query.filter_by(project_id=project.id).order_by(Group.position.asc(), Group.id.asc()).all()
    tasks = (
        Task.query.filter_by(project_id=project.id)
        .order_by(Task.position.asc(), Task.id.asc())
        .all()
    )
    status_map = task_status_meta_map(tasks, viewer_user_id=user.id) if tasks else {}
    if not show_completed:
        tasks = [
            task
            for task in tasks
            if (effective_task_status_for_user(task, viewer_user_id=user.id, status_meta=status_map.get(task.id)) or "").strip().lower()
            not in {"complete", "completed", "done", "closed", "pr closed", "pr merged"}
        ]
    tasks_by_group: dict[int, list[dict]] = {group.id: [] for group in groups}
    ungrouped_tasks: list[dict] = []
    for task in tasks:
        row = _serialize_task_row(task, viewer_user_id=user.id)
        if task.group_id and task.group_id in tasks_by_group:
            tasks_by_group[task.group_id].append(row)
        else:
            ungrouped_tasks.append(row)

    return {
        "project": _serialize_project_payload_for_user(project, user.id),
        "can_manage_project": bool(_can_manage_project(user, project.id)),
        "groups": [
            {
                "id": group.id,
                "project_id": group.project_id,
                "name": group.name,
                "position": group.position,
                "links": _info_payload_for(group).get("links", []),
                "tasks": tasks_by_group.get(group.id, []),
            }
            for group in groups
        ],
        "ungrouped_tasks": ungrouped_tasks,
    }, 200


@api_bp.get("/groups/<int:group_id>")
@login_required
def get_group(group_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_access_project(user, group.project_id):
        return {"error": "unauthorized"}, 403
    return {
        "id": group.id,
        "project_id": group.project_id,
        "name": group.name,
        "links": _info_payload_for(group).get("links", []),
        "description": group.description,
        "description_format": group.description_format or DEFAULT_GROUP_DESCRIPTION_FORMAT,
        "rendered_description": _render_description(group.description, group.description_format, DEFAULT_GROUP_DESCRIPTION_FORMAT),
        "created_at": group.created_at.isoformat() if group.created_at else None,
    }, 200


@api_bp.get("/tasks/<int:task_id>/comments")
@login_required
def list_task_comments(task_id: int):
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    comments = TaskComment.query.filter_by(task_id=task.id).order_by(TaskComment.created_at.asc(), TaskComment.id.asc()).all()
    user_ids = {comment.user_id for comment in comments}
    collaborator_ids = {comment.collaborator_id for comment in comments if comment.collaborator_id}
    users = {row.id: row for row in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
    collaborators = {row.id: row for row in CollaboratorProfile.query.filter(CollaboratorProfile.id.in_(collaborator_ids)).all()} if collaborator_ids else {}
    unread_count = TaskNotification.query.filter_by(user_id=user.id, task_id=task.id).filter(TaskNotification.read_at.is_(None)).count()
    return {
        "comments": [_serialize_task_comment(comment, users.get(comment.user_id), collaborators.get(comment.collaborator_id)) for comment in comments],
        "unread_count": unread_count,
    }


@api_bp.post("/tasks/<int:task_id>/comments")
@login_required
def create_task_comment(task_id: int):
    payload = request.get_json(silent=True) or {}
    body = (payload.get("body") or "").strip()
    if not body:
        return {"error": "body is required"}, 400

    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    comment = TaskComment(task_id=task.id, user_id=user.id, body=body)
    db.session.add(comment)
    db.session.flush()
    links_changed = _merge_comment_links(task, body)
    activity_rows = upsert_discussion_activity(
        user_ids=task_discussion_user_ids(task),
        entity_type="task",
        entity_id=task.id,
        task_id=task.id,
        project_id=task.project_id,
        group_id=task.group_id,
        comment_id=comment.id,
        actor_user_id=user.id,
        preview=body,
        exclude_user_id=user.id,
    )

    queue_task_notifications(task, exclude_user_id=user.id, kind="comment", comment_id=comment.id)

    db.session.commit()
    comment_count = TaskComment.query.filter_by(task_id=task.id).count()
    if links_changed:
        emit_task_updated(task, actor_user_id=user.id)
    emit_task_comment_created(task.id, _serialize_task_comment(comment, user))
    _emit_comment_notification_previews(
        recipient_ids=task_discussion_user_ids(task),
        actor_user=user,
        task_id=task.id,
        project_id=task.project_id,
        group_id=task.group_id,
        task_title=task.title or "Task",
        project_name=task.project.name if getattr(task, "project", None) and task.project.name else "",
        preview=body,
        exclude_user_id=user.id,
    )
    for row in activity_rows:
        emit_discussion_activity_updated(row.user_id, row.id)
    emit_task_notification_updates(task, exclude_user_id=user.id)
    return {"comment": _serialize_task_comment(comment, user), "comment_count": comment_count}, 201


@api_bp.patch("/tasks/<int:task_id>/comments/<int:comment_id>")
@login_required
def update_task_comment(task_id: int, comment_id: int):
    payload = request.get_json(silent=True) or {}
    body = (payload.get("body") or "").strip()
    if not body:
        return {"error": "body is required"}, 400

    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    comment = TaskComment.query.filter_by(id=comment_id, task_id=task.id, user_id=user.id).first()
    if not comment:
        return {"error": "comment not found"}, 404

    comment.body = body
    db.session.commit()
    payload = _serialize_task_comment(comment, user)
    emit_task_comment_updated(task.id, payload)
    return {"comment": payload}, 200


@api_bp.delete("/tasks/<int:task_id>/comments/<int:comment_id>")
@login_required
def delete_task_comment(task_id: int, comment_id: int):
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    comment = TaskComment.query.filter_by(id=comment_id, task_id=task.id, user_id=user.id).first()
    if not comment:
        return {"error": "comment not found"}, 404

    db.session.delete(comment)
    db.session.commit()
    emit_task_comment_deleted(task.id, comment_id)
    return {"status": "ok", "comment_id": comment_id}, 200


@api_bp.get("/projects/<int:project_id>/comments")
@login_required
def list_project_comments(project_id: int):
    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if not _can_access_project(user, project.id):
        return {"error": "unauthorized"}, 403

    comments = ProjectComment.query.filter_by(project_id=project.id).order_by(ProjectComment.created_at.asc(), ProjectComment.id.asc()).all()
    user_ids = {comment.user_id for comment in comments}
    users = {row.id: row for row in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
    return {
        "comments": [_serialize_simple_comment(comment, users.get(comment.user_id), project_id=project.id) for comment in comments],
    }


@api_bp.post("/projects/<int:project_id>/comments")
@login_required
def create_project_comment(project_id: int):
    payload = request.get_json(silent=True) or {}
    body = (payload.get("body") or "").strip()
    if not body:
        return {"error": "body is required"}, 400

    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if not _can_access_project(user, project.id):
        return {"error": "unauthorized"}, 403

    comment = ProjectComment(project_id=project.id, user_id=user.id, body=body)
    db.session.add(comment)
    _merge_comment_links(project, body)
    db.session.flush()
    activity_rows = upsert_discussion_activity(
        user_ids=project_discussion_user_ids(project.id),
        entity_type="project",
        entity_id=project.id,
        project_id=project.id,
        comment_id=comment.id,
        actor_user_id=user.id,
        preview=body,
        exclude_user_id=user.id,
    )
    db.session.commit()
    comment_count = ProjectComment.query.filter_by(project_id=project.id).count()
    payload = _serialize_simple_comment(comment, user, project_id=project.id)
    emit_project_updated(project, actor_user_id=user.id)
    emit_project_comment_created(project.id, payload)
    _emit_comment_notification_previews(
        recipient_ids=project_discussion_user_ids(project.id),
        actor_user=user,
        project_id=project.id,
        task_title="Project chat",
        project_name=project.name or "Project",
        preview=body,
        exclude_user_id=user.id,
    )
    for row in activity_rows:
        emit_discussion_activity_updated(row.user_id, row.id)
    return {"comment": payload, "comment_count": comment_count}, 201


@api_bp.patch("/projects/<int:project_id>/comments/<int:comment_id>")
@login_required
def update_project_comment(project_id: int, comment_id: int):
    payload = request.get_json(silent=True) or {}
    body = (payload.get("body") or "").strip()
    if not body:
        return {"error": "body is required"}, 400

    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if not _can_access_project(user, project.id):
        return {"error": "unauthorized"}, 403

    comment = ProjectComment.query.filter_by(id=comment_id, project_id=project.id, user_id=user.id).first()
    if not comment:
        return {"error": "comment not found"}, 404

    comment.body = body
    db.session.commit()
    payload = _serialize_simple_comment(comment, user, project_id=project.id)
    emit_project_comment_updated(project.id, payload)
    return {"comment": payload}, 200


@api_bp.delete("/projects/<int:project_id>/comments/<int:comment_id>")
@login_required
def delete_project_comment(project_id: int, comment_id: int):
    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if not _can_access_project(user, project.id):
        return {"error": "unauthorized"}, 403

    comment = ProjectComment.query.filter_by(id=comment_id, project_id=project.id, user_id=user.id).first()
    if not comment:
        return {"error": "comment not found"}, 404

    db.session.delete(comment)
    db.session.commit()
    emit_project_comment_deleted(project.id, comment_id)
    return {"status": "ok", "comment_id": comment_id}, 200


@api_bp.get("/groups/<int:group_id>/comments")
@login_required
def list_group_comments(group_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_access_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    comments = GroupComment.query.filter_by(group_id=group.id).order_by(GroupComment.created_at.asc(), GroupComment.id.asc()).all()
    user_ids = {comment.user_id for comment in comments}
    users = {row.id: row for row in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
    return {
        "comments": [_serialize_simple_comment(comment, users.get(comment.user_id), group_id=group.id) for comment in comments],
    }


@api_bp.post("/groups/<int:group_id>/comments")
@login_required
def create_group_comment(group_id: int):
    payload = request.get_json(silent=True) or {}
    body = (payload.get("body") or "").strip()
    if not body:
        return {"error": "body is required"}, 400

    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_access_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    comment = GroupComment(group_id=group.id, user_id=user.id, body=body)
    db.session.add(comment)
    _merge_comment_links(group, body)
    db.session.flush()
    activity_rows = upsert_discussion_activity(
        user_ids=group_discussion_user_ids(group.id),
        entity_type="group",
        entity_id=group.id,
        project_id=group.project_id,
        group_id=group.id,
        comment_id=comment.id,
        actor_user_id=user.id,
        preview=body,
        exclude_user_id=user.id,
    )
    db.session.commit()
    comment_count = GroupComment.query.filter_by(group_id=group.id).count()
    payload = _serialize_simple_comment(comment, user, group_id=group.id)
    emit_group_updated(group, actor_user_id=user.id)
    emit_group_comment_created(group.id, payload)
    _emit_comment_notification_previews(
        recipient_ids=group_discussion_user_ids(group.id),
        actor_user=user,
        project_id=group.project_id,
        group_id=group.id,
        task_title=(group.name or "Group") + " chat",
        project_name=group.project.name if getattr(group, "project", None) and group.project.name else "",
        preview=body,
        exclude_user_id=user.id,
    )
    for row in activity_rows:
        emit_discussion_activity_updated(row.user_id, row.id)
    return {"comment": payload, "comment_count": comment_count}, 201


@api_bp.patch("/groups/<int:group_id>/comments/<int:comment_id>")
@login_required
def update_group_comment(group_id: int, comment_id: int):
    payload = request.get_json(silent=True) or {}
    body = (payload.get("body") or "").strip()
    if not body:
        return {"error": "body is required"}, 400

    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_access_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    comment = GroupComment.query.filter_by(id=comment_id, group_id=group.id, user_id=user.id).first()
    if not comment:
        return {"error": "comment not found"}, 404

    comment.body = body
    db.session.commit()
    payload = _serialize_simple_comment(comment, user, group_id=group.id)
    emit_group_comment_updated(group.id, payload)
    return {"comment": payload}, 200


@api_bp.delete("/groups/<int:group_id>/comments/<int:comment_id>")
@login_required
def delete_group_comment(group_id: int, comment_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_access_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    comment = GroupComment.query.filter_by(id=comment_id, group_id=group.id, user_id=user.id).first()
    if not comment:
        return {"error": "comment not found"}, 404

    db.session.delete(comment)
    db.session.commit()
    emit_group_comment_deleted(group.id, comment_id)
    return {"status": "ok", "comment_id": comment_id}, 200


@api_bp.post("/tasks/<int:task_id>/notifications/read")
@login_required
def mark_task_notifications_read(task_id: int):
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403
    now = datetime.utcnow()
    TaskNotification.query.filter_by(user_id=user.id, task_id=task.id, kind="comment").filter(TaskNotification.read_at.is_(None)).update(
        {"read_at": now},
        synchronize_session=False,
    )
    db.session.commit()
    payload = notification_payload_for_user(user.id)
    emit_notification_state(user.id)
    payload["status"] = "ok"
    return payload


@api_bp.post("/tasks/<int:task_id>/notifications/unread")
@login_required
def mark_task_notifications_unread(task_id: int):
    user = current_user()
    payload = request.get_json(silent=True) or {}
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    comment_id = payload.get("comment_id")
    comment = None
    if comment_id not in (None, "", 0, "0"):
        try:
            comment_id = int(comment_id)
        except (TypeError, ValueError):
            return {"error": "invalid comment_id"}, 400
        comment = TaskComment.query.filter_by(id=comment_id, task_id=task.id).first()
        if not comment:
            return {"error": "comment not found"}, 404

    existing = TaskNotification.query.filter_by(user_id=user.id, task_id=task.id, kind="comment").filter(TaskNotification.read_at.is_(None)).first()
    if not existing:
        latest_comment = comment or (
            TaskComment.query.filter_by(task_id=task.id)
            .order_by(TaskComment.created_at.desc(), TaskComment.id.desc())
            .first()
        )
        db.session.add(
            TaskNotification(
                user_id=user.id,
                task_id=task.id,
                comment_id=latest_comment.id if latest_comment else None,
                kind="comment",
            )
        )
        db.session.commit()

    payload = notification_payload_for_user(user.id)
    emit_notification_state(user.id)
    payload["status"] = "ok"
    return payload


@api_bp.post("/tasks/<int:task_id>/move")
@login_required
def move_task(task_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()

    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404

    source_project_id = task.project_id
    source_group_id = task.group_id
    target_project_id = payload.get("project_id")
    target_group_raw = payload.get("group_id")
    before_task_id = payload.get("before_task_id")

    if not target_project_id:
        return {"error": "project_id is required"}, 400
    target_project = Project.query.get(target_project_id)
    if not target_project:
        return {"error": "project not found"}, 404

    target_group_id = None if target_group_raw in (None, "", "null") else int(target_group_raw)
    if target_group_id is not None:
        target_group = Group.query.filter_by(id=target_group_id, project_id=target_project.id).first()
        if not target_group:
            return {"error": "group not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403
    if not _can_access_project(user, target_project.id):
        return {"error": "unauthorized"}, 403
    if target_group_id is not None and not _can_access_group(user, target_group_id):
        return {"error": "unauthorized"}, 403

    before_task = None
    if before_task_id:
        before_task = Task.query.get(before_task_id)
        if not before_task:
            return {"error": "before_task not found"}, 404
        if before_task.id == task.id:
            before_task = None
        elif before_task.project_id != target_project.id or before_task.group_id != target_group_id:
            return {"error": "before_task bucket mismatch"}, 400
        elif not _can_access_task(user, before_task):
            return {"error": "unauthorized"}, 403

    if source_project_id == target_project.id and source_group_id == target_group_id:
        siblings = [row for row in _task_bucket_query(target_project.id, target_group_id).all() if row.id != task.id]
    else:
        siblings = _task_bucket_query(target_project.id, target_group_id).all()

    insert_at = len(siblings)
    if before_task:
        for idx, sibling in enumerate(siblings):
            if sibling.id == before_task.id:
                insert_at = idx
                break

    task.project_id = target_project.id
    task.group_id = target_group_id
    siblings.insert(insert_at, task)

    for idx, sibling in enumerate(siblings, start=1):
        sibling.position = idx

    if source_project_id != target_project.id or source_group_id != target_group_id:
        _resequence_tasks(source_project_id, source_group_id, exclude_task_id=task.id)

    db.session.commit()
    queue_task_notifications(
        task,
        exclude_user_id=user.id,
        kind="task_moved",
        detail_payload={
            **_task_notification_actor_payload(user),
            "old_project_id": source_project_id,
            "old_group_id": source_group_id,
            "new_project_id": task.project_id,
            "new_group_id": task.group_id,
        },
    )
    db.session.commit()
    emit_task_updated(task, action="moved", old_project_id=source_project_id, old_group_id=source_group_id, actor_user_id=user.id)
    emit_task_notification_updates(task, exclude_user_id=user.id)
    return {
        "status": "ok",
        "task_id": task.id,
        "project_id": task.project_id,
        "group_id": task.group_id,
        "position": task.position,
    }, 200


@api_bp.patch("/groups/<int:group_id>")
@login_required
def update_group(group_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()
    name = payload.get("name")
    link = payload.get("link")
    links = payload.get("links")
    description = payload.get("description")
    description_format = payload.get("description_format")
    if name is None and links is None and link is None and description is None and description_format is None:
        return {"error": "name or links is required"}, 400

    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404

    if not _can_manage_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    if name is not None:
        group.name = name.strip() or group.name
    if description is not None:
        group.description = description
    if description_format is not None:
        normalized = _coerce_description_format(description_format, DEFAULT_GROUP_DESCRIPTION_FORMAT)
        if normalized is None:
            return {"error": "invalid description_format"}, 400
        group.description_format = normalized
    info_payload = _info_payload_for(group)
    if links is None and link is not None:
        stripped_link = link.strip()
        links = [stripped_link] if stripped_link else []
    if links is not None:
        info_payload["links"] = links
        group.link = (info_payload.get("links") or [None])[0]
        group.info = normalize_info_payload(info_payload, group.link)
    db.session.commit()
    emit_group_updated(group, actor_user_id=user.id)
    return {
        "id": group.id,
        "name": group.name,
        "links": _info_payload_for(group).get("links", []),
        "description": group.description,
        "description_format": group.description_format or DEFAULT_GROUP_DESCRIPTION_FORMAT,
        "rendered_description": _render_description(group.description, group.description_format, DEFAULT_GROUP_DESCRIPTION_FORMAT),
    }, 200


@api_bp.patch("/projects/<int:project_id>")
@login_required
def update_project(project_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()
    name = payload.get("name")
    division_id = payload.get("division_id", "__missing__")
    link = payload.get("link")
    links = payload.get("links")
    description = payload.get("description")
    description_format = payload.get("description_format")
    if (
        name is None
        and division_id == "__missing__"
        and links is None
        and link is None
        and description is None
        and description_format is None
    ):
        return {"error": "name, links, or division_id is required"}, 400
    if name is not None and not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    if (links is not None or link is not None) and not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    if (description is not None or description_format is not None) and not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    if division_id != "__missing__" and not _can_access_project(user, project_id):
        return {"error": "unauthorized"}, 403
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if name is not None:
        project.name = name.strip() or project.name
    if links is None and link is not None:
        stripped_link = link.strip()
        links = [stripped_link] if stripped_link else []
    if links is not None:
        info_payload = _info_payload_for(project)
        info_payload["links"] = links
        project.link = (info_payload.get("links") or [None])[0]
        project.info = normalize_info_payload(info_payload, project.link)
    if division_id != "__missing__":
        pref = ensure_sidebar_preference(user.id, project.id, default_division_id=project.division_id, default_position=project.position or 0)
        old_division_id = pref.division_id
        if division_id in (None, "", 0, "0"):
            resequence_top_level_items(user.id)
            max_top_pos = max(
                [division.position or 0 for division in Division.query.filter_by(owner_id=user.id).all()]
                + [row.position or 0 for row in ProjectSidebarPreference.query.filter_by(user_id=user.id, division_id=None).all() if row.project_id != project.id]
                + [0]
            )
            pref.division_id = None
            pref.position = max_top_pos + 1
            if old_division_id is not None:
                resequence_division_projects(user.id, old_division_id, exclude_project_id=project.id)
        else:
            try:
                division_id_int = int(division_id)
            except (TypeError, ValueError):
                return {"error": "invalid division_id"}, 400
            division = Division.query.filter_by(id=division_id_int, owner_id=user.id).first()
            if not division:
                return {"error": "division not found"}, 404
            pref.division_id = division.id
            sibling_prefs = (
                ProjectSidebarPreference.query.filter(
                    ProjectSidebarPreference.user_id == user.id,
                    ProjectSidebarPreference.division_id == division.id,
                    ProjectSidebarPreference.project_id != project.id,
                )
                .order_by(ProjectSidebarPreference.position.asc(), ProjectSidebarPreference.id.asc())
                .all()
            )
            pref.position = len(sibling_prefs) + 1
            if old_division_id is None:
                resequence_top_level_items(user.id)
            elif old_division_id != division.id:
                resequence_division_projects(user.id, old_division_id, exclude_project_id=project.id)
    if description is not None:
        project.description = description
    if description_format is not None:
        normalized = _coerce_description_format(description_format, DEFAULT_PROJECT_DESCRIPTION_FORMAT)
        if normalized is None:
            return {"error": "invalid description_format"}, 400
        project.description_format = normalized
    db.session.commit()
    emit_project_updated(project, actor_user_id=user.id)
    if division_id != "__missing__":
        emit_sidebar_reordered(user.id, _sidebar_order_tokens(user), actor_user_id=user.id)
    current_pref = ProjectSidebarPreference.query.filter_by(user_id=user.id, project_id=project.id).first()
    return {
        "id": project.id,
        "name": project.name,
        "division_id": current_pref.division_id if current_pref else None,
        "links": _info_payload_for(project).get("links", []),
        "description": project.description,
        "description_format": project.description_format or DEFAULT_PROJECT_DESCRIPTION_FORMAT,
        "rendered_description": _render_description(project.description, project.description_format, DEFAULT_PROJECT_DESCRIPTION_FORMAT),
    }, 200


@api_bp.post("/projects/<int:project_id>/move")
@login_required
def move_project(project_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if not _can_access_project(user, project.id):
        return {"error": "unauthorized"}, 403

    placement = (payload.get("placement") or "before").strip().lower()
    if placement not in {"before", "after", "append"}:
        return {"error": "invalid placement"}, 400

    target_project_id = payload.get("target_project_id")
    target_division_id = payload.get("target_division_id", "__missing__")
    insert_position = payload.get("insert_position")
    moving_pref = ensure_sidebar_preference(user.id, project.id, default_division_id=project.division_id, default_position=project.position or 0)
    old_division_id = moving_pref.division_id

    if target_project_id:
        try:
            target_project_id = int(target_project_id)
        except (TypeError, ValueError):
            return {"error": "invalid target_project_id"}, 400
        target_project = Project.query.get(target_project_id)
        if not target_project or target_project.id == project.id:
            return {"error": "target project not found"}, 404
        if not _can_access_project(user, target_project.id):
            return {"error": "unauthorized"}, 403
        target_pref = ensure_sidebar_preference(user.id, target_project.id, default_division_id=target_project.division_id, default_position=target_project.position or 0)

        if target_pref.division_id is None:
            accessible_projects = _accessible_projects_for_user(user)
            divisions = Division.query.filter_by(owner_id=user.id).order_by(Division.position.asc(), Division.id.asc()).all()
            pref_map = sidebar_preference_map(user.id, accessible_projects, divisions)
            combined = top_level_sidebar_items(accessible_projects, divisions, pref_map)
            target_index = next((idx for idx, item in enumerate(combined) if item["type"] == "project" and item["id"] == target_project.id), None)
            if target_index is None:
                return {"error": "target project not found"}, 404
            insert_index = target_index + (1 if placement == "after" else 0)
            apply_top_level_project_order(user.id, moving_pref, insert_index)
            if old_division_id is not None:
                resequence_division_projects(user.id, old_division_id, exclude_project_id=project.id)
        else:
            target_division_id = target_pref.division_id
            moving_pref.division_id = target_division_id
            siblings = (
                ProjectSidebarPreference.query.filter(
                    ProjectSidebarPreference.user_id == user.id,
                    ProjectSidebarPreference.division_id == target_division_id,
                    ProjectSidebarPreference.project_id != project.id,
                )
                .order_by(ProjectSidebarPreference.position.asc(), ProjectSidebarPreference.id.asc())
                .all()
            )
            target_index = next((idx for idx, item in enumerate(siblings) if item.project_id == target_project.id), None)
            if target_index is None:
                return {"error": "target project not found"}, 404
            insert_index = target_index + (1 if placement == "after" else 0)
            siblings.insert(insert_index, moving_pref)
            for index, item in enumerate(siblings, start=1):
                item.position = index
            if old_division_id is None:
                resequence_top_level_items(user.id)
            elif old_division_id != target_division_id:
                resequence_division_projects(user.id, old_division_id, exclude_project_id=project.id)
    else:
        if target_division_id in ("__missing__",):
            return {"error": "target is required"}, 400
        if target_division_id in (None, "", 0, "0"):
            if insert_position is None:
                top_count = ProjectSidebarPreference.query.filter(
                    ProjectSidebarPreference.user_id == user.id,
                    ProjectSidebarPreference.division_id.is_(None),
                    ProjectSidebarPreference.project_id != project.id,
                ).count()
                insert_index = top_count + len(Division.query.filter_by(owner_id=user.id).all())
            else:
                try:
                    insert_index = max(0, int(insert_position) - 1)
                except (TypeError, ValueError):
                    return {"error": "invalid insert_position"}, 400
            apply_top_level_project_order(user.id, moving_pref, insert_index)
            if old_division_id is not None:
                resequence_division_projects(user.id, old_division_id, exclude_project_id=project.id)
        else:
            try:
                target_division_id = int(target_division_id)
            except (TypeError, ValueError):
                return {"error": "invalid target_division_id"}, 400
            division = Division.query.filter_by(id=target_division_id, owner_id=user.id).first()
            if not division:
                return {"error": "division not found"}, 404
            moving_pref.division_id = division.id
            siblings = (
                ProjectSidebarPreference.query.filter(
                    ProjectSidebarPreference.user_id == user.id,
                    ProjectSidebarPreference.division_id == division.id,
                    ProjectSidebarPreference.project_id != project.id,
                )
                .order_by(ProjectSidebarPreference.position.asc(), ProjectSidebarPreference.id.asc())
                .all()
            )
            if placement == "append":
                siblings.append(moving_pref)
            else:
                try:
                    insert_index = max(0, int(insert_position or 1) - 1)
                except (TypeError, ValueError):
                    return {"error": "invalid insert_position"}, 400
                siblings.insert(min(insert_index, len(siblings)), moving_pref)
            for index, item in enumerate(siblings, start=1):
                item.position = index
            if old_division_id is None:
                resequence_top_level_items(user.id)
            elif old_division_id != division.id:
                resequence_division_projects(user.id, old_division_id, exclude_project_id=project.id)

    db.session.commit()
    emit_sidebar_reordered(user.id, _sidebar_order_tokens(user), actor_user_id=user.id)
    return {"status": "ok", "id": project.id, "division_id": moving_pref.division_id, "position": moving_pref.position}, 200


@api_bp.post("/divisions/reorder")
@login_required
def reorder_divisions():
    user = current_user()
    payload = request.get_json(silent=True) or {}
    order = payload.get("order") or []
    if not isinstance(order, list) or not order:
        return {"error": "order is required"}, 400

    divisions = Division.query.filter_by(owner_id=user.id).order_by(Division.position.asc(), Division.id.asc()).all()
    expected_ids = [division.id for division in divisions]
    try:
        order_ids = [int(item) for item in order]
    except (TypeError, ValueError):
        return {"error": "invalid order"}, 400
    if set(order_ids) != set(expected_ids):
        return {"error": "order mismatch"}, 400

    for index, division_id in enumerate(order_ids, start=1):
        Division.query.filter_by(id=division_id, owner_id=user.id).update({"position": index})

    db.session.commit()
    emit_sidebar_reordered(user.id, _sidebar_order_tokens(user), actor_user_id=user.id)
    return {"status": "ok"}, 200


@api_bp.post("/divisions/<int:division_id>/shift")
@login_required
def shift_division(division_id: int):
    user = current_user()
    payload = request.get_json(silent=True) or {}
    direction = (payload.get("direction") or "").strip().lower()
    if direction not in {"up", "down"}:
        return {"error": "invalid direction"}, 400

    divisions = Division.query.filter_by(owner_id=user.id).order_by(Division.position.asc(), Division.id.asc()).all()
    division_ids = [row.id for row in divisions]
    if division_id not in division_ids:
        return {"error": "division not found"}, 404

    index = division_ids.index(division_id)
    if direction == "up":
        if index == 0:
            return {"status": "ok", "moved": False}, 200
        swap_index = index - 1
    else:
        if index >= len(divisions) - 1:
            return {"status": "ok", "moved": False}, 200
        swap_index = index + 1

    divisions[index], divisions[swap_index] = divisions[swap_index], divisions[index]
    for position, division in enumerate(divisions, start=1):
        division.position = position

    db.session.commit()
    emit_sidebar_reordered(user.id, _sidebar_order_tokens(user), actor_user_id=user.id)
    return {
        "status": "ok",
        "moved": True,
        "order": [row.id for row in divisions],
    }, 200


@api_bp.patch("/divisions/<int:division_id>")
@login_required
def update_division(division_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()
    name = payload.get("name")
    color = payload.get("color")
    if name is None and color is None:
        return {"error": "name or color is required"}, 400
    division = Division.query.filter_by(id=division_id, owner_id=user.id).first()
    if not division:
        return {"error": "division not found"}, 404
    if name is not None:
        division.name = name.strip() or division.name
    if color is not None:
        color_value = (color or "").strip()
        if color_value and not (
            len(color_value) == 7
            and color_value.startswith("#")
            and all(ch in "0123456789abcdefABCDEF" for ch in color_value[1:])
        ):
            return {"error": "invalid color"}, 400
        division.color = color_value or None
    db.session.commit()
    emit_division_updated(division, actor_user_id=user.id, recipient_user_id=user.id)
    return {"id": division.id, "name": division.name, "color": division.color}, 200


@api_bp.delete("/projects/<int:project_id>")
@login_required
def delete_project(project_id: int):
    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if project.owner_id != user.id:
        return {"error": "unauthorized"}, 403

    recipient_user_ids = [project.owner_id] + [row.user_id for row in ProjectMember.query.filter_by(project_id=project.id).all()]
    recipient_user_ids = sorted({user_id for user_id in recipient_user_ids if user_id})
    group_ids = [g.id for g in Group.query.filter_by(project_id=project.id).all()]
    task_ids = [t.id for t in Task.query.filter_by(project_id=project.id).all()]
    Assignment.query.filter(Assignment.task_id.in_(task_ids)).delete(synchronize_session=False)
    Invite.query.filter(Invite.task_id.in_(task_ids)).delete(synchronize_session=False)
    Task.query.filter(Task.project_id == project.id).delete(synchronize_session=False)
    GroupMember.query.filter(GroupMember.group_id.in_(group_ids)).delete(synchronize_session=False)
    ProjectMember.query.filter(ProjectMember.project_id == project.id).delete(synchronize_session=False)
    ProjectSidebarPreference.query.filter_by(project_id=project.id).delete(synchronize_session=False)
    Group.query.filter(Group.project_id == project.id).delete(synchronize_session=False)
    db.session.delete(project)
    db.session.commit()
    emit_project_deleted(project.id, actor_user_id=user.id, task_ids=task_ids, recipient_user_ids=recipient_user_ids)
    return {"status": "deleted"}, 200


@api_bp.delete("/divisions/<int:division_id>")
@login_required
def delete_division(division_id: int):
    user = current_user()
    division = Division.query.filter_by(id=division_id, owner_id=user.id).first()
    if not division:
        return {"error": "division not found"}, 404

    resequence_top_level_items(user.id)
    max_top_pos = max(
        [division_row.position or 0 for division_row in Division.query.filter(Division.owner_id == user.id, Division.id != division.id).all()]
        + [pref.position or 0 for pref in ProjectSidebarPreference.query.filter_by(user_id=user.id, division_id=None).all()]
        + [0]
    )
    moved_prefs = (
        ProjectSidebarPreference.query.filter_by(user_id=user.id, division_id=division.id)
        .order_by(ProjectSidebarPreference.position.asc(), ProjectSidebarPreference.id.asc())
        .all()
    )
    for offset, pref in enumerate(moved_prefs, start=1):
        pref.division_id = None
        pref.position = max_top_pos + offset

    db.session.delete(division)
    db.session.commit()
    emit_division_deleted(division.id, actor_user_id=user.id, recipient_user_id=user.id)
    emit_sidebar_reordered(user.id, _sidebar_order_tokens(user), actor_user_id=user.id)
    return {"status": "deleted"}, 200


@api_bp.post("/projects/<int:project_id>/duplicate")
@login_required
def duplicate_project(project_id: int):
    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if project.owner_id != user.id:
        return {"error": "unauthorized"}, 403

    copy = Project(
        name=f"{project.name} Copy",
        owner_id=user.id,
        division_id=None,
        position=0,
        default_owner_calendar_opt_in=project.default_owner_calendar_opt_in,
        default_invitee_calendar_opt_in=project.default_invitee_calendar_opt_in,
    )
    db.session.add(copy)
    db.session.flush()
    source_pref = ProjectSidebarPreference.query.filter_by(user_id=user.id, project_id=project.id).first()
    source_division_id = source_pref.division_id if source_pref else None
    copy_pref_position = insert_project_position(user.id, source_division_id, (source_pref.position + 1) if source_pref else None)
    db.session.add(
        ProjectSidebarPreference(
            user_id=user.id,
            project_id=copy.id,
            division_id=source_division_id,
            position=copy_pref_position,
        )
    )

    group_map = {}
    for group in Group.query.filter_by(project_id=project.id).order_by(Group.position.asc()).all():
        new_group = Group(
            project_id=copy.id,
            name=group.name,
            position=group.position,
            color=group.color,
        )
        db.session.add(new_group)
        db.session.flush()
        group_map[group.id] = new_group.id

    task_map = {}
    for task in Task.query.filter_by(project_id=project.id).order_by(Task.position.asc(), Task.id.asc()).all():
        new_task = Task(
            project_id=copy.id,
            group_id=group_map.get(task.group_id),
            creator_user_id=user.id,
            position=task.position,
            title=task.title,
            description=task.description,
            due_at=task.due_at,
            status=task.status,
            owner_calendar_opt_in=task.owner_calendar_opt_in,
        )
        db.session.add(new_task)
        db.session.flush()
        task_map[task.id] = new_task.id

    db.session.commit()
    copy_pref = ProjectSidebarPreference.query.filter_by(user_id=user.id, project_id=copy.id).first()
    emit_project_created(copy, actor_user_id=user.id, recipient_user_id=user.id)
    emit_sidebar_reordered(user.id, _sidebar_order_tokens(user), actor_user_id=user.id)
    return {
        "id": copy.id,
        "name": copy.name,
        "division_id": copy_pref.division_id if copy_pref else None,
    }, 201


@api_bp.delete("/groups/<int:group_id>")
@login_required
def delete_group(group_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if group.project_id is None or not _can_manage_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    project_id = group.project_id
    task_ids = [t.id for t in Task.query.filter_by(group_id=group.id).all()]
    Assignment.query.filter(Assignment.task_id.in_(task_ids)).delete(synchronize_session=False)
    Invite.query.filter(Invite.task_id.in_(task_ids)).delete(synchronize_session=False)
    Task.query.filter(Task.group_id == group.id).delete(synchronize_session=False)
    GroupMember.query.filter(GroupMember.group_id == group.id).delete(synchronize_session=False)
    db.session.delete(group)
    db.session.commit()
    emit_group_deleted(group_id, project_id, actor_user_id=user.id, task_ids=task_ids)
    return {"status": "deleted"}, 200


@api_bp.post("/groups/<int:group_id>/duplicate")
@login_required
def duplicate_group(group_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_manage_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    max_pos = db.session.query(db.func.max(Group.position)).filter_by(project_id=group.project_id).scalar() or 0
    new_group = Group(
        project_id=group.project_id,
        name=f"{group.name} Copy",
        position=max_pos + 1,
        color=group.color,
    )
    db.session.add(new_group)
    db.session.flush()

    task_map = {}
    for task in Task.query.filter_by(group_id=group.id).order_by(Task.position.asc(), Task.id.asc()).all():
        new_task = Task(
            project_id=task.project_id,
            group_id=new_group.id,
            creator_user_id=user.id,
            position=task.position,
            title=task.title,
            description=task.description,
            due_at=task.due_at,
            status=task.status,
            owner_calendar_opt_in=task.owner_calendar_opt_in,
        )
        db.session.add(new_task)
        db.session.flush()
        task_map[task.id] = new_task.id

    db.session.commit()
    tasks = Task.query.filter_by(group_id=new_group.id).order_by(Task.position.asc(), Task.id.asc()).all()
    emit_group_created(new_group, actor_user_id=user.id)
    return {
        "id": new_group.id,
        "name": new_group.name,
        "project_id": new_group.project_id,
        "color": new_group.color,
        "tasks": [_serialize_task_row(task) for task in tasks],
    }, 201


@api_bp.post("/groups/<int:group_id>/promote")
@login_required
def promote_group_to_project(group_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    source_project = Project.query.get(group.project_id)
    if not source_project:
        return {"error": "project not found"}, 404
    if not _can_manage_project(user, source_project.id):
        return {"error": "unauthorized"}, 403

    new_project = Project(
        name=group.name,
        owner_id=source_project.owner_id,
        division_id=None,
        position=0,
        default_owner_calendar_opt_in=source_project.default_owner_calendar_opt_in,
        default_invitee_calendar_opt_in=source_project.default_invitee_calendar_opt_in,
    )
    db.session.add(new_project)
    db.session.flush()

    member_rows = ProjectMember.query.filter_by(project_id=source_project.id).all()
    member_user_ids = {row.user_id for row in member_rows if row.user_id and int(row.user_id) != int(new_project.owner_id)}
    for member_user_id in sorted(member_user_ids):
        db.session.add(ProjectMember(project_id=new_project.id, user_id=member_user_id))

    pref_user_ids = {source_project.owner_id}
    pref_user_ids.update(member_user_ids)
    for pref_user_id in list(pref_user_ids):
        source_pref = ProjectSidebarPreference.query.filter_by(user_id=pref_user_id, project_id=source_project.id).first()
        if not source_pref:
            continue
        pref_position = insert_project_position(pref_user_id, source_pref.division_id, (source_pref.position or 0) + 1)
        db.session.add(
            ProjectSidebarPreference(
                user_id=pref_user_id,
                project_id=new_project.id,
                division_id=source_pref.division_id,
                position=pref_position,
            )
        )

    moved_tasks = Task.query.filter_by(group_id=group.id).order_by(Task.position.asc(), Task.id.asc()).all()
    old_group_id = group.id
    source_project_id = source_project.id
    for index, task in enumerate(moved_tasks):
        task.project_id = new_project.id
        task.group_id = None
        task.position = index

    db.session.delete(group)
    db.session.commit()

    emit_project_created(new_project, actor_user_id=user.id)
    emit_group_deleted(old_group_id, source_project_id, actor_user_id=user.id, task_ids=[])
    for task in moved_tasks:
        emit_task_updated(task, action="moved", old_project_id=source_project_id, old_group_id=old_group_id, actor_user_id=user.id)
    for pref_user_id in pref_user_ids:
        emit_sidebar_reordered(pref_user_id, _sidebar_order_tokens(User.query.get(pref_user_id)), actor_user_id=user.id)

    new_pref = ProjectSidebarPreference.query.filter_by(user_id=user.id, project_id=new_project.id).first()
    return {
        "project": {
            "id": new_project.id,
            "name": new_project.name,
            "division_id": new_pref.division_id if new_pref else None,
        },
        "moved_task_ids": [task.id for task in moved_tasks],
        "old_group_id": old_group_id,
    }, 201


@api_bp.get("/users")
@login_required
def list_users():
    user = current_user()
    q = (request.args.get("q") or "").strip().lower()
    limit = 100
    if q:
        users = search_users_by_identity(q, exclude_user_id=user.id, limit=limit)
    else:
        users = (
            User.query.filter(User.id != user.id)
            .order_by(User.display_name.asc(), User.email.asc())
            .limit(limit)
            .all()
        )
    results = []
    for u in users:
        results.append(
            {
                "id": u.id,
                "email": u.email,
                "display_name": display_name_for_user(u),
                "avatar_url": u.avatar_url,
            }
        )
    return {"results": results}


@api_bp.get("/tasks/search")
@login_required
def search_tasks():
    user = current_user()
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return {"results": []}
    accessible_projects = _accessible_projects_for_user(user)
    accessible_project_ids = [project.id for project in accessible_projects]
    if not accessible_project_ids:
        return {"results": []}
    project_map = {project.id: project for project in accessible_projects}
    sidebar_divisions = Division.query.filter_by(owner_id=user.id).order_by(Division.position.asc(), Division.id.asc()).all()
    pref_map = sidebar_preference_map(user.id, [project for project in accessible_projects if not project.is_direct], sidebar_divisions)
    division_map = {division.id: division for division in sidebar_divisions}
    group_map = {
        group.id: group
        for group in Group.query.filter(Group.project_id.in_(accessible_project_ids)).all()
    }
    needle = f"%{q}%"
    tasks = (
        Task.query
        .join(Project, Project.id == Task.project_id)
        .outerjoin(Group, Group.id == Task.group_id)
        .filter(Task.project_id.in_(accessible_project_ids))
        .filter(
            or_(
                Task.title.ilike(needle),
                Project.name.ilike(needle),
                Group.name.ilike(needle),
            )
        )
        .order_by(Task.created_at.desc(), Task.id.desc())
        .limit(12)
        .all()
    )
    groups = (
        Group.query
        .join(Project, Project.id == Group.project_id)
        .filter(Group.project_id.in_(accessible_project_ids))
        .filter(
            or_(
                Group.name.ilike(needle),
                Project.name.ilike(needle),
            )
        )
        .order_by(Group.id.desc())
        .limit(8)
        .all()
    )
    projects = (
        Project.query
        .filter(Project.id.in_(accessible_project_ids))
        .filter(Project.name.ilike(needle))
        .order_by(Project.id.desc())
        .limit(8)
        .all()
    )
    results = []
    for task in tasks:
        project = project_map.get(task.project_id)
        group = group_map.get(task.group_id) if task.group_id else None
        project_name = _project_display_name_for_user(project, user.id)
        pref = pref_map.get(task.project_id)
        division = division_map.get(pref.division_id) if pref and pref.division_id else None
        show_project_label = bool(project and not project.is_direct and division)
        results.append(
            {
                "entity_type": "task",
                "id": task.id,
                "title": task.title,
                "project_id": task.project_id,
                "project_name": project_name,
                "group_id": task.group_id,
                "group_name": group.name if group else None,
                "division_name": division.name if division else None,
                "show_project_label": show_project_label,
                "project_color": (division.color or "#4cc9f0") if show_project_label and division else None,
                "status": task.status,
            }
        )
    for group in groups:
        project = project_map.get(group.project_id)
        project_name = _project_display_name_for_user(project, user.id)
        pref = pref_map.get(group.project_id)
        division = division_map.get(pref.division_id) if pref and pref.division_id else None
        show_project_label = bool(project and not project.is_direct and division)
        results.append(
            {
                "entity_type": "group",
                "id": group.id,
                "title": group.name,
                "project_id": group.project_id,
                "project_name": project_name,
                "group_id": group.id,
                "group_name": group.name,
                "division_name": division.name if division else None,
                "show_project_label": show_project_label,
                "project_color": (division.color or "#4cc9f0") if show_project_label and division else None,
                "status": None,
            }
        )
    for project in projects:
        project_name = _project_display_name_for_user(project, user.id)
        pref = pref_map.get(project.id)
        division = division_map.get(pref.division_id) if pref and pref.division_id else None
        show_project_label = bool(project and not project.is_direct and division)
        results.append(
            {
                "entity_type": "project",
                "id": project.id,
                "title": project_name,
                "project_id": project.id,
                "project_name": project_name,
                "group_id": None,
                "group_name": None,
                "division_name": division.name if division else None,
                "show_project_label": show_project_label,
                "project_color": (division.color or "#4cc9f0") if show_project_label and division else None,
                "status": None,
            }
        )
    type_rank = {"project": 0, "group": 1, "task": 2}
    deduped = []
    seen: set[tuple[str, int]] = set()
    for item in sorted(results, key=lambda item: (type_rank.get(item["entity_type"], 9), str(item["title"] or "").lower(), -int(item["id"]))):
        key = (str(item["entity_type"]), int(item["id"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= 20:
            break
    return {"results": deduped}


@api_bp.post("/direct-projects")
@login_required
def create_direct_project():
    user = current_user()
    payload = request.get_json(silent=True) or {}
    target_user_id = payload.get("user_id")
    try:
        target_user_id = int(target_user_id)
    except (TypeError, ValueError):
        return {"error": "user_id is required"}, 400
    target_user = User.query.get(target_user_id)
    if not target_user:
        return {"error": "user not found"}, 404

    user_a_id, user_b_id = sorted([int(user.id), int(target_user.id)])
    project = Project.query.filter_by(
        is_direct=True,
        direct_user_a_id=user_a_id,
        direct_user_b_id=user_b_id,
    ).first()
    created = False
    if not project:
        label_a = user.display_name or user.email or f"User {user.id}"
        label_b = target_user.display_name or target_user.email or f"User {target_user.id}"
        project_name = label_a if int(user.id) == int(target_user.id) else f"{label_a} · {label_b}"
        project = Project(
            name=project_name,
            owner_id=user.id,
            is_direct=True,
            direct_user_a_id=user_a_id,
            direct_user_b_id=user_b_id,
            division_id=None,
            position=0,
        )
        db.session.add(project)
        db.session.flush()
        if not ProjectMember.query.filter_by(project_id=project.id, user_id=target_user.id).first():
            db.session.add(ProjectMember(project_id=project.id, user_id=target_user.id))
        db.session.flush()
        db.session.commit()
        emit_project_created(project, actor_user_id=user.id, recipient_user_id=user.id)
        emit_project_created(project, actor_user_id=user.id, recipient_user_id=target_user.id)
        created = True

    return {
        "project": _serialize_project_payload_for_user(project, user.id),
        "created": created,
    }, 201 if created else 200


@api_bp.get("/direct-projects")
@login_required
def list_direct_projects():
    user = current_user()
    member_project_ids = [row.project_id for row in ProjectMember.query.filter_by(user_id=user.id).all()]
    projects = (
        Project.query.filter(
            Project.is_direct.is_(True),
            or_(Project.owner_id == user.id, Project.id.in_(member_project_ids if member_project_ids else [-1])),
        )
        .all()
    )
    results = [_serialize_project_payload_for_user(project, user.id) for project in projects]
    results.sort(key=lambda item: ((item.get("display_name") or item.get("name") or "").lower(), item.get("id") or 0))
    return {"results": results}


@api_bp.post("/admin/resequence_tasks")
@login_required
def admin_resequence_tasks():
    user = current_user()
    if not user_is_admin(user):
        return {"error": "unauthorized"}, 403
    buckets = db.session.query(Task.project_id, Task.group_id).distinct().all()
    for project_id, group_id in buckets:
        _resequence_tasks(project_id, group_id)
    db.session.commit()
    return {"status": "ok", "buckets": len(buckets)}, 200


@api_bp.post("/projects/<int:project_id>/members")
@login_required
def add_project_member(project_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()
    user_id = payload.get("user_id")
    if not user_id:
        return {"error": "user_id is required"}, 400
    if not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    member_user = User.query.get(user_id)
    if not member_user:
        return {"error": "user not found"}, 404
    existing = ProjectMember.query.filter_by(project_id=project_id, user_id=user_id).first()
    if existing:
        return {"status": "ok"}, 200
    db.session.add(ProjectMember(project_id=project_id, user_id=user_id))
    group_ids = [group_id for (group_id,) in db.session.query(Group.id).filter(Group.project_id == project_id).all()]
    if group_ids:
        GroupMember.query.filter(GroupMember.group_id.in_(group_ids), GroupMember.user_id == user_id).delete(synchronize_session=False)
    db.session.commit()
    _sync_project_group_mode_tasks(project_id, actor_user_id=user.id)
    _sync_project_follow_mode_tasks(project_id, actor_user_id=user.id)
    project = Project.query.get(project_id)
    emit_project_created(project, actor_user_id=user.id, recipient_user_id=user_id)
    _queue_project_shared_notification(member_user, user, project)
    emit_project_members_updated(project_id, actor_user_id=user.id)
    for group_id in group_ids:
        emit_group_members_updated(group_id, actor_user_id=user.id)
    return {"status": "ok"}, 201


@api_bp.post("/groups/<int:group_id>/members")
@login_required
def add_group_member(group_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()
    user_id = payload.get("user_id")
    if not user_id:
        return {"error": "user_id is required"}, 400
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_manage_project(user, group.project_id):
        return {"error": "unauthorized"}, 403
    member_user = User.query.get(user_id)
    if not member_user:
        return {"error": "user not found"}, 404
    existing = ProjectMember.query.filter_by(project_id=group.project_id, user_id=user_id).first()
    if existing:
        return {"status": "ok"}, 200
    db.session.add(ProjectMember(project_id=group.project_id, user_id=user_id))
    group_ids = [item_id for (item_id,) in db.session.query(Group.id).filter(Group.project_id == group.project_id).all()]
    if group_ids:
        GroupMember.query.filter(GroupMember.group_id.in_(group_ids), GroupMember.user_id == user_id).delete(synchronize_session=False)
    db.session.commit()
    _sync_project_group_mode_tasks(group.project_id, actor_user_id=user.id)
    _sync_project_follow_mode_tasks(group.project_id, actor_user_id=user.id)
    project = Project.query.get(group.project_id)
    emit_project_created(project, actor_user_id=user.id, recipient_user_id=user_id)
    _queue_project_shared_notification(member_user, user, project)
    emit_project_members_updated(group.project_id, actor_user_id=user.id)
    for item_id in group_ids:
        emit_group_members_updated(item_id, actor_user_id=user.id)
    return {"status": "ok"}, 201


@api_bp.get("/projects/<int:project_id>/members")
@login_required
def list_project_members(project_id: int):
    user = current_user()
    if not _can_access_project(user, project_id):
        return {"error": "unauthorized"}, 403
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    members = list(project_access_map(project_id).values())
    for member in members:
        if member.get("owner"):
            member["access_label"] = "Owner"
        else:
            member["access_label"] = "Shared"
        member["can_promote"] = False
        member["can_demote"] = False
    members.sort(key=lambda item: (0 if item.get("owner") else 1, (item.get("display_name") or item.get("email") or "").lower()))
    return {"members": members}, 200


@api_bp.get("/groups/<int:group_id>/members")
@login_required
def list_group_members(group_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_access_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    members = list(project_access_map(group.project_id).values())
    members.sort(key=lambda item: (0 if item.get("owner") else 1, (item.get("display_name") or item.get("email") or "").lower()))
    return {"members": members}, 200


@api_bp.delete("/projects/<int:project_id>/members/<int:user_id>")
@login_required
def remove_project_member(project_id: int, user_id: int):
    user = current_user()
    if not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    member_user = User.query.get(user_id)
    project = Project.query.get(project_id)
    ProjectMember.query.filter_by(project_id=project_id, user_id=user_id).delete()
    group_ids = [group_id for (group_id,) in db.session.query(Group.id).filter(Group.project_id == project_id).all()]
    if group_ids:
        GroupMember.query.filter(GroupMember.group_id.in_(group_ids), GroupMember.user_id == user_id).delete(synchronize_session=False)
    db.session.commit()
    _sync_project_group_mode_tasks(project_id, actor_user_id=user.id)
    _sync_project_follow_mode_tasks(project_id, actor_user_id=user.id)
    emit_project_deleted(project_id, actor_user_id=user.id, recipient_user_ids=[user_id], include_project_room=False)
    if member_user and project:
        _queue_project_removed_notification(member_user, user, project)
    emit_project_members_updated(project_id, actor_user_id=user.id)
    for group_id in group_ids:
        emit_group_members_updated(group_id, actor_user_id=user.id)
    return {"status": "deleted"}, 200


@api_bp.post("/projects/<int:project_id>/members/<int:user_id>/scope")
@login_required
def update_project_member_scope(project_id: int, user_id: int):
    user = current_user()
    if not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if user_id == project.owner_id:
        return {"error": "cannot change owner scope"}, 400

    payload = request.get_json(silent=True) or {}
    scope = (payload.get("scope") or "").strip().lower()
    if scope not in {"full", "partial"}:
        return {"error": "invalid scope"}, 400

    existing = ProjectMember.query.filter_by(project_id=project_id, user_id=user_id).first()
    member_user = User.query.get(user_id)
    if scope == "full":
        if not existing:
            db.session.add(ProjectMember(project_id=project_id, user_id=user_id))
    else:
        if existing:
            db.session.delete(existing)
        group_ids = [group_id for (group_id,) in db.session.query(Group.id).filter(Group.project_id == project_id).all()]
        if group_ids:
            GroupMember.query.filter(GroupMember.group_id.in_(group_ids), GroupMember.user_id == user_id).delete(synchronize_session=False)
    db.session.commit()
    _sync_project_group_mode_tasks(project_id, actor_user_id=user.id)
    _sync_project_follow_mode_tasks(project_id, actor_user_id=user.id)
    if scope == "partial" and member_user:
        _queue_project_removed_notification(member_user, user, project)
    emit_project_members_updated(project_id, actor_user_id=user.id)
    for group_id in [group_id for (group_id,) in db.session.query(Group.id).filter(Group.project_id == project_id).all()]:
        emit_group_members_updated(group_id, actor_user_id=user.id)
    return {"status": "ok"}, 200


@api_bp.delete("/groups/<int:group_id>/members/<int:user_id>")
@login_required
def remove_group_member(group_id: int, user_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_manage_project(user, group.project_id):
        return {"error": "unauthorized"}, 403
    member_user = User.query.get(user_id)
    ProjectMember.query.filter_by(project_id=group.project_id, user_id=user_id).delete()
    group_ids = [item_id for (item_id,) in db.session.query(Group.id).filter(Group.project_id == group.project_id).all()]
    if group_ids:
        GroupMember.query.filter(GroupMember.group_id.in_(group_ids), GroupMember.user_id == user_id).delete(synchronize_session=False)
    db.session.commit()
    _sync_project_group_mode_tasks(group.project_id, actor_user_id=user.id)
    _sync_project_follow_mode_tasks(group.project_id, actor_user_id=user.id)
    emit_project_deleted(group.project_id, actor_user_id=user.id, recipient_user_ids=[user_id], include_project_room=False)
    if member_user:
        _queue_project_removed_notification(member_user, user, Project.query.get(group.project_id))
    emit_project_members_updated(group.project_id, actor_user_id=user.id)
    for item_id in group_ids:
        emit_group_members_updated(item_id, actor_user_id=user.id)
    return {"status": "deleted"}, 200


@api_bp.post("/groups/reorder")
@login_required
def reorder_groups():
    payload = request.get_json(silent=True) or {}
    user = current_user()
    project_id = payload.get("project_id")
    order = payload.get("order") or []
    if not project_id or not isinstance(order, list):
        return {"error": "project_id and order are required"}, 400

    if not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404

    group_ids = [g.id for g in Group.query.filter_by(project_id=project.id).all()]
    order_ids = [int(gid) for gid in order if str(gid).isdigit()]
    if set(order_ids) != set(group_ids):
        return {"error": "order does not match groups"}, 400

    for idx, gid in enumerate(order_ids):
        Group.query.filter_by(id=gid, project_id=project.id).update({"position": idx + 1})
    db.session.commit()
    emit_group_reordered(project.id, order_ids, actor_user_id=user.id)
    return {"status": "ok"}, 200


@api_bp.post("/groups/<int:group_id>/move")
@login_required
def move_group(group_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()
    target_project_id = payload.get("project_id")
    before_group_id = payload.get("before_group_id")

    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not target_project_id:
        return {"error": "project_id is required"}, 400

    try:
        target_project_id = int(target_project_id)
    except (TypeError, ValueError):
        return {"error": "invalid project_id"}, 400

    target_project = Project.query.get(target_project_id)
    if not target_project:
        return {"error": "project not found"}, 404

    source_project_id = group.project_id
    if not _can_access_project(user, source_project_id):
        return {"error": "unauthorized"}, 403
    if not _can_access_project(user, target_project_id):
        return {"error": "unauthorized"}, 403

    before_group = None
    if before_group_id:
        try:
            before_group_id = int(before_group_id)
        except (TypeError, ValueError):
            return {"error": "invalid before_group_id"}, 400
        before_group = Group.query.get(before_group_id)
        if not before_group:
            return {"error": "before_group not found"}, 404
        if before_group.id == group.id:
            before_group = None
        elif before_group.project_id != target_project_id:
            return {"error": "before_group project mismatch"}, 400

    if source_project_id == target_project_id:
        siblings = [row for row in Group.query.filter_by(project_id=target_project_id).order_by(Group.position.asc(), Group.id.asc()).all() if row.id != group.id]
    else:
        siblings = Group.query.filter_by(project_id=target_project_id).order_by(Group.position.asc(), Group.id.asc()).all()

    insert_at = len(siblings)
    if before_group:
        for idx, sibling in enumerate(siblings):
            if sibling.id == before_group.id:
                insert_at = idx
                break

    old_task_rows = Task.query.filter_by(group_id=group.id).all()
    old_task_ids = [task.id for task in old_task_rows]

    if source_project_id != target_project_id:
        source_groups = [row for row in Group.query.filter_by(project_id=source_project_id).order_by(Group.position.asc(), Group.id.asc()).all() if row.id != group.id]
        for idx, sibling in enumerate(source_groups, start=1):
            sibling.position = idx
        group.project_id = target_project_id
        for task in old_task_rows:
            task.project_id = target_project_id

    siblings.insert(insert_at, group)
    for idx, sibling in enumerate(siblings, start=1):
        sibling.position = idx

    db.session.commit()

    if source_project_id != target_project_id:
        emit_group_deleted(group.id, source_project_id, actor_user_id=user.id, task_ids=old_task_ids)
        emit_group_created(group, actor_user_id=user.id)
        for task in old_task_rows:
            emit_task_updated(task, action="moved", old_project_id=source_project_id, old_group_id=group.id, actor_user_id=user.id)
    else:
        emit_group_reordered(target_project_id, [row.id for row in siblings], actor_user_id=user.id)
        emit_group_updated(group, actor_user_id=user.id)

    return {"status": "ok", "group": {"id": group.id, "project_id": group.project_id}}, 200


@api_bp.post("/sidebar/reorder")
@login_required
def reorder_sidebar():
    user = current_user()
    payload = request.get_json(silent=True) or {}
    order = payload.get("order") or []
    if not isinstance(order, list) or not order:
        return {"error": "order is required"}, 400

    accessible_projects = _accessible_projects_for_user(user)
    divisions = Division.query.filter_by(owner_id=user.id).all()
    pref_map = sidebar_preference_map(user.id, accessible_projects, divisions)
    expected = [f"project:{project.id}" for project in accessible_projects if pref_map.get(project.id) and pref_map[project.id].division_id is None]
    expected.extend([f"division:{division.id}" for division in divisions])
    if sorted(order) != sorted(expected):
        return {"error": "order mismatch"}, 400

    for index, token in enumerate(order, start=1):
        kind, raw_id = token.split(":", 1)
        item_id = int(raw_id)
        if kind == "project":
            project = Project.query.get(item_id)
            if not project or not _can_access_project(user, item_id):
                return {"error": "project not found"}, 404
            pref = ensure_sidebar_preference(user.id, project.id, default_division_id=project.division_id, default_position=project.position or 0)
            pref.division_id = None
            pref.position = index
        elif kind == "division":
            division = Division.query.filter_by(id=item_id, owner_id=user.id).first()
            if not division:
                return {"error": "division not found"}, 404
            division.position = index
        else:
            return {"error": "invalid token"}, 400

    db.session.commit()
    emit_sidebar_reordered(user.id, order, actor_user_id=user.id)
    return {"status": "ok"}, 200


@api_bp.delete("/tasks/<int:task_id>")
@login_required
def delete_task(task_id: int):
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404

    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    invite_query = Invite.query.filter(Invite.task_id == task.id)
    invite_count = invite_query.filter(Invite.status != "draft").count()

    requires_confirm = invite_count > 0
    if request.args.get("confirm") != "1":
        if requires_confirm:
            return {
                "requires_confirm": True,
                "invite_count": invite_count,
            }, 200
        # No confirm needed; proceed with delete

    Assignment.query.filter_by(task_id=task.id).delete()
    Invite.query.filter_by(task_id=task.id).delete()
    db.session.delete(task)
    db.session.commit()
    emit_task_updated(task, action="deleted", old_project_id=task.project_id, old_group_id=task.group_id, actor_user_id=user.id)
    return {"status": "deleted"}, 200


@api_bp.post("/assignments")
@login_required
def create_assignment():
    payload = request.get_json(silent=True) or {}
    user = current_user()
    target_type = payload.get("target_type")
    target_id = payload.get("target_id")
    email = (payload.get("email") or "").strip().lower()

    if target_type != "task" or not target_id or not email:
        return {"error": "target_type, target_id, and email are required"}, 400
    if "@" not in email:
        return {"error": "invalid email"}, 400

    task = Task.query.get(target_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    account_user = find_user_by_email(email)
    project = Project.query.get(task.project_id) if task else None
    project_share_required = _assignment_requires_project_share_payload(project, account_user)
    if project_share_required:
        return project_share_required, 200
    if not account_user:
        get_or_create_collaborator_profile(
            email=email,
            default_calendar_opt_in=project.default_invitee_calendar_opt_in if project else False,
        )
    existing_assignment = _existing_assignment_for_target(
        task_id=task.id,
        account_user_id=account_user.id if account_user else None,
        email=email,
    )
    if existing_assignment:
        existing_assignment = _collapse_duplicate_task_assignments(
            task_id=task.id,
            account_user_id=account_user.id if account_user else None,
            email=email,
        ) or existing_assignment
        _reset_existing_assignment(existing_assignment)
        db.session.commit()
        _queue_assignment_added_notifications(
            task,
            actor_user=user,
            fallback_assignee_label=display_name_for_user(account_user) if account_user else (existing_assignment.email or email),
        )
        emit_assignment_updated(task, existing_assignment, actor_user_id=user.id)
        return {
            "id": existing_assignment.id,
            "user_id": existing_assignment.user_id,
            "email": existing_assignment.email,
            "status": existing_assignment.status,
            "display_name": account_user.display_name if account_user else None,
            "display_email": account_user.email if account_user else existing_assignment.email,
            "avatar_url": account_user.avatar_url if account_user else None,
        }, 200
    assignment = Assignment(
        task_id=task.id,
        user_id=account_user.id if account_user else None,
        email=email if not account_user else None,
        status="assigned" if account_user else "draft",
    )
    db.session.add(assignment)
    db.session.commit()
    assignment = _collapse_duplicate_task_assignments(
        task_id=task.id,
        account_user_id=account_user.id if account_user else None,
        email=email,
    ) or assignment
    db.session.commit()
    _queue_assignment_added_notifications(
        task,
        actor_user=user,
        fallback_assignee_label=display_name_for_user(account_user) if account_user else (assignment.email or email),
    )
    emit_assignment_updated(task, assignment, action="created", actor_user_id=user.id)

    return {
        "id": assignment.id,
        "user_id": assignment.user_id,
        "email": assignment.email,
        "status": assignment.status,
        "display_name": account_user.display_name if account_user else None,
        "display_email": account_user.email if account_user else assignment.email,
        "avatar_url": account_user.avatar_url if account_user else None,
    }, 201


@api_bp.post("/tasks/<int:task_id>/assign_all")
@login_required
def assign_all_task_members(task_id: int):
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403
    task.assign_group_members = True
    changes = sync_group_task_assignments(task)
    db.session.commit()
    for assignment in changes["deleted"]:
        emit_assignment_updated(task, assignment, action="deleted", actor_user_id=user.id)
    for assignment in changes["created"]:
        emit_assignment_updated(task, assignment, action="created", actor_user_id=user.id)
    _queue_group_assignment_notifications(task, changes["created"], user)
    emit_task_updated(task, actor_user_id=user.id)
    return {
        "assign_group_members": True,
        "group_assignment_members": serialize_group_assignment_members(task),
        "assignments": [_serialize_assignment_row(row) for row in Assignment.query.filter_by(task_id=task.id).all()],
    }, 200


@api_bp.post("/tasks/<int:task_id>/followers")
@login_required
def create_task_follower(task_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    follower_user = None
    follower_user_id = payload.get("user_id")
    email = (payload.get("email") or "").strip().lower()
    if follower_user_id not in (None, "", 0, "0"):
        try:
            follower_user_id = int(follower_user_id)
        except (TypeError, ValueError):
            return {"error": "invalid user_id"}, 400
        follower_user = User.query.get(follower_user_id)
    elif email:
        follower_user = find_user_by_email(email)
    if not follower_user:
        return {"error": "account user not found"}, 400
    if not _account_user_has_project_access(task.project_id, follower_user.id):
        return {"error": "user must have project access to follow this task"}, 400

    existing = TaskFollower.query.filter_by(task_id=task.id, user_id=follower_user.id).first()
    if existing:
        return _serialize_follower_row(existing), 200

    follower = TaskFollower(task_id=task.id, user_id=follower_user.id)
    db.session.add(follower)
    db.session.commit()
    emit_task_updated(task, actor_user_id=user.id)
    emit_task_notification_updates(task, exclude_user_id=user.id)
    return _serialize_follower_row(follower), 201


@api_bp.post("/tasks/<int:task_id>/follow_all")
@login_required
def create_task_followers_for_project(task_id: int):
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    _set_task_follow_project_members(task, True)
    desired_user_ids = set(_project_access_user_ids(task.project_id))
    existing_rows = TaskFollower.query.filter_by(task_id=task.id).all()
    existing_by_user_id = {int(row.user_id): row for row in existing_rows if row.user_id}
    changed = False
    for row in existing_rows:
        if row.user_id and int(row.user_id) not in desired_user_ids:
            db.session.delete(row)
            changed = True
    for follower_user_id in desired_user_ids:
        if follower_user_id in existing_by_user_id:
            continue
        db.session.add(TaskFollower(task_id=task.id, user_id=follower_user_id))
        changed = True

    db.session.commit()
    if changed or _task_follow_project_members(task):
        emit_task_updated(task, actor_user_id=user.id)
        emit_task_notification_updates(task, exclude_user_id=user.id)

    followers = TaskFollower.query.filter_by(task_id=task.id).all()
    return {
        "follow_project_members": True,
        "project_follower_members": _serialize_project_follower_members(task),
        "followers": [_serialize_follower_row(row) for row in followers],
    }, 200


@api_bp.delete("/task-followers/<int:follower_id>")
@login_required
def delete_task_follower(follower_id: int):
    user = current_user()
    follower = TaskFollower.query.get(follower_id)
    if not follower:
        return {"error": "task follower not found"}, 404

    task = Task.query.get(follower.task_id) if follower.task_id else None
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    db.session.delete(follower)
    db.session.commit()
    emit_task_updated(task, actor_user_id=user.id)
    emit_task_notification_updates(task, exclude_user_id=user.id)
    return {"status": "deleted"}, 200


def _sync_group_mode_tasks(group_id: int, *, actor_user_id: int | None = None) -> None:
    tasks = Task.query.filter_by(group_id=group_id, assign_group_members=True).all()
    if not tasks:
        return
    emitted = []
    for task in tasks:
        changes = sync_group_task_assignments(task)
        emitted.append((task, changes))
    db.session.commit()
    for task, changes in emitted:
        for assignment in changes["deleted"]:
            emit_assignment_updated(task, assignment, action="deleted", actor_user_id=actor_user_id)
        for assignment in changes["created"]:
            emit_assignment_updated(task, assignment, action="created", actor_user_id=actor_user_id)
        actor_user = User.query.get(actor_user_id) if actor_user_id else None
        _queue_group_assignment_notifications(task, changes["created"], actor_user)
        emit_task_updated(task, actor_user_id=actor_user_id)


def _sync_project_group_mode_tasks(project_id: int, *, actor_user_id: int | None = None) -> None:
    for group in Group.query.filter_by(project_id=project_id).all():
        _sync_group_mode_tasks(group.id, actor_user_id=actor_user_id)


def _sync_project_follow_mode_tasks(project_id: int, *, actor_user_id: int | None = None) -> None:
    desired_user_ids = set(_project_access_user_ids(project_id))
    tasks = [task for task in Task.query.filter_by(project_id=project_id).all() if _task_follow_project_members(task)]
    if not tasks:
        return
    for task in tasks:
        existing_rows = TaskFollower.query.filter_by(task_id=task.id).all()
        existing_by_user_id = {int(row.user_id): row for row in existing_rows if row.user_id}
        changed = False
        for row in existing_rows:
            if row.user_id and int(row.user_id) not in desired_user_ids:
                db.session.delete(row)
                changed = True
        for follower_user_id in desired_user_ids:
            if follower_user_id in existing_by_user_id:
                continue
            db.session.add(TaskFollower(task_id=task.id, user_id=follower_user_id))
            changed = True
        if changed:
            db.session.flush()
    db.session.commit()
    for task in tasks:
        emit_task_updated(task, actor_user_id=actor_user_id)


@api_bp.post("/assignments/<int:assignment_id>/send_link")
@login_required
def send_assignment_link(assignment_id: int):
    user = current_user()
    assignment = Assignment.query.get(assignment_id)
    if not assignment:
        return {"error": "assignment not found"}, 404

    task = Task.query.get(assignment.task_id) if assignment.task_id else None
    if not task:
        return {"error": "task not found"}, 404

    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    email = assignment.email or (User.query.get(assignment.user_id).email if assignment.user_id else None)
    if not email:
        return {"error": "assignment has no email"}, 400

    project = Project.query.get(task.project_id) if task.project_id else None
    collaborator = get_or_create_collaborator_profile(
        email=email,
        default_calendar_opt_in=project.default_invitee_calendar_opt_in if project else False,
    )

    invite = Invite.query.filter_by(assignment_id=assignment.id).first()
    if not invite:
        token = secrets.token_hex(24)
        invite = Invite(
            task_id=assignment.task_id,
            assignment_id=assignment.id,
            email=email,
            token=token,
            status="sent",
            calendar_opt_in=collaborator.default_calendar_opt_in,
        )
        db.session.add(invite)
    base_url = current_app.config["PUBLIC_BASE_URL"].rstrip("/")
    manage_url = f"{base_url}/collaborators/{collaborator.access_token}"
    accept_url = f"{base_url}/invites/{invite.token}/quick/accept"
    decline_url = f"{base_url}/invites/{invite.token}/quick/decline"

    try:
        send_magic_link_email(
            to_email=email,
            invite_url=None,
            manage_url=manage_url,
            project_name=project.name if project else None,
            task_title=task.title if task else None,
            inviter_email=user.email,
            inviter_name=user.display_name or user.email,
        )
    except MailDeliveryError as exc:
        db.session.rollback()
        return {"error": f"email delivery failed: {exc}"}, 503

    invite.status = "sent"
    assignment.status = "link_sent"
    db.session.commit()
    emit_assignment_updated(task, assignment, actor_user_id=user.id)

    return {"status": "link_sent", "invite_url": manage_url}, 200


@api_bp.post("/assignments/send_links")
@login_required
def send_assignment_links_bulk():
    payload = request.get_json(silent=True) or {}
    assignment_ids = payload.get("assignment_ids") or []
    if not assignment_ids or not isinstance(assignment_ids, list):
        return {"error": "assignment_ids is required"}, 400

    user = current_user()
    assignments = Assignment.query.filter(Assignment.id.in_(assignment_ids)).all()
    if len(assignments) != len(set(int(item) for item in assignment_ids)):
        return {"error": "one or more assignments were not found"}, 404

    normalized_ids = [int(item) for item in assignment_ids]
    assignments.sort(key=lambda assignment: normalized_ids.index(assignment.id))

    first_email = None
    prepared_items = []
    prepared_assignments = []
    collaborator = None

    for assignment in assignments:
        task = Task.query.get(assignment.task_id) if assignment.task_id else None
        if not task:
            return {"error": f"task not found for assignment {assignment.id}"}, 404
        if not _can_access_task(user, task):
            return {"error": "unauthorized"}, 403

        email = assignment.email or (User.query.get(assignment.user_id).email if assignment.user_id else None)
        if not email:
            return {"error": f"assignment {assignment.id} has no email"}, 400
        if first_email is None:
            first_email = email
        elif email != first_email:
            return {"error": "all assignments in a bulk send must belong to the same email address"}, 400

        project = Project.query.get(task.project_id) if task.project_id else None
        collaborator = get_or_create_collaborator_profile(
            email=email,
            default_calendar_opt_in=project.default_invitee_calendar_opt_in if project else False,
        )
        invite = Invite.query.filter_by(assignment_id=assignment.id).first()
        if not invite:
            invite = Invite(
                task_id=assignment.task_id,
                assignment_id=assignment.id,
                email=email,
                token=secrets.token_hex(24),
                status="sent",
                calendar_opt_in=collaborator.default_calendar_opt_in,
            )
            db.session.add(invite)
            db.session.flush()

        base_url = current_app.config["PUBLIC_BASE_URL"].rstrip("/")
        manage_url = f"{base_url}/collaborators/{collaborator.access_token}"
        prepared_items.append(
            {
                "project_name": project.name if project else None,
                "task_title": task.title if task else None,
                "invite_url": manage_url,
                "portal_url": manage_url,
                "accept_url": f"{base_url}/invites/{invite.token}/quick/accept",
                "decline_url": f"{base_url}/invites/{invite.token}/quick/decline",
                "manage_url": manage_url,
            }
        )
        prepared_assignments.append((assignment, invite))

    try:
        send_magic_link_digest_email(
            to_email=first_email,
            manage_url=prepared_items[0].get("manage_url") if prepared_items else None,
            items=prepared_items,
            inviter_email=user.email,
            inviter_name=user.display_name or user.email,
        )
    except MailDeliveryError as exc:
        db.session.rollback()
        return {"error": f"email delivery failed: {exc}"}, 503

    for assignment, invite in prepared_assignments:
        invite.status = "sent"
        assignment.status = "link_sent"
    db.session.commit()
    for assignment, _invite in prepared_assignments:
        task = Task.query.get(assignment.task_id) if assignment.task_id else None
        if task:
            emit_assignment_updated(task, assignment, actor_user_id=user.id)
    return {"status": "link_sent", "assignment_ids": [assignment.id for assignment, _ in prepared_assignments]}, 200


@api_bp.delete("/assignments/<int:assignment_id>")
@login_required
def delete_assignment(assignment_id: int):
    user = current_user()
    assignment = Assignment.query.get(assignment_id)
    if not assignment:
        return {"error": "assignment not found"}, 404

    task = Task.query.get(assignment.task_id) if assignment.task_id else None
    if not task:
        return {"error": "task not found"}, 404

    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    Invite.query.filter_by(assignment_id=assignment.id).delete()
    db.session.delete(assignment)
    db.session.commit()
    emit_assignment_updated(task, assignment, action="deleted", actor_user_id=user.id)
    return {"status": "deleted"}, 200


@api_bp.post("/tasks/<int:task_id>/info/attachments")
@login_required
def upload_task_attachment(task_id: int):
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return {"error": "file is required"}, 400
    attachment = save_uploaded_file(upload, _upload_root(), "task", task.id)
    info = _info_payload_for(task)
    info.setdefault("attachments", []).append(attachment)
    task.info = normalize_info_payload(info, task.link)
    db.session.commit()
    return {"attachment": attachment, "info": info}, 201


@api_bp.delete("/tasks/<int:task_id>/info/attachments/<attachment_id>")
@login_required
def delete_task_attachment(task_id: int, attachment_id: str):
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403
    info = _info_payload_for(task)
    remaining = []
    deleted = None
    for item in info.get("attachments", []):
        if item.get("id") == attachment_id and deleted is None:
            deleted = item
        else:
            remaining.append(item)
    if not deleted:
        return {"error": "attachment not found"}, 404
    if deleted.get("path"):
        file_path = Path(current_app.instance_path) / deleted["path"]
        if file_path.exists():
            file_path.unlink()
    info["attachments"] = remaining
    task.info = normalize_info_payload(info, task.link)
    db.session.commit()
    return {"status": "deleted", "info": info}, 200


@api_bp.get("/uploads/<item_type>/<int:item_id>/<filename>")
@login_required
def serve_upload(item_type: str, item_id: int, filename: str):
    item = None
    user = current_user()
    if item_type == "task":
        item = Task.query.get(item_id)
        if not item or not _can_access_task(user, item):
            return {"error": "not found"}, 404
    else:
        return {"error": "not found"}, 404
    upload_dir = _upload_root() / item_type / str(item_id)
    return send_from_directory(upload_dir, filename, as_attachment=False)


@api_bp.get("/assignees")
@login_required
def list_assignees():
    user = current_user()
    q = (request.args.get("q") or "").strip().lower()
    limit = 100

    # Limit scope to projects the user owns or tasks they are assigned to
    owned_project_ids = [p.id for p in Project.query.filter_by(owner_id=user.id).all()]
    member_project_ids = [
        m.project_id for m in ProjectMember.query.filter_by(user_id=user.id).all()
    ]
    base_project_ids = list(set(owned_project_ids + member_project_ids))
    owned_task_ids = (
        [t.id for t in Task.query.filter(Task.project_id.in_(base_project_ids)).all()]
        if base_project_ids
        else []
    )
    assigned_task_ids = [
        a.task_id for a in Assignment.query.filter_by(user_id=user.id).filter(Assignment.task_id.isnot(None)).all()
    ]
    task_ids = list(set(owned_task_ids + assigned_task_ids))

    # Users by email/display_name
    if q:
        users = search_users_by_identity(q, limit=limit)
    else:
        users = (
            User.query.filter(User.id != user.id)
            .order_by(User.display_name.asc(), User.email.asc())
            .limit(limit)
            .all()
        )

    # Prior emails from assignments/invites within scope
    emails = set()
    if task_ids:
        for row in Assignment.query.filter(Assignment.task_id.in_(task_ids)).all():
            if row.email:
                emails.add(row.email)
    if task_ids:
        invite_rows = Invite.query.filter(Invite.task_id.in_(task_ids)).all()
        for row in invite_rows:
            if row.email:
                emails.add(row.email)

    filtered_emails = [e for e in emails if not q or q in e.lower()]
    filtered_emails.sort()

    # Count accepts per email
    accept_counts = {}
    if task_ids:
        invite_rows = Invite.query.filter(
            Invite.task_id.in_(task_ids),
            Invite.status == "accepted",
        ).all()
        for row in invite_rows:
            accept_counts[row.email] = accept_counts.get(row.email, 0) + 1

    results = []
    for u in users:
        results.append(
            {
                "type": "user",
                "id": u.id,
                "email": u.email,
                "display_name": u.display_name,
                "avatar_url": u.avatar_url,
                "accepted_count": accept_counts.get(u.email, 0),
            }
        )
    for e in filtered_emails[:limit]:
        results.append(
            {
                "type": "email",
                "email": e,
                "accepted_count": accept_counts.get(e, 0),
            }
        )

    return {"results": results}
