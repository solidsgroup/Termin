from datetime import date, datetime, timedelta
from functools import wraps
import hashlib
import json
from pathlib import Path
import secrets
import subprocess

from flask import Blueprint, Response, current_app, redirect, render_template, request, url_for
from sqlalchemy import func, or_
from werkzeug.security import generate_password_hash

from app.auth import login_required
from app.calendar_sync import CalendarSyncError, ensure_task_event
from app.collaborators import get_or_create_collaborator_profile
from app.emailer import MailDeliveryError, send_account_verification_email, send_calendar_invite_email
from app.emailer import _escape_ics_text, _format_ics_timestamp
from app.extensions import db
from app.discussion_activity import build_discussion_activity_items, task_discussion_user_ids, upsert_discussion_activity
from app.github_sync import GitHubSyncError, github_identity_for_user, github_sync_state_for_user, should_sync_github_issues, sync_github_issues_for_user
from app.identity import (
    add_user_email,
    claim_collaborator_profile,
    consume_email_verification,
    create_email_verification,
    find_user_by_email,
    list_external_identities,
    list_user_emails,
    merge_users,
    remove_user_email,
    set_primary_user_email,
)
from app.info_utils import load_info_payload, normalize_info_payload
from app.notification_preferences import (
    GENERAL_NOTIFICATION_DEFINITIONS,
    NOTIFICATION_EVENT_DEFINITIONS,
    TASK_NOTIFICATION_DEFINITIONS,
    TASK_NOTIFICATION_SCOPES,
    user_notification_channel_enabled,
    user_notification_preference_map,
)
from app.notification_emailer import (
    EMAIL_FREQUENCY_OPTIONS,
    build_test_notification_email_items,
    normalize_notification_email_frequency,
    send_notification_digest_email,
)
from app.models import CalendarFeed, CollaboratorProfile, CollaboratorTaskRead, DevMailboxMessage, Division, EmailVerification, ExternalIdentity, GitHubIssueLink, GitHubSyncState, Project, ProjectSidebarPreference, Task, Invite, Assignment, User, UserEmail, Group, ProjectMember, GroupMember, TaskComment, TaskNotification, UserDiscussionActivity
from app.models import UserNotificationPreference
from app.group_assignments import serialize_group_assignment_members
from app.realtime import (
    emit_assignment_updated,
    emit_discussion_activity_updated,
    emit_division_created,
    emit_group_created,
    emit_project_created,
    emit_sidebar_reordered,
    emit_task_comment_created,
    emit_task_notification_updates,
    emit_task_updated,
    emit_user_notification_preview,
    is_user_viewing_discussion,
    queue_task_notifications,
    is_user_viewing_task,
    _task_notification_user_ids,
)
from app.sidebar_layout import (
    ensure_sidebar_preference,
    insert_project_position,
    insert_top_level,
    sidebar_preference_map,
    top_level_sidebar_items,
)
from app.task_status import effective_task_status_for_user, set_task_collaborator_status, task_status_meta_map
from app.utils import ALL_TIMEZONE_OPTIONS, COMMON_TIMEZONE_OPTIONS, current_user, display_name_for_user, normalize_user_timezone
from app.utils import is_admin as user_is_admin


def _direct_peer_for_user(project: Project, user_id: int) -> User | None:
    if not project or not getattr(project, "is_direct", False):
        return None
    peer_id = None
    if project.direct_user_a_id and int(project.direct_user_a_id) != int(user_id):
        peer_id = project.direct_user_a_id
    elif project.direct_user_b_id and int(project.direct_user_b_id) != int(user_id):
        peer_id = project.direct_user_b_id
    return User.query.get(peer_id) if peer_id else None


def _project_display_name_for_user(project: Project, user_id: int) -> str:
    peer = _direct_peer_for_user(project, user_id)
    if peer:
        return display_name_for_user(peer) or project.name
    return project.name


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


def _normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def _ensure_calendar_feed(email: str | None) -> CalendarFeed | None:
    normalized = _normalize_email(email)
    if not normalized:
        return None
    feed = CalendarFeed.query.filter_by(email=normalized).first()
    if feed:
        return feed
    feed = CalendarFeed(
        email=normalized,
        token=secrets.token_urlsafe(32),
        updated_at=datetime.utcnow(),
    )
    db.session.add(feed)
    db.session.commit()
    return feed


def calendar_subscription_urls_for_email(email: str | None) -> dict[str, str]:
    normalized = _normalize_email(email)
    if not normalized:
        return {"https": "", "webcal": ""}
    feed = _ensure_calendar_feed(normalized)
    if not feed:
        return {"https": "", "webcal": ""}
    https_url = url_for("ui.calendar_feed", token=feed.token, _external=True)
    webcal_url = "webcal://" + https_url[len("https://"):] if https_url.startswith("https://") else https_url
    return {"https": https_url, "webcal": webcal_url}


def calendar_subscription_urls_for_user(user) -> dict[str, str]:
    if not user:
        return {"https": "", "webcal": ""}
    return calendar_subscription_urls_for_email(user.email)


def _calendar_feed_user_ids(email: str) -> list[int]:
    normalized = _normalize_email(email)
    if not normalized:
        return []
    user_ids = {
        row.id
        for row in User.query.filter(func.lower(User.email) == normalized).all()
    }
    user_ids.update(
        row.user_id
        for row in UserEmail.query.filter(func.lower(UserEmail.email) == normalized).all()
        if row.user_id
    )
    collaborator = CollaboratorProfile.query.filter(func.lower(CollaboratorProfile.email) == normalized).first()
    if collaborator and collaborator.user_id:
        user_ids.add(collaborator.user_id)
    return sorted(user_ids)


def _calendar_feed_tasks(email: str) -> list[Task]:
    normalized = _normalize_email(email)
    if not normalized:
        return []
    user_ids = _calendar_feed_user_ids(normalized)
    assignment_query = Assignment.query.filter(Assignment.status != "denied")
    assignment_filter = func.lower(Assignment.email) == normalized
    if user_ids:
        assignment_filter = or_(assignment_filter, Assignment.user_id.in_(user_ids))
    assignments = assignment_query.filter(assignment_filter).all()
    task_ids: list[int] = []
    seen_task_ids: set[int] = set()
    for assignment in assignments:
        if not assignment.task_id or assignment.task_id in seen_task_ids:
            continue
        seen_task_ids.add(assignment.task_id)
        task_ids.append(assignment.task_id)
    if not task_ids:
        return []
    tasks = (
        Task.query.filter(Task.id.in_(task_ids), Task.due_at.isnot(None))
        .order_by(Task.due_at.asc(), Task.id.asc())
        .all()
    )
    if not tasks:
        return []
    status_map = task_status_meta_map(tasks, viewer_email=normalized)
    return [
        task
        for task in tasks
        if not _is_complete_status(
            effective_task_status_for_user(
                task,
                viewer_email=normalized,
                status_meta=status_map.get(task.id),
            )
        )
    ]


def _calendar_portal_url(email: str, public_base_url: str, task_id: int | None = None) -> str:
    if not public_base_url:
        return ""
    collaborator = CollaboratorProfile.query.filter(func.lower(CollaboratorProfile.email) == _normalize_email(email)).first()
    if not collaborator or not collaborator.access_token:
        return ""
    url = f"{public_base_url}/collaborators/{collaborator.access_token}"
    if task_id:
        url += f"?open_discussion_task_id={task_id}#task-{task_id}"
    return url


def _calendar_event_uid(task: Task, email: str) -> str:
    identity_hash = hashlib.sha256(_normalize_email(email).encode("utf-8")).hexdigest()[:16]
    return f"termin-task-{task.id}-{identity_hash}"


def _calendar_event_description(
    task: Task,
    project_map: dict[int, Project],
    group_map: dict[int, Group],
    status_value: str,
    event_url: str,
    portal_url: str,
) -> str:
    bits = ["Termin task"]
    project = project_map.get(task.project_id)
    group = group_map.get(task.group_id) if task.group_id else None
    bits.append("")
    if project:
        bits.append(f"Project: {project.name}")
    if group:
        bits.append(f"Group: {group.name}")
    bits.append(f"Status: {status_value}")
    if event_url or portal_url:
        bits.append("")
    if event_url:
        bits.append(f"Open task: {event_url}")
    if portal_url:
        bits.append(f"Collaborator portal: {portal_url}")
    return "\n".join(bits)


def _calendar_event_url(task: Task, public_base_url: str) -> str:
    if not public_base_url:
        return ""
    selection_type = "group" if task.group_id else "project"
    selection_id = task.group_id if task.group_id else task.project_id
    return (
        f"{public_base_url}/?project_id={task.project_id}"
        f"&view=tree"
        f"&show_completed=1"
        f"&open_discussion_task_id={task.id}"
        f"&tree_select_type={selection_type}"
        f"&tree_select_id={selection_id}"
    )


def _build_calendar_feed_ics(email: str, tasks: list[Task]) -> str:
    project_ids = {task.project_id for task in tasks}
    group_ids = {task.group_id for task in tasks if task.group_id}
    project_map = {row.id: row for row in Project.query.filter(Project.id.in_(project_ids)).all()} if project_ids else {}
    group_map = {row.id: row for row in Group.query.filter(Group.id.in_(group_ids)).all()} if group_ids else {}
    status_map = task_status_meta_map(tasks, viewer_email=email)
    public_base_url = (current_app.config.get("PUBLIC_BASE_URL") or request.url_root.rstrip("/")).rstrip("/")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Termin//Assigned Tasks//EN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:" + _escape_ics_text("Termin - Assigned Tasks"),
        "X-PUBLISHED-TTL:PT6H",
    ]
    now = datetime.utcnow()
    for task in tasks:
        if not task.due_at:
            continue
        effective_status = effective_task_status_for_user(
            task,
            viewer_email=email,
            status_meta=status_map.get(task.id),
        )
        account_url = _calendar_event_url(task, public_base_url)
        portal_url = _calendar_portal_url(email, public_base_url, task.id)
        event_url = portal_url or account_url
        start_date = task.due_at.date()
        end_date = start_date + timedelta(days=1)
        event_lines = [
            "BEGIN:VEVENT",
            f"UID:{_calendar_event_uid(task, email)}",
            f"DTSTAMP:{_format_ics_timestamp(now)}",
            f"DTSTART;VALUE=DATE:{start_date.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{end_date.strftime('%Y%m%d')}",
            f"SUMMARY:{_escape_ics_text(task.title)}",
            f"DESCRIPTION:{_escape_ics_text(_calendar_event_description(task, project_map, group_map, effective_status, account_url, portal_url))}",
            "STATUS:CONFIRMED",
            "TRANSP:TRANSPARENT",
            "X-MICROSOFT-CDO-BUSYSTATUS:FREE",
            ]
        if event_url:
            event_lines.append(f"URL:{_escape_ics_text(event_url)}")
        event_lines.append("END:VEVENT")
        lines.extend(event_lines)
    lines.extend(["END:VCALENDAR", ""])
    return "\r\n".join(lines)


def _build_notification_items(rows: list[TaskNotification], *, user_id: int) -> list[dict]:
    if not rows:
        return []
    comment_ids = [row.comment_id for row in rows if row.comment_id]
    task_ids = [row.task_id for row in rows if row.task_id]
    comments = {
        row.id: row
        for row in TaskComment.query.filter(TaskComment.id.in_(comment_ids)).all()
    } if comment_ids else {}
    tasks = {
        row.id: row
        for row in Task.query.filter(Task.id.in_(task_ids)).all()
    } if task_ids else {}
    project_ids = {task.project_id for task in tasks.values() if task and task.project_id}
    group_ids = {task.group_id for task in tasks.values() if task and task.group_id}
    projects = {
        row.id: row
        for row in Project.query.filter(Project.id.in_(project_ids)).all()
    } if project_ids else {}
    groups = {
        row.id: row
        for row in Group.query.filter(Group.id.in_(group_ids)).all()
    } if group_ids else {}
    author_ids = sorted({row.user_id for row in comments.values() if row and row.user_id})
    collaborator_ids = sorted({row.collaborator_id for row in comments.values() if row and row.collaborator_id})
    authors = {
        row.id: row
        for row in User.query.filter(User.id.in_(author_ids)).all()
    } if author_ids else {}
    collaborators = {
        row.id: row
        for row in CollaboratorProfile.query.filter(CollaboratorProfile.id.in_(collaborator_ids)).all()
    } if collaborator_ids else {}
    unread_comment_task_ids = sorted({row.task_id for row in rows if row.kind == "comment" and row.read_at is None and row.task_id})
    latest_read_by_task = {}
    unread_comment_count_by_task = {}
    if unread_comment_task_ids:
        latest_read_rows = (
            db.session.query(TaskNotification.task_id, db.func.max(TaskNotification.read_at))
            .filter(
                TaskNotification.user_id == user_id,
                TaskNotification.kind == "comment",
                TaskNotification.task_id.in_(unread_comment_task_ids),
                TaskNotification.read_at.isnot(None),
            )
            .group_by(TaskNotification.task_id)
            .all()
        )
        latest_read_by_task = {task_id: read_at for task_id, read_at in latest_read_rows if task_id}
        unread_comment_count_by_task = {task_id: 0 for task_id in unread_comment_task_ids}
        for comment_row in TaskComment.query.filter(TaskComment.task_id.in_(unread_comment_task_ids)).all():
            if comment_row.user_id and int(comment_row.user_id) == int(user_id):
                continue
            threshold = latest_read_by_task.get(comment_row.task_id)
            if threshold and comment_row.created_at <= threshold:
                continue
            unread_comment_count_by_task[comment_row.task_id] = unread_comment_count_by_task.get(comment_row.task_id, 0) + 1
    items = []
    specific_task_ids = {
        row.task_id
        for row in rows
        if row.task_id and row.kind in _SPECIFIC_TASK_NOTIFICATION_KINDS
    }
    for row in rows:
        if row.kind == "task_update" and row.task_id and row.task_id in specific_task_ids:
            continue
        task = tasks.get(row.task_id)
        comment = comments.get(row.comment_id)
        group = groups.get(task.group_id) if task and task.group_id else None
        try:
            detail = json.loads(row.notification_data) if getattr(row, "notification_data", None) else {}
        except Exception:
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        project_id = detail.get("project_id") if detail.get("project_id") is not None else (task.project_id if task else None)
        try:
            project_id = int(project_id) if project_id is not None and project_id != "" else None
        except (TypeError, ValueError):
            project_id = None
        project = projects.get(project_id) if project_id else (projects.get(task.project_id) if task else None)
        sender_name = ""
        if comment:
            if comment.user_id:
                author = authors.get(comment.user_id)
                sender_name = display_name_for_user(author) if author else ""
            elif comment.collaborator_id:
                collaborator = collaborators.get(comment.collaborator_id)
                sender_name = (collaborator.display_name or collaborator.email) if collaborator else ""
        actor_name = str(detail.get("actor_name") or "").strip()
        task_label = str((task.title if task else (detail.get("project_name") or (project.name if project else "Project"))) or "Task").strip()
        inbox_preview = ""
        if row.kind == "comment":
            preview = (comment.body[:120] + "…") if comment and comment.body and len(comment.body) > 120 else ((comment.body or "").strip() if comment else "New comment")
            summary = f"{sender_name or 'New message'} commented"
            inbox_preview = preview
        else:
            if row.kind == "task_completed":
                summary = "Task completed"
                preview = (actor_name + " completed " + task_label) if actor_name else "Task completed"
                inbox_preview = "completed"
            elif row.kind == "task_due_changed":
                summary = "Due date changed"
                new_due = str(detail.get("new_due_label") or detail.get("new_due_at") or "").strip()
                if actor_name and new_due:
                    preview = actor_name + " changed " + task_label + " due date to " + new_due
                elif actor_name:
                    preview = actor_name + " changed " + task_label + " due date"
                else:
                    preview = ("Due date changed to " + new_due) if new_due else "Due date changed"
                inbox_preview = ("due date changed to " + new_due) if new_due else "due date changed"
            elif row.kind == "task_due_cleared":
                summary = "Due date removed"
                preview = (actor_name + " removed the due date from " + task_label) if actor_name else "Due date removed"
                inbox_preview = "due date removed"
            elif row.kind == "task_due_asap":
                summary = "Marked ASAP"
                preview = (actor_name + " marked " + task_label + " ASAP") if actor_name else "Marked ASAP"
                inbox_preview = "marked ASAP"
            elif row.kind == "task_status_changed":
                summary = "Status changed"
                new_status = str(detail.get("new_status") or "").strip()
                if actor_name and new_status:
                    preview = actor_name + " changed " + task_label + " status to " + new_status
                elif actor_name:
                    preview = actor_name + " changed " + task_label + " status"
                else:
                    preview = ("Status changed to " + new_status) if new_status else "Status changed"
                inbox_preview = ("status changed to " + new_status) if new_status else "status changed"
            elif row.kind == "task_renamed":
                summary = "Task renamed"
                new_title = str(detail.get("new_title") or "").strip()
                if actor_name and new_title:
                    preview = actor_name + " renamed this task to " + new_title
                elif actor_name:
                    preview = actor_name + " renamed " + task_label
                else:
                    preview = ("Renamed to " + new_title) if new_title else "Task renamed"
                inbox_preview = ("renamed to " + new_title) if new_title else "renamed"
            elif row.kind == "assignment_added":
                summary = "Assigned to task"
                if detail.get("group_assign"):
                    project_label = str(detail.get("project_name") or (project.name if project else "") or (task.title if task else "Task")).strip()
                    other_count = max(int(detail.get("group_assign_other_count") or 0), 0)
                    if actor_name:
                        preview = (
                            actor_name + " assigned " + project_label + " to you and " + str(other_count) + " other(s)"
                            if other_count
                            else actor_name + " assigned " + project_label + " to you"
                        )
                    else:
                        preview = (
                            project_label + " was assigned to you and " + str(other_count) + " other(s)"
                            if other_count
                            else project_label + " was assigned to you"
                        )
                    inbox_preview = (
                        "assigned to you and " + str(other_count) + " other(s)"
                        if other_count
                        else "assigned to you"
                    )
                else:
                    recipient_phrase = str(detail.get("recipient_assignment_phrase") or "").strip()
                    assignee = str(detail.get("assignee_label") or "").strip()
                    assigned_to_current_user = recipient_phrase.startswith("you")
                    if actor_name and recipient_phrase:
                        preview = actor_name + " assigned " + task_label + " to " + recipient_phrase
                    elif actor_name and assignee:
                        preview = actor_name + " assigned " + task_label + " to " + assignee
                    elif actor_name:
                        preview = actor_name + " assigned " + task_label
                    elif recipient_phrase:
                        preview = task_label + " was assigned to " + recipient_phrase
                    else:
                        preview = ("Assigned to " + assignee) if assignee else "Assigned to task"
                    inbox_preview = ("assigned to " + recipient_phrase) if recipient_phrase else (("assigned to " + assignee) if assignee else "assigned")
            elif row.kind == "task_moved":
                summary = "Task moved"
                preview = (actor_name + " moved " + task_label) if actor_name else "Task moved"
                inbox_preview = "moved"
            elif row.kind == "task_created":
                summary = "Task created"
                preview = (actor_name + " created " + task_label) if actor_name else "Task created"
                inbox_preview = "created"
            elif row.kind == "project_shared":
                summary = "Added to project"
                project_label = str(detail.get("project_name") or (project.name if project else "") or "Project").strip()
                preview = ((actor_name + " added you to " + project_label) if actor_name else ("Added to " + project_label))
                inbox_preview = "added you to this project"
            elif row.kind == "project_removed":
                summary = "Removed from project"
                project_label = str(detail.get("project_name") or (project.name if project else "") or "Project").strip()
                preview = ((actor_name + " removed you from " + project_label) if actor_name else ("Removed from " + project_label))
                inbox_preview = "removed you from this project"
            else:
                summary = "Task updated"
                changed_fields = [str(value).strip() for value in (detail.get("changed_fields") or []) if str(value).strip()]
                if actor_name and changed_fields:
                    preview = actor_name + " updated " + task_label + " (" + ", ".join(changed_fields) + ")"
                elif changed_fields:
                    preview = "Updated " + ", ".join(changed_fields)
                else:
                    preview = "Task updated"
                inbox_preview = ("updated " + ", ".join(changed_fields)) if changed_fields else "updated"
        items.append({
            "id": row.id,
            "task_id": row.task_id,
            "task_title": task.title if task else str(detail.get("project_name") or (project.name if project else "Project")),
            "project_id": project.id if project else project_id,
            "project_name": project.name if project else (detail.get("project_name") or None),
            "group_id": group.id if group else None,
            "group_name": group.name if group else None,
            "sender_name": sender_name,
            "detail_payload": detail,
            "summary": summary,
            "preview": preview,
            "inbox_preview": inbox_preview or preview,
            "comment_preview": preview,
            "unread_count": unread_comment_count_by_task.get(row.task_id, 0) if row.kind == "comment" and row.read_at is None else 0,
            "created_at": row.created_at,
            "created_at_iso": row.created_at.isoformat() if row.created_at else "",
            "read": row.read_at is not None,
            "pinned": bool(getattr(row, "pinned", False)),
            "kind": row.kind,
        })
    return items


def _sidebar_order_tokens(user) -> list[str]:
    accessible_projects = _accessible_projects_for_user(user)
    divisions = Division.query.filter_by(owner_id=user.id).order_by(Division.position.asc(), Division.id.asc()).all()
    pref_map = sidebar_preference_map(user.id, accessible_projects, divisions)
    items = top_level_sidebar_items(accessible_projects, divisions, pref_map)
    return [f"{item['type']}:{item['id']}" for item in items]


def _emit_collaborator_comment_previews(task: Task, collaborator: CollaboratorProfile, preview: str) -> None:
    if not task or not collaborator:
        return
    created_at = datetime.utcnow().isoformat()
    actor_name = collaborator.display_name or collaborator.email or "New message"
    for recipient_id in sorted(task_discussion_user_ids(task)):
        if is_user_viewing_discussion(recipient_id, "task", task.id):
            continue
        if not user_notification_channel_enabled(recipient_id, "task_message", "push", task=task):
            continue
        emit_user_notification_preview(
            recipient_id,
            {
                "id": ":".join(["live-comment", str(task.id), str(task.project_id or ""), str(task.group_id or ""), created_at]),
                "task_id": task.id,
                "project_id": task.project_id or "",
                "project_name": "",
                "group_id": task.group_id or "",
                "group_name": "",
                "task_title": task.title or "Task",
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
                    "actor_avatar_url": "",
                },
            },
        )


ui_bp = Blueprint("ui", __name__)


_SPECIFIC_TASK_NOTIFICATION_KINDS = {
    "task_completed",
    "task_due_changed",
    "task_due_cleared",
    "task_due_asap",
    "task_status_changed",
    "task_renamed",
    "assignment_added",
    "task_moved",
    "task_created",
}


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user_is_admin(user):
            return redirect(url_for("ui.dashboard"))
        return fn(*args, **kwargs)
    return wrapper


def _task_ordering(query):
    return query.order_by(Task.position.asc(), Task.id.asc())


def _should_show_todo_task(task: Task, show_completed: bool, today: date, *, viewer_status: str | None = None) -> bool:
    task_status = viewer_status if viewer_status is not None else task.status
    if show_completed or not _is_complete_status(task_status):
        return True
    if task.due_at and task.due_at.date() >= today:
        return True
    return False


def _division_palette() -> list[str]:
    return [
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
    ]


def _build_dashboard_viewer_maps(projects: list[Project]) -> tuple[dict[int, list[User]], dict[int, list[User]], dict[int, User]]:
    if not projects:
        return {}, {}, {}

    project_ids = [project.id for project in projects]
    project_by_id = {project.id: project for project in projects}
    group_rows = Group.query.filter(Group.project_id.in_(project_ids)).all()
    group_by_id = {group.id: group for group in group_rows}
    group_ids = list(group_by_id.keys())

    project_flags: dict[int, dict[int, dict[str, bool]]] = {project_id: {} for project_id in project_ids}
    group_user_ids: dict[int, set[int]] = {group_id: set() for group_id in group_ids}
    user_ids: set[int] = set()

    def ensure_project_flag(project_id: int, user_id: int) -> dict[str, bool]:
        row = project_flags.setdefault(project_id, {}).setdefault(
            user_id,
            {"owner": False, "project_member": False, "group_member": False, "task_assignee": False},
        )
        user_ids.add(user_id)
        return row

    for project in projects:
        if project.owner_id:
            flag = ensure_project_flag(project.id, project.owner_id)
            flag["owner"] = True
            flag["project_member"] = True

    for row in ProjectMember.query.filter(ProjectMember.project_id.in_(project_ids)).all():
        flag = ensure_project_flag(row.project_id, row.user_id)
        flag["project_member"] = True

    if group_ids:
        for row in GroupMember.query.filter(GroupMember.group_id.in_(group_ids)).all():
            group = group_by_id.get(row.group_id)
            if not group:
                continue
            group_user_ids.setdefault(row.group_id, set()).add(row.user_id)
            flag = ensure_project_flag(group.project_id, row.user_id)
            flag["project_member"] = True
            flag["group_member"] = True
            user_ids.add(row.user_id)

    user_map = {
        user.id: user
        for user in User.query.filter(User.id.in_(user_ids)).all()
    } if user_ids else {}

    project_viewers: dict[int, list[User]] = {}
    for project_id, flags_by_user in project_flags.items():
        viewers = [user_map[user_id] for user_id in flags_by_user.keys() if user_id in user_map]
        viewers.sort(
            key=lambda user: (
                0 if flags_by_user[user.id].get("owner") else 1,
                0 if flags_by_user[user.id].get("project_member") else 1,
                (user.display_name or user.email or "").lower(),
            )
        )
        project_viewers[project_id] = viewers

    group_viewers: dict[int, list[User]] = {}
    for group_id, member_ids in group_user_ids.items():
        group = group_by_id.get(group_id)
        if not group:
            continue
        project_flag_map = project_flags.get(group.project_id, {})
        combined_ids = {
            user_id
            for user_id, flags in project_flag_map.items()
            if flags.get("owner") or flags.get("project_member")
        }
        viewers = [user_map[user_id] for user_id in combined_ids if user_id in user_map]
        viewers.sort(
            key=lambda user: (
                0 if project_flag_map.get(user.id, {}).get("owner") else 1,
                0 if project_flag_map.get(user.id, {}).get("project_member") else 1,
                (user.display_name or user.email or "").lower(),
            )
        )
        group_viewers[group_id] = viewers

    return project_viewers, group_viewers, user_map


def _render_account_page(user: User, *, section: str = "emails", error: str | None = None, message: str | None = None):
    emails = list_user_emails(user.id)
    identities = list_external_identities(user.id)
    first_identity = identities[0] if identities else None
    default_display_name = (
        (first_identity.display_name or first_identity.email or first_identity.provider_user_id)
        if first_identity
        else (user.email or None)
    )
    pending = EmailVerification.query.filter(
        EmailVerification.user_id == user.id,
        EmailVerification.consumed_at.is_(None),
    ).order_by(EmailVerification.created_at.desc()).all()
    github_identity = github_identity_for_user(user.id)
    github_sync_state = github_sync_state_for_user(user.id)
    notification_preferences = user_notification_preference_map(user.id)
    notification_rows = (
        TaskNotification.query.filter_by(user_id=user.id)
        .filter(or_(TaskNotification.read_at.is_(None), TaskNotification.pinned.is_(True)))
        .order_by(TaskNotification.pinned.desc(), TaskNotification.created_at.desc(), TaskNotification.id.desc())
        .limit(24)
        .all()
    )
    notification_items = _build_notification_items(notification_rows, user_id=user.id)
    task_notifications = [item for item in notification_items if item["kind"] != "comment"]
    message_notifications = [item for item in notification_items if item["kind"] == "comment"]
    return render_template(
        "account.html",
        user=user,
        emails=emails,
        identities=identities,
        pending_verifications=pending,
        github_identity=github_identity,
        github_sync_state=github_sync_state,
        title="Settings",
        account_section=section,
        default_display_name=default_display_name,
        common_timezone_options=COMMON_TIMEZONE_OPTIONS,
        all_timezone_options=ALL_TIMEZONE_OPTIONS,
        notification_event_definitions=NOTIFICATION_EVENT_DEFINITIONS,
        general_notification_definitions=GENERAL_NOTIFICATION_DEFINITIONS,
        task_notification_definitions=TASK_NOTIFICATION_DEFINITIONS,
        task_notification_scopes=TASK_NOTIFICATION_SCOPES,
        notification_preferences=notification_preferences,
        email_frequency_options=EMAIL_FREQUENCY_OPTIONS,
        notification_email_frequency_seconds=normalize_notification_email_frequency(user.notification_email_frequency_seconds),
        task_notifications=task_notifications,
        message_notifications=message_notifications,
        error=error,
        merge_message=message,
    )


def _build_github_task_meta(user_id: int, task_ids: list[int], user_map: dict[int, User]) -> tuple[dict[int, dict], int | None]:
    if not task_ids:
        return {}, None

    github_links = GitHubIssueLink.query.filter(GitHubIssueLink.task_id.in_(task_ids)).all()
    if not github_links:
        return {}, None

    provider_ids = set()
    for link in github_links:
        try:
            assignees = json.loads(link.assignees_json) if link.assignees_json else []
        except json.JSONDecodeError:
            assignees = []
        for assignee in assignees:
            provider_user_id = str((assignee or {}).get("provider_user_id") or "").strip()
            if provider_user_id:
                provider_ids.add(provider_user_id)

    github_identities = (
        ExternalIdentity.query.filter(
            ExternalIdentity.provider == "github",
            ExternalIdentity.provider_user_id.in_(provider_ids),
        ).all()
        if provider_ids
        else []
    )
    identities_by_provider_user_id = {identity.provider_user_id: identity for identity in github_identities}
    sync_state = GitHubSyncState.query.filter_by(user_id=user_id).first()

    meta_by_task: dict[int, dict] = {}
    for link in github_links:
        try:
            assignees = json.loads(link.assignees_json) if link.assignees_json else []
        except json.JSONDecodeError:
            assignees = []
        assignee_meta = []
        for assignee in assignees:
            provider_user_id = str((assignee or {}).get("provider_user_id") or "").strip()
            identity = identities_by_provider_user_id.get(provider_user_id)
            connected_user = user_map.get(identity.user_id) if identity and identity.user_id in user_map else None
            label = (
                (connected_user.display_name or connected_user.email)
                if connected_user
                else ((assignee or {}).get("login") or provider_user_id or "GitHub")
            )
            assignee_meta.append(
                {
                    "label": label,
                    "avatar_url": (
                        connected_user.avatar_url
                        if connected_user and connected_user.avatar_url
                        else (identity.avatar_url if identity and identity.avatar_url else (assignee or {}).get("avatar_url"))
                    ),
                    "provider_user_id": provider_user_id,
                    "profile_url": (assignee or {}).get("html_url"),
                    "is_connected": bool(connected_user),
                    "login": (assignee or {}).get("login") or provider_user_id,
                    "role": (assignee or {}).get("role") or "assignee",
                }
            )
        meta_by_task[link.task_id] = {
            "issue_url": link.issue_url,
            "repository_full_name": link.repository_full_name,
            "issue_number": link.issue_number,
            "issue_state": link.issue_state,
            "item_type": link.item_type or "issue",
            "assignees": assignee_meta,
        }
    return meta_by_task, (sync_state.project_id if sync_state else None)


def _render_admin_page(template: str, *, section: str, **context):
    return render_template(
        template,
        title="Admin",
        admin_section=section,
        **context,
    )


def _deployment_git_info() -> dict[str, str | bool | None]:
    repo_root = Path(__file__).resolve().parent.parent

    def run_git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        return (result.stdout or "").strip() or None

    commit = run_git("rev-parse", "HEAD")
    short_commit = run_git("rev-parse", "--short", "HEAD")
    branch = run_git("rev-parse", "--abbrev-ref", "HEAD")
    status = run_git("status", "--porcelain")
    return {
        "commit": commit,
        "short_commit": short_commit,
        "branch": branch,
        "is_dirty": bool(status),
    }


def _safe_ensure_task_event(task_id: int, invitee_email: str | None = None) -> None:
    try:
        ensure_task_event(task_id, invitee_email)
    except CalendarSyncError as exc:
        current_app.logger.warning("Calendar sync skipped for task %s: %s", task_id, exc)


def _calendar_window(task: Task | None) -> tuple[datetime, datetime] | tuple[None, None]:
    start_at = task.due_at if task else None
    if not start_at:
        return None, None
    return start_at, start_at + timedelta(hours=1)


def _send_collaborator_calendar_invite(
    collaborator: CollaboratorProfile,
    invite: Invite,
    task: Task | None,
    project: Project | None,
) -> bool:
    start_at, end_at = _calendar_window(task)
    if not start_at or not end_at:
        return False

    title = task.title if task else "Task"
    summary = title
    context_parts: list[str] = []
    if project:
        context_parts.append(project.name)
    description_lines = [title]
    if context_parts:
        description_lines.append(" | ".join(context_parts))
    description_lines.append("Manage this task in Termin:")
    description_lines.append(url_for("ui.collaborator_portal", token=collaborator.access_token, _external=True))

    send_calendar_invite_email(
        to_email=invite.email,
        open_url=url_for("ui.collaborator_portal", token=collaborator.access_token, _external=True),
        complete_url=url_for("ui.quick_collaborator_invite_action", token=collaborator.access_token, invite_id=invite.id, action="complete", _external=True),
        summary=summary,
        description="\n".join(description_lines),
        start_at=start_at,
        end_at=end_at,
        uid=f"invite-{invite.token}@termin.solids.group",
        reply_to=current_user().email if current_user() else None,
    )
    invite.calendar_invite_sent_at = datetime.utcnow()
    invite.calendar_opt_in = True
    return True


def _resolve_work_item(task_id: int | None) -> Task | None:
    return Task.query.get(task_id) if task_id else None


def _build_collaborator_entries(collaborator: CollaboratorProfile) -> list[dict]:
    invites = Invite.query.filter_by(email=collaborator.email).order_by(Invite.created_at.desc(), Invite.id.desc()).all()
    assignments = Assignment.query.filter_by(email=collaborator.email).order_by(Assignment.created_at.desc(), Assignment.id.desc()).all()
    invite_by_assignment_id = {invite.assignment_id: invite for invite in invites if invite.assignment_id}
    task_ids = {invite.task_id for invite in invites if invite.task_id}
    task_ids.update({assignment.task_id for assignment in assignments if assignment.task_id})
    tasks = {task.id: task for task in Task.query.filter(Task.id.in_(task_ids)).all()} if task_ids else {}
    task_status_map = task_status_meta_map(list(tasks.values()), viewer_email=collaborator.email) if tasks else {}
    project_ids = {task.project_id for task in tasks.values() if task and task.project_id}
    projects = {project.id: project for project in Project.query.filter(Project.id.in_(project_ids)).all()} if project_ids else {}
    entries: list[dict] = []
    default_calendar_opt_in = collaborator.new_task_notification_preference == "calendar"

    def resolve_context(task_id: int | None) -> tuple[Task | None, Project | None]:
        task = tasks.get(task_id) if task_id else None
        project = None
        if task and task.project_id:
            project = projects.get(task.project_id)
            if not project:
                project = Project.query.get(task.project_id)
                if project:
                    projects[project.id] = project
        return task, project

    for assignment in assignments:
        invite = invite_by_assignment_id.get(assignment.id)
        task, project = resolve_context(assignment.task_id)
        work_status = effective_task_status_for_user(
            task,
            viewer_email=collaborator.email,
            status_meta=task_status_map.get(task.id) if task else None,
        )
        entries.append(
            {
                "kind": "assignment",
                "assignment": assignment,
                "invite": invite,
                "task": task,
                "project": project,
                "status": invite.status if invite else assignment.status,
                "calendar_opt_in": invite.calendar_opt_in if invite else default_calendar_opt_in,
                "calendar_available": bool(task and task.due_at),
                "calendar_invite_sent_at": invite.calendar_invite_sent_at if invite else None,
                "created_at": (invite.created_at if invite else assignment.created_at),
                "work_status": work_status,
                "is_complete": _is_complete_status(work_status),
            }
        )

    assigned_invite_ids = {invite.id for invite in invites if invite.assignment_id}
    for invite in invites:
        if invite.id in assigned_invite_ids:
            continue
        task, project = resolve_context(invite.task_id)
        work_status = effective_task_status_for_user(
            task,
            viewer_email=collaborator.email,
            status_meta=task_status_map.get(task.id) if task else None,
        )
        entries.append(
            {
                "kind": "invite",
                "assignment": None,
                "invite": invite,
                "task": task,
                "project": project,
                "status": invite.status,
                "calendar_opt_in": invite.calendar_opt_in,
                "calendar_available": bool(task and task.due_at),
                "calendar_invite_sent_at": invite.calendar_invite_sent_at,
                "created_at": invite.created_at,
                "work_status": work_status,
                "is_complete": _is_complete_status(work_status),
            }
        )

    deduped: dict[tuple[str, int], dict] = {}
    for entry in entries:
        if entry.get("task"):
            key = ("task", entry["task"].id)
        elif entry.get("invite"):
            invite = entry["invite"]
            key = ("invite", invite.id)
        else:
            assignment = entry["assignment"]
            key = ("assignment", assignment.id)

        existing = deduped.get(key)
        if not existing:
            deduped[key] = entry
            continue

        existing_sort = (
            existing["created_at"],
            existing["invite"].id if existing["invite"] else existing["assignment"].id,
        )
        candidate_sort = (
            entry["created_at"],
            entry["invite"].id if entry["invite"] else entry["assignment"].id,
        )
        if candidate_sort > existing_sort:
            deduped[key] = entry

    final_entries = list(deduped.values())
    final_entries.sort(
        key=lambda item: (item["created_at"], item["invite"].id if item["invite"] else item["assignment"].id),
        reverse=True,
    )
    return final_entries


def _collaborator_task_ids(collaborator: CollaboratorProfile) -> set[int]:
    entries = _build_collaborator_entries(collaborator)
    return {entry["task"].id for entry in entries if entry.get("task")}


def _work_item_status(task: Task | None) -> str:
    return (task.status if task else "open") or "open"


def _is_complete_status(status: str | None) -> bool:
    return (status or "").strip().lower() in {"complete", "completed", "done", "closed", "pr closed", "pr merged"}


def _mark_work_item_complete(task: Task | None) -> None:
    if task:
        task.status = "complete"


def _mark_work_item_open(task: Task | None) -> None:
    if task:
        task.status = "open"


def _set_collaborator_work_item_status(task: Task | None, collaborator_email: str | None, status: str) -> None:
    if not task:
        return
    normalized_email = (collaborator_email or "").strip().lower()
    if (
        task.per_user_status_enabled
        and normalized_email
        and db.session.query(Assignment.id)
        .filter(
            Assignment.task_id == task.id,
            Assignment.email.isnot(None),
            db.func.lower(Assignment.email) == normalized_email,
        )
        .first()
    ):
        set_task_collaborator_status(task.id, normalized_email, status)
        return
    if (status or "").strip().lower() == "complete":
        _mark_work_item_complete(task)
    else:
        _mark_work_item_open(task)


def _serialize_portal_comment(comment: TaskComment, users: dict[int, User], collaborators: dict[int, CollaboratorProfile]) -> dict:
    collaborator = collaborators.get(comment.collaborator_id) if comment.collaborator_id else None
    user = users.get(comment.user_id) if comment.user_id else None
    if collaborator:
        author = {
            "id": collaborator.id,
            "display_name": collaborator.display_name or collaborator.email,
            "email": collaborator.email,
            "avatar_url": None,
            "kind": "collaborator",
        }
    else:
        label = (user.display_name or user.email) if user else "Unknown user"
        author = {
            "id": user.id if user else None,
            "display_name": label,
            "email": user.email if user else None,
            "avatar_url": user.avatar_url if user else None,
            "kind": "user",
        }
    return {
        "id": comment.id,
        "task_id": comment.task_id,
        "user_id": comment.user_id,
        "collaborator_id": comment.collaborator_id,
        "body": comment.body,
        "created_at": comment.created_at.isoformat(),
        "updated_at": comment.updated_at.isoformat(),
        "author": author,
    }


def _apply_invite_response(invite: Invite, action: str, calendar_opt_in: bool) -> None:
    if action == "accept":
        invite.status = "accepted"
        invite.calendar_opt_in = calendar_opt_in
    elif action == "decline":
        invite.status = "declined"
        invite.calendar_opt_in = False

    if invite.assignment_id:
        assignment = Assignment.query.get(invite.assignment_id)
        if assignment:
            assignment.status = "accepted" if invite.status == "accepted" else "denied"


def _ensure_assignment_invite(assignment: Assignment, calendar_opt_in: bool = False) -> Invite:
    invite = Invite.query.filter_by(assignment_id=assignment.id).first()
    if invite:
        return invite
    invite = Invite(
        task_id=assignment.task_id,
        assignment_id=assignment.id,
        email=assignment.email,
        token=secrets.token_hex(24),
        status="sent",
        calendar_opt_in=calendar_opt_in,
    )
    db.session.add(invite)
    db.session.flush()
    return invite


@ui_bp.get("/")
@login_required
def dashboard():
    user = current_user()
    now_utc = datetime.utcnow()
    github_identity = github_identity_for_user(user.id)
    github_sync_state = github_sync_state_for_user(user.id)
    github_project_id = github_sync_state.project_id if github_sync_state else None
    github_auto_sync_needed = bool(github_identity and github_identity.access_token and should_sync_github_issues(user.id))
    current_view = (request.args.get("view") or "tree").strip().lower()
    if current_view == "project":
        current_view = "tree"
    if current_view not in {"todo", "tree", "inbox"}:
        current_view = "tree"
    default_show_completed = "0" if current_view == "todo" else "1"
    if current_view in {"tree", "todo"}:
        show_completed = True
    else:
        show_completed = (request.args.get("show_completed") or default_show_completed).strip().lower() not in {"0", "false", "no", "off"}
    owned_projects = Project.query.filter_by(owner_id=user.id).all()
    direct_member_project_ids = [
        m.project_id for m in ProjectMember.query.filter_by(user_id=user.id).all()
    ]
    member_group_project_ids = [
        row.project_id
        for row in db.session.query(Group.project_id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(GroupMember.user_id == user.id)
        .all()
    ]
    member_project_ids = list(set(direct_member_project_ids).union(member_group_project_ids))
    accessible_project_ids = list(
        {p.id for p in owned_projects}
        .union(member_project_ids)
    )
    projects = (
        Project.query.filter(Project.id.in_(accessible_project_ids)).all()
        if accessible_project_ids
        else []
    )
    standard_projects = [project for project in projects if not project.is_direct]
    direct_projects = [project for project in projects if project.is_direct]
    direct_projects.sort(
        key=lambda project: ((_project_display_name_for_user(project, user.id) or "").lower(), project.id)
    )
    sidebar_divisions = Division.query.filter_by(owner_id=user.id).order_by(Division.position.asc(), Division.name.asc(), Division.id.asc()).all()
    division_options = [{"id": division.id, "name": division.name, "color": division.color} for division in sidebar_divisions]
    sidebar_pref_map = sidebar_preference_map(user.id, standard_projects, sidebar_divisions)
    projects_by_division = {division.id: [] for division in sidebar_divisions}
    top_level_items = top_level_sidebar_items(standard_projects, sidebar_divisions, sidebar_pref_map)
    for project in standard_projects:
        pref = sidebar_pref_map.get(project.id)
        if pref and pref.division_id and pref.division_id in projects_by_division:
            projects_by_division[pref.division_id].append(project)
    for division_id, grouped_projects in projects_by_division.items():
        grouped_projects.sort(key=lambda project: ((sidebar_pref_map.get(project.id).position if sidebar_pref_map.get(project.id) else 0), project.id))

    groups_by_project: dict[int, list[Group]] = {project.id: [] for project in projects}
    if projects:
        group_rows = (
            Group.query.filter(Group.project_id.in_([project.id for project in projects]))
            .order_by(Group.position.asc(), Group.id.asc())
            .all()
        )
        for group in group_rows:
            groups_by_project.setdefault(group.project_id, []).append(group)

    unassigned_projects = [
        project
        for project in projects
        if project.is_direct or not (sidebar_pref_map.get(project.id) and sidebar_pref_map.get(project.id).division_id)
    ]

    selected_project = None
    division_color_map = {division.id: (division.color or "#4cc9f0") for division in sidebar_divisions}
    selected_id = request.args.get("project_id")
    open_task_id = request.args.get("open_discussion_task_id")
    tree_selection_type = (request.args.get("tree_select_type") or "").strip().lower()
    tree_selection_id = (request.args.get("tree_select_id") or "").strip()
    if tree_selection_type not in {"division", "project", "group"}:
        tree_selection_type = ""
        tree_selection_id = ""
    selected_from_task_id = None
    dashboard_notice = None
    if open_task_id:
        try:
            open_task_id_int = int(open_task_id)
        except ValueError:
            open_task_id_int = None
            dashboard_notice = "That task link is invalid."
        if open_task_id_int:
            open_task = Task.query.filter_by(id=open_task_id_int).first()
            if open_task and open_task.project_id in accessible_project_ids:
                selected_from_task_id = open_task.project_id
                current_view = "tree"
                if not tree_selection_type or not tree_selection_id:
                    if open_task.group_id:
                        tree_selection_type = "group"
                        tree_selection_id = str(open_task.group_id)
                    else:
                        tree_selection_type = "project"
                        tree_selection_id = str(open_task.project_id)
            elif open_task:
                dashboard_notice = "You do not have access to that task."
            else:
                dashboard_notice = "That task link is no longer valid."
    if projects:
        if selected_from_task_id:
            selected_project = next((p for p in projects if p.id == selected_from_task_id), None)
        if selected_id:
            try:
                selected_id_int = int(selected_id)
            except ValueError:
                selected_id_int = None
            if selected_id_int and not selected_project:
                selected_project = next((p for p in projects if p.id == selected_id_int), None)
        if not selected_project:
            selected_project = standard_projects[0] if standard_projects else (direct_projects[0] if direct_projects else projects[0])

    tasks = []
    groups = []
    tasks_by_group = {}
    ungrouped_tasks = []
    is_owner = False
    is_project_member = False
    can_manage_project = False
    manageable_group_ids: set[int] = set()
    selected_project_color = "#4cc9f0"
    is_github_project = False
    tree_project_contexts = []
    manageable_project_ids = {project.id for project in owned_projects}.union(member_project_ids)

    def build_project_context(project: Project) -> dict:
        project_is_github = bool(github_project_id and project.id == github_project_id)
        pref = sidebar_pref_map.get(project.id)
        project_color = division_color_map.get(pref.division_id if pref else None, "#4cc9f0")
        project_is_owner = project.owner_id == user.id
        project_is_member = (
            ProjectMember.query.filter_by(project_id=project.id, user_id=user.id).first()
            is not None
            or (
                db.session.query(Group.id)
                .join(GroupMember, GroupMember.group_id == Group.id)
                .filter(Group.project_id == project.id, GroupMember.user_id == user.id)
                .first()
                is not None
            )
        )
        project_can_manage = project_is_owner or project_is_member
        project_manageable_group_ids: set[int] = set()
        project_tasks: list[Task] = []
        project_groups: list[Group] = []
        project_tasks_by_group: dict[int, list[Task]] = {}
        project_ungrouped_tasks: list[Task] = []
        project_tasks = _task_ordering(Task.query.filter_by(project_id=project.id)).all()
        project_status_map = task_status_meta_map(project_tasks, viewer_user_id=user.id)
        if not show_completed:
            project_tasks = [
                task for task in project_tasks
                if not _is_complete_status(
                    effective_task_status_for_user(task, viewer_user_id=user.id, status_meta=project_status_map.get(task.id))
                )
            ]
        project_groups = Group.query.filter_by(project_id=project.id).order_by(Group.position.asc(), Group.id.asc()).all()
        project_tasks_by_group = {group.id: [] for group in project_groups}
        for task in project_tasks:
            if task.group_id and task.group_id in project_tasks_by_group:
                project_tasks_by_group[task.group_id].append(task)
            else:
                project_ungrouped_tasks.append(task)

        if project_is_github:
            for grouped_tasks in project_tasks_by_group.values():
                grouped_tasks.sort(key=lambda task: (task.created_at, task.id), reverse=True)
            project_ungrouped_tasks.sort(key=lambda task: (task.created_at, task.id), reverse=True)

        return {
            "project": project,
            "groups": project_groups,
            "tasks": project_tasks,
            "tasks_by_group": project_tasks_by_group,
            "ungrouped_tasks": project_ungrouped_tasks,
            "can_manage_project": project_can_manage,
            "manageable_group_ids": project_manageable_group_ids,
            "is_owner": project_is_owner,
            "is_project_member": project_is_member,
            "color": project_color,
            "is_github_project": project_is_github,
            "division_id": pref.division_id if pref else None,
            "project_info": load_info_payload(project.info, project.link),
            "group_info_by_id": {
                group.id: load_info_payload(group.info, group.link)
                for group in project_groups
            },
            "display_name": _project_display_name_for_user(project, user.id),
        }

    if selected_project:
        selected_project_context = build_project_context(selected_project)
        is_github_project = selected_project_context["is_github_project"]
        selected_project_color = selected_project_context["color"]
        is_owner = selected_project_context["is_owner"]
        is_project_member = selected_project_context["is_project_member"]
        can_manage_project = selected_project_context["can_manage_project"]
        manageable_group_ids = selected_project_context["manageable_group_ids"]
        tasks = selected_project_context["tasks"]
        groups = selected_project_context["groups"]
        tasks_by_group = selected_project_context["tasks_by_group"]
        ungrouped_tasks = selected_project_context["ungrouped_tasks"]
    assignments_by_task = {}
    group_assignment_members_by_task = {}
    info_by_task = {}
    project_map = {project.id: project for project in projects}
    project_info = load_info_payload(selected_project.info, selected_project.link) if selected_project else {"html": "", "attachments": [], "links": []}
    group_info_by_id = {group.id: load_info_payload(group.info, group.link) for group in groups}
    project_viewers, group_viewers, viewer_user_map = _build_dashboard_viewer_maps(projects)
    user_map = dict(viewer_user_map)
    direct_project_peers = {project.id: _direct_peer_for_user(project, user.id) for project in direct_projects}
    project_display_names = {project.id: _project_display_name_for_user(project, user.id) for project in projects}
    project_hierarchy_meta = {}
    for project in projects:
        pref = sidebar_pref_map.get(project.id) if not project.is_direct else None
        division = next((item for item in sidebar_divisions if pref and item.id == pref.division_id), None)
        project_hierarchy_meta[project.id] = {
            "division_name": division.name if division else None,
            "division_color": (division.color or "#4cc9f0") if division else None,
            "project_name": project_display_names.get(project.id, project.name),
        }
    shared_project_ids = {
        project.id
        for project in projects
        if not project.is_direct and int(project.owner_id or 0) != int(user.id)
    }
    shared_group_ids = {
        group.id
        for group in Group.query.filter(Group.project_id.in_(shared_project_ids if shared_project_ids else [-1])).all()
    } if shared_project_ids else set()
    shared_out_project_ids = {
        project.id
        for project in projects
        if project.id not in shared_project_ids
        and any(int(viewer.id) != int(user.id) for viewer in project_viewers.get(project.id, []))
    }
    shared_out_group_ids = {
        group.id
        for group in Group.query.filter(Group.project_id.in_(shared_out_project_ids if shared_out_project_ids else [-1])).all()
    }
    selected_project_display_name = project_display_names.get(selected_project.id, selected_project.name) if selected_project else ""
    tree_groups_by_project: dict[int, list[Group]] = {}
    if current_view == "tree":
        tree_source_projects = standard_projects + direct_projects
        tree_project_contexts = [build_project_context(project) for project in tree_source_projects]
        tree_groups_by_project = {
            context["project"].id: context["groups"]
            for context in tree_project_contexts
        }
    today = now_utc.date()
    todo_items = []
    todo_tasks = []
    todo_assignee_options = []
    if current_view == "todo":
        project_ids = [p.id for p in projects]
        visible_task_ids = set()
        if project_ids:
            visible_task_ids.update(
                row.id for row in Task.query.filter(Task.project_id.in_(project_ids)).all()
            )
        todo_tasks = Task.query.filter(Task.id.in_(visible_task_ids)).all() if visible_task_ids else []
        todo_status_map = task_status_meta_map(todo_tasks, viewer_user_id=user.id)
        if not show_completed:
            todo_tasks = [
                task for task in todo_tasks
                if _should_show_todo_task(
                    task,
                    show_completed,
                    today,
                    viewer_status=effective_task_status_for_user(task, viewer_user_id=user.id, status_meta=todo_status_map.get(task.id)),
                )
            ]
        todo_groups = {group.id: group for group in Group.query.filter(Group.id.in_([task.group_id for task in todo_tasks if task.group_id])).all()} if todo_tasks else {}
        week_start = today - timedelta(days=today.weekday())
        next_week_start = week_start + timedelta(days=7)
        two_weeks_start = week_start + timedelta(days=14)
        three_weeks_start = week_start + timedelta(days=21)
        four_weeks_start = week_start + timedelta(days=28)
        next_month_year = today.year + (1 if today.month == 12 else 0)
        next_month_month = 1 if today.month == 12 else today.month + 1
        month_after_next_year = next_month_year + (1 if next_month_month == 12 else 0)
        month_after_next_month = 1 if next_month_month == 12 else next_month_month + 1
        next_month_start = date(next_month_year, next_month_month, 1)
        month_after_next_start = date(month_after_next_year, month_after_next_month, 1)

        def todo_bucket(due_date):
            if due_date is None:
                return ("none", "No Due Date", 9)
            if due_date < today:
                return ("overdue", "Overdue", 0)
            if due_date == today:
                return ("today", "Today", 1)
            if due_date == today + timedelta(days=1):
                return ("tomorrow", "Tomorrow", 2)
            if due_date < next_week_start:
                return ("this_week", "This Week", 3)
            if due_date < two_weeks_start:
                return ("next_week", "Next Week", 4)
            if due_date < three_weeks_start:
                return ("two_weeks", "Two Weeks", 5)
            if due_date < four_weeks_start:
                return ("three_weeks", "Three Weeks", 6)
            if next_month_start <= due_date < month_after_next_start:
                return ("next_month", "Next Month", 7)
            return ("later", "Later", 8)

        for task in todo_tasks:
            project = project_map.get(task.project_id)
            if not project:
                continue
            task_info = load_info_payload(task.info, task.link)
            due_mode = str(task_info.get("meta", {}).get("due_mode") or "").strip().lower()
            pref = sidebar_pref_map.get(project.id)
            division = next((item for item in sidebar_divisions if pref and item.id == pref.division_id), None)
            if due_mode == "asap" and not task.due_at:
                bucket_key, bucket_label, bucket_rank = ("asap", "ASAP", 1)
            else:
                bucket_key, bucket_label, bucket_rank = todo_bucket(task.due_at.date() if task.due_at else None)
            todo_items.append(
                {
                    "task": task,
                    "project": project,
                    "group": todo_groups.get(task.group_id),
                    "division_color": (division.color if division and division.color else "#4cc9f0"),
                    "division_id": (division.id if division else None),
                    "date_key": bucket_key,
                    "date_label": bucket_label,
                    "date_rank": bucket_rank,
                }
            )
        todo_items.sort(
            key=lambda item: (
                item["date_rank"],
                item["task"].due_at.date().isoformat() if item["task"].due_at else "9999-12-31",
                item["project"].name.lower(),
                item["task"].position or 0,
                item["task"].id,
            )
        )
    visible_tasks = list({
        task.id: task
        for task in (
            list(todo_tasks)
            + list(tasks)
            + [task for context in tree_project_contexts for task in context["tasks"]]
        )
    }.values())
    task_status_payloads = task_status_meta_map(visible_tasks, viewer_user_id=user.id)
    if visible_tasks:
        assignment_rows = Assignment.query.filter(Assignment.task_id.in_([t.id for t in visible_tasks])).all()
        for assignment in assignment_rows:
            assignments_by_task.setdefault(assignment.task_id, []).append(assignment)
        for task in visible_tasks:
            info_by_task[task.id] = load_info_payload(task.info, task.link)
            if task.assign_group_members:
                group_assignment_members_by_task[task.id] = serialize_group_assignment_members(task)

    missing_assignee_ids = {
        assignment.user_id
        for assignments in assignments_by_task.values()
        for assignment in assignments
        if assignment.user_id and assignment.user_id not in user_map
    }
    if missing_assignee_ids:
        for assignee in User.query.filter(User.id.in_(missing_assignee_ids)).all():
            user_map[assignee.id] = assignee
    if current_view == "todo" and todo_tasks:
        todo_assignee_map = {}
        for task in todo_tasks:
            for assignment in assignments_by_task.get(task.id, []):
                key = None
                label = ""
                avatar_url = ""
                sort_key = ""
                option_type = "email"
                if assignment.user_id:
                    assignee = user_map.get(assignment.user_id)
                    if assignee:
                        key = f"user:{assignment.user_id}"
                        label = assignee.display_name or assignee.email
                        avatar_url = assignee.avatar_url or ""
                        sort_key = (assignee.display_name or assignee.email or "").lower()
                        option_type = "user"
                elif assignment.email:
                    normalized_email = assignment.email.strip().lower()
                    if normalized_email:
                        key = f"email:{normalized_email}"
                        label = assignment.email.strip()
                        sort_key = normalized_email
                if key and key not in todo_assignee_map:
                    todo_assignee_map[key] = {
                        "key": key,
                        "label": label,
                        "avatar_url": avatar_url,
                        "type": option_type,
                        "sort_key": sort_key,
                    }
        todo_assignee_options = sorted(
            todo_assignee_map.values(),
            key=lambda item: (0 if item["type"] == "user" else 1, item["sort_key"], item["label"].lower()),
        )
    github_task_meta, github_project_id = _build_github_task_meta(
        user.id,
        [task.id for task in list(todo_tasks) + list(tasks)],
        user_map,
    )
    visible_dashboard_task_ids = {
        task.id
        for task in (
            todo_tasks
            if current_view == "todo"
            else [task for context in tree_project_contexts for task in context["tasks"]]
            if current_view == "tree"
            else tasks
        )
    }
    comment_counts = {}
    if visible_dashboard_task_ids:
        comment_counts = {
            task_id: count
            for task_id, count in (
                db.session.query(TaskComment.task_id, db.func.count(TaskComment.id))
                .filter(TaskComment.task_id.in_(visible_dashboard_task_ids))
                .group_by(TaskComment.task_id)
                .all()
            )
        }
    notification_rows = (
        TaskNotification.query.filter_by(user_id=user.id)
        .filter(TaskNotification.read_at.is_(None))
        .order_by(TaskNotification.created_at.desc(), TaskNotification.id.desc())
        .limit(12)
        .all()
    )
    unread_task_ids = {
        task_id
        for (task_id,) in (
            db.session.query(TaskNotification.task_id)
            .filter(TaskNotification.user_id == user.id, TaskNotification.read_at.is_(None))
            .distinct()
            .all()
        )
        if task_id
    }
    notification_comment_ids = [row.comment_id for row in notification_rows if row.comment_id]
    notification_task_ids = [row.task_id for row in notification_rows]
    notification_comments = {
        row.id: row
        for row in TaskComment.query.filter(TaskComment.id.in_(notification_comment_ids)).all()
    } if notification_comment_ids else {}
    notification_tasks = {
        row.id: row
        for row in Task.query.filter(Task.id.in_(notification_task_ids)).all()
    } if notification_task_ids else {}
    notification_author_ids = sorted({row.user_id for row in notification_comments.values() if row and row.user_id})
    notification_collaborator_ids = sorted({row.collaborator_id for row in notification_comments.values() if row and row.collaborator_id})
    notification_authors = {
        row.id: row
        for row in User.query.filter(User.id.in_(notification_author_ids)).all()
    } if notification_author_ids else {}
    notification_collaborators = {
        row.id: row
        for row in CollaboratorProfile.query.filter(CollaboratorProfile.id.in_(notification_collaborator_ids)).all()
    } if notification_collaborator_ids else {}
    unread_message_task_ids = {row.task_id for row in notification_rows if row.kind == "comment" and row.read_at is None and row.task_id}
    latest_read_by_task = {}
    unread_comment_count_by_task = {}
    if unread_message_task_ids:
        latest_read_rows = (
            db.session.query(TaskNotification.task_id, db.func.max(TaskNotification.read_at))
            .filter(
                TaskNotification.user_id == user.id,
                TaskNotification.kind == "comment",
                TaskNotification.task_id.in_(unread_message_task_ids),
                TaskNotification.read_at.isnot(None),
            )
            .group_by(TaskNotification.task_id)
            .all()
        )
        latest_read_by_task = {task_id: read_at for task_id, read_at in latest_read_rows if task_id}
        unread_comment_count_by_task = {task_id: 0 for task_id in unread_message_task_ids}
        for comment_row in TaskComment.query.filter(TaskComment.task_id.in_(unread_message_task_ids)).all():
            if comment_row.user_id and int(comment_row.user_id) == int(user.id):
                continue
            threshold = latest_read_by_task.get(comment_row.task_id)
            if threshold and comment_row.created_at <= threshold:
                continue
            unread_comment_count_by_task[comment_row.task_id] = unread_comment_count_by_task.get(comment_row.task_id, 0) + 1
    notification_items = _build_notification_items(notification_rows, user_id=user.id)
    task_notifications = [item for item in notification_items if item["kind"] != "comment"]
    message_notifications = [item for item in notification_items if item["kind"] == "comment"]
    inbox_notification_rows = (
        TaskNotification.query.filter(TaskNotification.user_id == user.id, TaskNotification.kind != "comment")
        .order_by(TaskNotification.created_at.desc(), TaskNotification.id.desc())
        .limit(100)
        .all()
    )
    discussion_rows = (
        UserDiscussionActivity.query.filter_by(user_id=user.id)
        .order_by(UserDiscussionActivity.updated_at.desc(), UserDiscussionActivity.id.desc())
        .limit(100)
        .all()
    )
    inbox_items = _build_notification_items(inbox_notification_rows, user_id=user.id) + build_discussion_activity_items(discussion_rows, user_id=user.id)
    inbox_items.sort(key=lambda item: item.get("created_at") or datetime.min, reverse=True)
    inbox_items_json = [{key: value for key, value in item.items() if key != "created_at"} for item in inbox_items]
    todo_groups_by_date = []
    for item in todo_items:
        if not todo_groups_by_date or todo_groups_by_date[-1]["key"] != item["date_key"]:
            todo_groups_by_date.append({"key": item["date_key"], "label": item["date_label"], "items": []})
        todo_groups_by_date[-1]["items"].append(item)
    return render_template(
        "dashboard.html",
        user=user,
        now_utc=now_utc,
        current_view=current_view,
        show_completed=show_completed,
        projects=projects,
        divisions=sidebar_divisions,
        division_options=division_options,
        selected_project_color=selected_project_color,
        projects_by_division=projects_by_division,
        top_level_items=top_level_items,
        unassigned_projects=unassigned_projects,
        tasks=tasks,
        selected_project=selected_project,
        assignments_by_task=assignments_by_task,
        group_assignment_members_by_task=group_assignment_members_by_task,
        info_by_task=info_by_task,
        project_info=project_info,
        group_info_by_id=group_info_by_id,
        groups_by_project=groups_by_project,
        user_map=user_map,
        groups=groups,
        tasks_by_group=tasks_by_group,
        ungrouped_tasks=ungrouped_tasks,
        is_owner=is_owner,
        can_manage_project=can_manage_project,
        manageable_group_ids=manageable_group_ids,
        member_project_ids=member_project_ids,
        project_viewers=project_viewers,
        group_viewers=group_viewers,
        direct_projects=direct_projects,
        direct_project_peers=direct_project_peers,
        project_display_names=project_display_names,
        shared_project_ids=shared_project_ids,
        shared_group_ids=shared_group_ids,
        shared_out_project_ids=shared_out_project_ids,
        shared_out_group_ids=shared_out_group_ids,
        selected_project_display_name=selected_project_display_name,
        tree_project_contexts=tree_project_contexts,
        tree_groups_by_project=tree_groups_by_project,
        todo_groups_by_date=todo_groups_by_date,
        todo_assignee_options=todo_assignee_options,
        github_task_meta=github_task_meta,
        github_project_id=github_project_id,
        github_auto_sync_needed=github_auto_sync_needed,
        comment_counts=comment_counts,
        unread_task_ids=unread_task_ids,
        task_status_payloads=task_status_payloads,
        task_notifications=task_notifications,
        message_notifications=message_notifications,
        inbox_items=inbox_items,
        inbox_items_json=inbox_items_json,
        dashboard_notice=dashboard_notice,
        tree_selection_type=tree_selection_type,
        tree_selection_id=tree_selection_id,
        project_hierarchy_meta=project_hierarchy_meta,
    )


@ui_bp.get("/follow/task/<int:task_id>")
@login_required
def follow_task(task_id: int):
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return redirect(url_for("ui.dashboard"))
    if not (
        Project.query.filter_by(id=task.project_id, owner_id=user.id).first()
        or ProjectMember.query.filter_by(project_id=task.project_id, user_id=user.id).first()
        or db.session.query(Group.id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(Group.project_id == task.project_id, GroupMember.user_id == user.id)
        .first()
    ):
        return redirect(url_for("ui.dashboard"))
    return redirect(
        url_for(
            "ui.dashboard",
            project_id=task.project_id,
            view="tree",
            show_completed=1,
            open_discussion_task_id=task.id,
            tree_select_type="group" if task.group_id else "project",
            tree_select_id=task.group_id if task.group_id else task.project_id,
        )
    )


@ui_bp.get("/follow/group/<int:group_id>")
@login_required
def follow_group(group_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return redirect(url_for("ui.dashboard"))
    if not (
        Project.query.filter_by(id=group.project_id, owner_id=user.id).first()
        or ProjectMember.query.filter_by(project_id=group.project_id, user_id=user.id).first()
        or db.session.query(Group.id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(Group.project_id == group.project_id, GroupMember.user_id == user.id)
        .first()
    ):
        return redirect(url_for("ui.dashboard"))
    return redirect(
        url_for(
            "ui.dashboard",
            project_id=group.project_id,
            view="tree",
            show_completed=1,
            open_discussion_group_id=group.id,
            tree_select_type="group",
            tree_select_id=group.id,
        )
    )


@ui_bp.get("/follow/project/<int:project_id>")
@login_required
def follow_project(project_id: int):
    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return redirect(url_for("ui.dashboard"))
    if not (
        Project.query.filter_by(id=project.id, owner_id=user.id).first()
        or ProjectMember.query.filter_by(project_id=project.id, user_id=user.id).first()
        or db.session.query(Group.id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(Group.project_id == project.id, GroupMember.user_id == user.id)
        .first()
    ):
        return redirect(url_for("ui.dashboard"))
    return redirect(
        url_for(
            "ui.dashboard",
            project_id=project.id,
            view="tree",
            show_completed=1,
            open_discussion_project_id=project.id,
            tree_select_type="project",
            tree_select_id=project.id,
        )
    )


@ui_bp.get("/dev/mail")
@login_required
def dev_mailbox():
    if not current_app.config.get("DEV_MAILBOX_ENABLED"):
        return redirect(url_for("ui.dashboard"))
    messages = DevMailboxMessage.query.order_by(DevMailboxMessage.created_at.desc(), DevMailboxMessage.id.desc()).all()
    return render_template("dev_mailbox.html", user=current_user(), messages=messages, title="Dev Mailbox")


@ui_bp.get("/account")
@login_required
def account():
    user = current_user()
    section = (request.args.get("section") or "emails").strip().lower()
    if section not in {"emails", "experience", "notifications"}:
        section = "emails"
    return _render_account_page(user, section=section)


@ui_bp.get("/account/notifications")
@login_required
def account_notifications():
    return _render_account_page(current_user(), section="notifications")


@ui_bp.post("/account/experience")
@login_required
def update_account_experience():
    user = current_user()
    theme_mode = (request.form.get("theme_mode") or user.theme_mode or "dark").strip().lower()
    if theme_mode not in {"dark", "light"}:
        return _render_account_page(user, section="experience", error="Invalid theme selection.")
    user.theme_mode = theme_mode
    timezone_value = normalize_user_timezone(request.form.get("timezone") or user.timezone or "UTC")
    user.timezone = timezone_value
    db.session.commit()
    return _render_account_page(user, section="experience", message="Experience settings updated.")


@ui_bp.post("/account/display-name")
@login_required
def update_account_display_name():
    user = current_user()
    display_name = (request.form.get("display_name") or "").strip()
    user.display_name = display_name or None
    db.session.commit()
    if user.display_name:
        message = "Display name updated."
    else:
        message = "Display name cleared. Default name will be used."
    return _render_account_page(user, section="emails", message=message)


@ui_bp.post("/account/notifications")
@login_required
def update_account_notifications():
    user = current_user()
    user.notification_email_frequency_seconds = normalize_notification_email_frequency(
        request.form.get("notification_email_frequency_seconds")
    )
    existing = {
        (row.event_key, row.scope_key or "default"): row
        for row in UserNotificationPreference.query.filter_by(user_id=user.id).all()
    }
    for definition in GENERAL_NOTIFICATION_DEFINITIONS:
        key = definition["key"]
        push_enabled = "1" in request.form.getlist(f"push__{key}")
        email_enabled = "1" in request.form.getlist(f"email__{key}")
        row = existing.get((key, "default"))
        if row is None:
            row = UserNotificationPreference(
                user_id=user.id,
                event_key=key,
                scope_key="default",
                push_enabled=push_enabled,
                email_enabled=email_enabled,
            )
            db.session.add(row)
        else:
            row.push_enabled = push_enabled
            row.email_enabled = email_enabled
        row.updated_at = datetime.utcnow()
    for definition in TASK_NOTIFICATION_DEFINITIONS:
        key = definition["key"]
        for scope in TASK_NOTIFICATION_SCOPES:
            scope_key = scope["key"]
            push_enabled = "1" in request.form.getlist(f"push__{key}__{scope_key}")
            email_enabled = "1" in request.form.getlist(f"email__{key}__{scope_key}")
            row = existing.get((key, scope_key))
            if row is None:
                row = UserNotificationPreference(
                    user_id=user.id,
                    event_key=key,
                    scope_key=scope_key,
                    push_enabled=push_enabled,
                    email_enabled=email_enabled,
                )
                db.session.add(row)
            else:
                row.push_enabled = push_enabled
                row.email_enabled = email_enabled
            row.updated_at = datetime.utcnow()
    db.session.commit()
    return _render_account_page(
        user,
        section="notifications",
        message="Notification preferences updated.",
    )


@ui_bp.post("/account/notifications/test-email")
@login_required
def send_account_notification_test_email():
    user = current_user()
    preference_map = user_notification_preference_map(user.id)
    items = build_test_notification_email_items(user, preference_map)
    if not user.email:
        return _render_account_page(user, section="notifications", error="No primary email is available for test delivery.")
    if not items:
        return _render_account_page(user, section="notifications", error="Enable at least one email notification type before sending a test email.")
    try:
        send_notification_digest_email(
            user.email,
            recipient_name=display_name_for_user(user) or user.email,
            notifications=items,
        )
    except MailDeliveryError as exc:
        return _render_account_page(user, section="notifications", error=str(exc))
    except Exception:
        current_app.logger.exception("notification test email failed", extra={"user_id": user.id})
        return _render_account_page(user, section="notifications", error="Unable to send the test email.")
    return _render_account_page(user, section="notifications", message="Test email sent.")


@ui_bp.get("/admin")
@admin_required
def admin_index():
    return redirect(url_for("ui.admin_users"))


@ui_bp.get("/calendar/feed/<token>.ics")
def calendar_feed(token: str):
    feed = CalendarFeed.query.filter_by(token=token).first()
    if not feed:
        return Response("Not found", status=404)
    ics = _build_calendar_feed_ics(feed.email, _calendar_feed_tasks(feed.email))
    response = Response(ics, mimetype="text/calendar")
    response.headers["Content-Disposition"] = 'inline; filename="termin-assigned-tasks.ics"'
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@ui_bp.get("/admin/users")
@admin_required
def admin_users():
    users = User.query.order_by(User.created_at.desc(), User.id.desc()).all()
    emails = UserEmail.query.order_by(UserEmail.user_id.asc(), UserEmail.is_primary.desc(), UserEmail.email.asc()).all()
    identities = ExternalIdentity.query.order_by(
        ExternalIdentity.user_id.asc(),
        ExternalIdentity.provider.asc(),
        ExternalIdentity.created_at.asc(),
    ).all()
    linked_user_emails = {email.email.strip().lower() for email in emails if email.email}
    collaborators = [
        collaborator
        for collaborator in CollaboratorProfile.query.order_by(
            CollaboratorProfile.updated_at.desc(), CollaboratorProfile.id.desc()
        ).all()
        if not collaborator.user_id
        and (collaborator.email or "").strip().lower() not in linked_user_emails
    ]

    emails_by_user: dict[int, list[UserEmail]] = {}
    for email in emails:
        emails_by_user.setdefault(email.user_id, []).append(email)

    identities_by_user: dict[int, list[ExternalIdentity]] = {}
    for identity in identities:
        identities_by_user.setdefault(identity.user_id, []).append(identity)

    return _render_admin_page(
        "admin_users.html",
        section="users",
        users=users,
        emails_by_user=emails_by_user,
        identities_by_user=identities_by_user,
        collaborators=collaborators,
    )


@ui_bp.get("/admin/deployment")
@admin_required
def admin_deployment():
    return _render_admin_page(
        "admin_deployment.html",
        section="deployment",
        deployment=_deployment_git_info(),
    )


def _collaborator_delete_summary(collaborator: CollaboratorProfile) -> dict:
    normalized_email = (collaborator.email or "").strip().lower()
    return {
        "assignments": Assignment.query.filter(
            Assignment.user_id.is_(None),
            db.func.lower(Assignment.email) == normalized_email,
        ).count(),
        "invites": Invite.query.filter(db.func.lower(Invite.email) == normalized_email).count(),
        "messages": DevMailboxMessage.query.filter(db.func.lower(DevMailboxMessage.to_email) == normalized_email).count(),
    }


@ui_bp.get("/admin/collaborators/<int:collaborator_id>")
@admin_required
def admin_collaborator(collaborator_id: int):
    collaborator = CollaboratorProfile.query.get_or_404(collaborator_id)
    entries = _build_collaborator_entries(collaborator)
    sent_messages = DevMailboxMessage.query.filter_by(to_email=collaborator.email).order_by(
        DevMailboxMessage.created_at.desc(),
        DevMailboxMessage.id.desc(),
    ).all()

    open_entries = [entry for entry in entries if entry["status"] in {"sent", "draft", "assigned", "link_sent"}]
    history_entries = [entry for entry in entries if entry["status"] in {"accepted", "declined", "denied"}]
    todo_entries = [entry for entry in entries if not entry["is_complete"]]
    delete_summary = _collaborator_delete_summary(collaborator)

    return _render_admin_page(
        "admin_collaborator.html",
        section="users",
        collaborator=collaborator,
        entries=entries,
        open_entries=open_entries,
        history_entries=history_entries,
        todo_entries=todo_entries,
        sent_messages=sent_messages,
        collaborator_portal_url=url_for("ui.collaborator_portal", token=collaborator.access_token),
        delete_summary=delete_summary,
    )


@ui_bp.post("/admin/collaborators/<int:collaborator_id>/delete")
@admin_required
def delete_admin_collaborator(collaborator_id: int):
    collaborator = CollaboratorProfile.query.get_or_404(collaborator_id)
    if collaborator.user_id or UserEmail.query.filter(db.func.lower(UserEmail.email) == collaborator.email.lower()).first():
        return redirect(url_for("ui.admin_collaborator", collaborator_id=collaborator.id))

    normalized_email = collaborator.email.lower()
    Invite.query.filter(db.func.lower(Invite.email) == normalized_email).delete(synchronize_session=False)
    Assignment.query.filter(
        Assignment.user_id.is_(None),
        db.func.lower(Assignment.email) == normalized_email,
    ).delete(synchronize_session=False)
    DevMailboxMessage.query.filter(db.func.lower(DevMailboxMessage.to_email) == normalized_email).delete(
        synchronize_session=False
    )
    db.session.delete(collaborator)
    db.session.commit()
    return redirect(url_for("ui.admin_users"))


@ui_bp.post("/account/emails")
@login_required
def add_account_email():
    user = current_user()
    try:
        verification = create_email_verification(user=user, email=request.form.get("email"), purpose="add_email")
        verify_url = url_for("ui.verify_account_email", token=verification.token, _external=True)
        send_account_verification_email(
            to_email=verification.email,
            verify_url=verify_url,
            purpose="add_email",
            inviter_email=user.email,
            inviter_name=user.display_name or user.email,
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _render_account_page(user, error=str(exc))
    except MailDeliveryError as exc:
        db.session.rollback()
        return _render_account_page(user, error=f"Could not send verification email: {exc}")
    return _render_account_page(user, message=f"Verification email sent to {verification.email}.")


@ui_bp.post("/account/emails/<int:email_id>/primary")
@login_required
def set_account_primary_email(email_id: int):
    user = current_user()
    try:
        set_primary_user_email(user, email_id)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _render_account_page(user, error=str(exc))
    return redirect(url_for("ui.account"))


@ui_bp.post("/account/emails/<int:email_id>/unlink")
@login_required
def unlink_account_email(email_id: int):
    user = current_user()
    try:
        remove_user_email(user, email_id)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _render_account_page(user, error=str(exc))
    return _render_account_page(user, message="Email unlinked.")


@ui_bp.post("/account/merge")
@login_required
def merge_account():
    user = current_user()
    email = request.form.get("email")
    source_user = find_user_by_email(email)

    if not source_user:
        return _render_account_page(user, error="No account was found for that email.")

    try:
        verification = create_email_verification(
            user=user,
            email=email,
            purpose="merge_account",
            source_user=source_user,
        )
        verify_url = url_for("ui.verify_account_email", token=verification.token, _external=True)
        send_account_verification_email(
            to_email=verification.email,
            verify_url=verify_url,
            purpose="merge_account",
            inviter_email=user.email,
            inviter_name=user.display_name or user.email,
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _render_account_page(user, error=str(exc))
    except MailDeliveryError as exc:
        db.session.rollback()
        return _render_account_page(user, error=f"Could not send merge verification email: {exc}")

    return _render_account_page(user, message=f"Merge verification email sent to {verification.email}.")


@ui_bp.post("/account/github/sync")
@login_required
def sync_github_account():
    user = current_user()
    try:
        result = sync_github_issues_for_user(user)
    except GitHubSyncError as exc:
        db.session.rollback()
        return _render_account_page(user, error=str(exc))

    message = (
        f"GitHub sync complete. {result['created']} created, {result['updated']} updated."
    )
    return _render_account_page(user, message=message)


@ui_bp.get("/account/verify/<token>")
def verify_account_email(token: str):
    verification = consume_email_verification(token)
    if not verification:
        return render_template(
            "account_verify.html",
            status="invalid",
            message="This verification link is invalid or already used.",
            continue_url=url_for("auth.login"),
        )

    user = User.query.get(verification.user_id)
    if not user:
        db.session.rollback()
        return render_template(
            "account_verify.html",
            status="invalid",
            message="The target account no longer exists.",
            continue_url=url_for("auth.login"),
        )

    try:
        if verification.purpose == "add_email":
            add_user_email(user, verification.email)
        elif verification.purpose == "merge_account":
            source_user = User.query.get(verification.source_user_id) if verification.source_user_id else find_user_by_email(verification.email)
            if not source_user:
                raise ValueError("The source account no longer exists.")
            merge_users(target_user=user, source_user=source_user)
        else:
            raise ValueError("Unsupported verification type.")
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return render_template(
            "account_verify.html",
            status="error",
            message=str(exc),
            continue_url=url_for("auth.login"),
        )

    return render_template(
        "account_verify.html",
        status="ok",
        message="Email verification complete." if verification.purpose == "add_email" else "Account merge complete.",
        continue_url=url_for("ui.account"),
    )


@ui_bp.post("/dev/mail/clear")
@login_required
def clear_dev_mailbox():
    if not current_app.config.get("DEV_MAILBOX_ENABLED"):
        return redirect(url_for("ui.dashboard"))
    DevMailboxMessage.query.delete()
    db.session.commit()
    return redirect(url_for("ui.dev_mailbox"))


@ui_bp.post("/projects")
@login_required
def create_project():
    user = current_user()
    name = (request.form.get("name") or "").strip()
    division_id = (request.form.get("division_id") or "").strip()
    insert_position_raw = (request.form.get("insert_position") or "").strip()
    default_owner_calendar_opt_in = request.form.get("default_owner_calendar_opt_in") == "on"
    default_invitee_calendar_opt_in = request.form.get("default_invitee_calendar_opt_in") == "on"
    if not name:
        return redirect(url_for("ui.dashboard"))

    division = None
    if division_id:
        try:
            division = Division.query.filter_by(id=int(division_id), owner_id=user.id).first()
        except ValueError:
            division = None
    try:
        insert_position = int(insert_position_raw) if insert_position_raw else None
    except ValueError:
        insert_position = None
    project_position = insert_project_position(user.id, division.id if division else None, insert_position)

    project = Project(
        name=name,
        owner_id=user.id,
        division_id=None,
        position=0,
        default_owner_calendar_opt_in=default_owner_calendar_opt_in,
        default_invitee_calendar_opt_in=default_invitee_calendar_opt_in,
    )
    db.session.add(project)
    db.session.flush()
    db.session.add(
        ProjectSidebarPreference(
            user_id=user.id,
            project_id=project.id,
            division_id=division.id if division else None,
            position=project_position,
        )
    )
    db.session.commit()
    emit_project_created(project, actor_user_id=user.id, recipient_user_id=user.id)
    emit_sidebar_reordered(user.id, _sidebar_order_tokens(user), actor_user_id=user.id)
    return redirect(url_for("ui.dashboard", project_id=project.id))


@ui_bp.post("/divisions")
@login_required
def create_division():
    user = current_user()
    name = (request.form.get("name") or "").strip()
    insert_position_raw = (request.form.get("insert_position") or "").strip()
    if not name:
        return redirect(url_for("ui.dashboard"))
    try:
        insert_position = int(insert_position_raw) if insert_position_raw else None
    except ValueError:
        insert_position = None

    division_position = insert_top_level(user.id, insert_position)
    palette = _division_palette()
    division = Division(
        owner_id=user.id,
        name=name,
        color=palette[(division_position - 1) % len(palette)],
        position=division_position,
    )
    db.session.add(division)
    db.session.commit()
    emit_division_created(division, actor_user_id=user.id, recipient_user_id=user.id)
    emit_sidebar_reordered(user.id, _sidebar_order_tokens(user), actor_user_id=user.id)
    return redirect(url_for("ui.dashboard"))


@ui_bp.post("/groups")
@login_required
def create_group():
    user = current_user()
    name = (request.form.get("name") or "").strip()
    project_id = request.form.get("project_id")
    if not name or not project_id:
        return redirect(url_for("ui.dashboard", project_id=project_id))
    try:
        project_id_int = int(project_id)
    except ValueError:
        return redirect(url_for("ui.dashboard"))

    project = Project.query.filter_by(id=project_id_int).first()
    if not project:
        return redirect(url_for("ui.dashboard"))
    if project.owner_id != user.id and not ProjectMember.query.filter_by(project_id=project.id, user_id=user.id).first():
        return redirect(url_for("ui.dashboard"))

    max_pos = db.session.query(db.func.max(Group.position)).filter_by(project_id=project.id).scalar() or 0
    palette = _division_palette()
    group = Group(
        project_id=project.id,
        name=name,
        position=max_pos + 1,
        color=palette[(max_pos) % len(palette)],
    )
    db.session.add(group)
    db.session.commit()
    emit_group_created(group, actor_user_id=user.id)
    return redirect(url_for("ui.dashboard", project_id=project.id))


@ui_bp.post("/tasks")
@login_required
def create_task():
    user = current_user()
    title = (request.form.get("title") or "").strip()
    project_id = request.form.get("project_id")
    due_raw = (request.form.get("due_at") or "").strip()
    owner_calendar_opt_in = request.form.get("owner_calendar_opt_in") == "on"
    use_project_defaults = request.form.get("use_project_defaults") == "on"

    if not title or not project_id:
        return redirect(url_for("ui.dashboard", project_id=project_id))

    project = Project.query.filter_by(id=int(project_id)).first()
    if not project:
        return redirect(url_for("ui.dashboard", project_id=project_id))
    if project.owner_id != user.id and not ProjectMember.query.filter_by(project_id=project.id, user_id=user.id).first():
        return redirect(url_for("ui.dashboard", project_id=project_id))

    due_at = None
    if due_raw:
        try:
            due_at = datetime.fromisoformat(due_raw)
        except ValueError:
            due_at = None

    if use_project_defaults:
        owner_calendar_opt_in = project.default_owner_calendar_opt_in

    group = Group.query.filter_by(project_id=project.id).first()
    task = Task(
        project_id=int(project_id),
        group_id=group.id if group else None,
        creator_user_id=user.id,
        position=(db.session.query(db.func.max(Task.position)).filter_by(project_id=project.id, group_id=group.id if group else None).scalar() or 0) + 1,
        title=title,
        info=normalize_info_payload(None),
        due_at=due_at,
        owner_calendar_opt_in=owner_calendar_opt_in,
    )
    db.session.add(task)
    db.session.commit()

    if owner_calendar_opt_in:
        _safe_ensure_task_event(task.id)
    return redirect(url_for("ui.dashboard", project_id=project.id))


@ui_bp.post("/tasks/<int:task_id>/invite")
@login_required
def invite_to_task(task_id: int):
    email = (request.form.get("email") or "").strip().lower()
    if not email:
        return redirect(url_for("ui.dashboard"))

    task = Task.query.get(task_id)
    project = Project.query.get(task.project_id) if task else None
    collaborator = get_or_create_collaborator_profile(
        email=email,
        default_calendar_opt_in=project.default_invitee_calendar_opt_in if project else False,
    )

    token = secrets.token_hex(24)
    invite = Invite(
        task_id=task_id,
        email=email,
        token=token,
        calendar_opt_in=collaborator.default_calendar_opt_in,
    )
    db.session.add(invite)
    db.session.commit()

    return redirect(url_for("ui.dashboard"))


@ui_bp.get("/invites/<token>")
def accept_invite(token: str):
    invite = Invite.query.filter_by(token=token).first()
    if not invite:
        return render_template("invite.html", status="invalid")

    task = Task.query.get(invite.task_id) if invite.task_id else None
    return render_template(
        "invite.html",
        status=invite.status,
        invite=invite,
        task=task,
        collaborator=CollaboratorProfile.query.filter_by(email=invite.email).first(),
    )


@ui_bp.post("/invites/<token>/respond")
def respond_invite(token: str):
    invite = Invite.query.filter_by(token=token).first()
    if not invite:
        return render_template("invite.html", status="invalid")

    action = request.form.get("action")
    calendar_opt_in = request.form.get("calendar_opt_in") == "on"
    _apply_invite_response(invite, action, calendar_opt_in)

    db.session.commit()
    task = Task.query.get(invite.task_id) if invite.task_id else None
    if invite.status == "accepted" and invite.calendar_opt_in and task:
        _safe_ensure_task_event(task.id, invite.email)
    if invite.assignment_id and task:
        assignment = Assignment.query.get(invite.assignment_id)
        if assignment:
            emit_assignment_updated(task, assignment)
    return render_template(
        "invite.html",
        status=invite.status,
        invite=invite,
        task=task,
        collaborator=CollaboratorProfile.query.filter_by(email=invite.email).first(),
    )


@ui_bp.route("/invites/<token>/quick/<action>", methods=["GET", "POST"])
def quick_respond_invite(token: str, action: str):
    invite = Invite.query.filter_by(token=token).first()
    if not invite:
        return render_template("invite.html", status="invalid")
    if action not in {"accept", "decline"}:
        return render_template("invite.html", status="invalid")
    if request.method == "GET":
        task = Task.query.get(invite.task_id) if invite.task_id else None
        return render_template(
            "quick_action_confirm.html",
            title="Confirm response",
            subtitle="Email links require confirmation before they change task status.",
            summary=task.title if task else "Task",
            detail=None,
            confirm_label="Accept" if action == "accept" else "Decline",
            confirm_tone="primary" if action == "accept" else "secondary",
            cancel_href=url_for("ui.view_invite", token=token),
        )

    _apply_invite_response(invite, action, False)
    db.session.commit()

    task = Task.query.get(invite.task_id) if invite.task_id else None
    collaborator = CollaboratorProfile.query.filter_by(email=invite.email).first()
    if collaborator:
        return redirect(url_for("ui.collaborator_portal", token=collaborator.access_token))
    return render_template(
        "invite.html",
        status=invite.status,
        invite=invite,
        task=task,
        collaborator=None,
    )


@ui_bp.get("/collaborators/<token>")
def collaborator_portal(token: str):
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return render_template("collaborator_portal.html", status="invalid", collaborator=None, entries=[])
    entries = _build_collaborator_entries(collaborator)
    task_ids = sorted({entry["task"].id for entry in entries if entry.get("task")})
    task_comment_counts = {
        task_id: count
        for task_id, count in (
            db.session.query(TaskComment.task_id, db.func.count(TaskComment.id))
            .filter(TaskComment.task_id.in_(task_ids))
            .group_by(TaskComment.task_id)
            .all()
        )
    } if task_ids else {}
    latest_comment_rows = (
        db.session.query(TaskComment.task_id, db.func.max(TaskComment.created_at))
        .filter(TaskComment.task_id.in_(task_ids))
        .group_by(TaskComment.task_id)
        .all()
        if task_ids
        else []
    )
    read_rows = {
        row.task_id: row.last_read_at
        for row in CollaboratorTaskRead.query.filter(
            CollaboratorTaskRead.collaborator_id == collaborator.id,
            CollaboratorTaskRead.task_id.in_(task_ids),
        ).all()
    } if task_ids else {}
    unread_task_ids = {
        task_id
        for task_id, latest_comment_at in latest_comment_rows
        if latest_comment_at and latest_comment_at > (read_rows.get(task_id) or datetime.min)
    }
    calendar_urls = calendar_subscription_urls_for_email(collaborator.email)

    return render_template(
        "collaborator_portal.html",
        status="ok",
        collaborator=collaborator,
        entries=entries,
        task_comment_counts=task_comment_counts,
        unread_task_ids=unread_task_ids,
        calendar_subscription_url=calendar_urls["https"],
        calendar_subscription_webcal_url=calendar_urls["webcal"],
    )


@ui_bp.route("/collaborators/<token>/invites/<int:invite_id>/quick/<action>", methods=["GET", "POST"])
def quick_collaborator_invite_action(token: str, invite_id: int, action: str):
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return render_template("collaborator_portal.html", status="invalid", collaborator=None, entries=[])

    invite = Invite.query.filter_by(id=invite_id, email=collaborator.email).first()
    if not invite:
        return redirect(url_for("ui.collaborator_portal", token=token))

    task = _resolve_work_item(invite.task_id)
    if request.method == "GET":
        return render_template(
            "quick_action_confirm.html",
            title="Confirm action",
            subtitle="Email links require confirmation before they change task status.",
            summary=task.title if task else "Task",
            detail=None,
            confirm_label="Mark complete" if action == "complete" else ("Accept" if action == "accept" else "Decline"),
            confirm_tone="primary" if action in {"accept", "complete"} else "secondary",
            cancel_href=url_for("ui.collaborator_portal", token=token),
        )
    if action in {"accept", "decline"}:
        _apply_invite_response(invite, action, invite.calendar_opt_in)
    elif action == "complete":
        _set_collaborator_work_item_status(task, collaborator.email, "complete")
    elif action == "uncomplete":
        _set_collaborator_work_item_status(task, collaborator.email, "open")
    else:
        return redirect(url_for("ui.collaborator_portal", token=token))

    db.session.commit()

    if action == "accept" and invite.calendar_opt_in and task:
        _safe_ensure_task_event(task.id, invite.email)
    if task and action in {"complete", "uncomplete"}:
        emit_task_updated(task)
    return redirect(url_for("ui.collaborator_portal", token=token))


@ui_bp.route("/collaborators/<token>/quick/<action>", methods=["GET", "POST"])
def quick_collaborator_bulk_action(token: str, action: str):
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return render_template("collaborator_portal.html", status="invalid", collaborator=None, entries=[])
    if action not in {"accept", "decline"}:
        return redirect(url_for("ui.collaborator_portal", token=token))

    raw_ids = (request.args.get("invites") or "").strip()
    invite_ids = []
    for item in raw_ids.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            invite_ids.append(int(item))
        except ValueError:
            continue
    if not invite_ids:
        return redirect(url_for("ui.collaborator_portal", token=token))
    if request.method == "GET":
        return render_template(
            "quick_action_confirm.html",
            title="Confirm bulk action",
            subtitle="Email links require confirmation before they change task status.",
            summary=f"{len(invite_ids)} invite{'s' if len(invite_ids) != 1 else ''}",
            detail="Apply this action to the linked tasks in your collaborator portal.",
            confirm_label="Accept all" if action == "accept" else "Decline all",
            confirm_tone="primary" if action == "accept" else "secondary",
            cancel_href=url_for("ui.collaborator_portal", token=token),
        )

    invites = Invite.query.filter(
        Invite.id.in_(invite_ids),
        Invite.email == collaborator.email,
    ).all()
    for invite in invites:
        _apply_invite_response(invite, action, False)
    db.session.commit()
    return redirect(url_for("ui.collaborator_portal", token=token))


@ui_bp.get("/collaborators/<token>/convert")
def collaborator_convert(token: str):
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return render_template("collaborator_claim.html", status="invalid", collaborator=None)
    return render_template("collaborator_claim.html", status="ok", collaborator=collaborator)


@ui_bp.post("/collaborators/<token>/claim/password")
def claim_collaborator_password(token: str):
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return render_template("collaborator_portal.html", status="invalid", collaborator=None, entries=[])

    password = request.form.get("password") or ""
    password_confirm = request.form.get("password_confirm") or ""
    if len(password) < 8:
        return render_template(
            "collaborator_portal.html",
            status="ok",
            collaborator=collaborator,
            entries=_build_collaborator_entries(collaborator),
            claim_error="Password must be at least 8 characters.",
        )
    if password != password_confirm:
        return render_template(
            "collaborator_portal.html",
            status="ok",
            collaborator=collaborator,
            entries=_build_collaborator_entries(collaborator),
            claim_error="Passwords do not match.",
        )

    user = claim_collaborator_profile(collaborator)
    user.password_hash = generate_password_hash(password)
    db.session.commit()
    from flask import session
    session["user_id"] = user.id
    return redirect(url_for("ui.dashboard"))


@ui_bp.get("/collaborators/<token>/claim/<provider>")
def claim_collaborator_provider(token: str, provider: str):
    if provider not in {"google", "github", "microsoft"}:
        return redirect(url_for("ui.collaborator_portal", token=token))
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return render_template("collaborator_portal.html", status="invalid", collaborator=None, entries=[])
    from flask import session
    session["collaborator_claim_token"] = token
    return redirect(url_for(f"auth.login_{provider}"))


@ui_bp.post("/collaborators/<token>/settings")
def update_collaborator_settings(token: str):
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return render_template("collaborator_portal.html", status="invalid", collaborator=None, entries=[])

    collaborator.display_name = (request.form.get("display_name") or "").strip() or None
    notification_preference = (request.form.get("notification_preference") or "email").strip().lower()
    if notification_preference not in {"email", "calendar", "none"}:
        notification_preference = "email"
    follow_up_updates = request.form.get("follow_up_updates") == "on"
    collaborator.notification_preference = notification_preference
    collaborator.new_task_notification_preference = notification_preference
    collaborator.update_notification_preference = notification_preference if follow_up_updates else "none"
    collaborator.default_calendar_opt_in = notification_preference == "calendar"
    collaborator.updated_at = datetime.utcnow()
    db.session.commit()
    return redirect(url_for("ui.collaborator_portal", token=token))


@ui_bp.post("/collaborators/<token>/invites/<int:invite_id>")
def update_collaborator_invite(token: str, invite_id: int):
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return render_template("collaborator_portal.html", status="invalid", collaborator=None, entries=[])

    invite = Invite.query.filter_by(id=invite_id, email=collaborator.email).first()
    if not invite:
        return redirect(url_for("ui.collaborator_portal", token=token))

    action = request.form.get("action")
    calendar_opt_in = request.form.get("calendar_opt_in") == "on"
    task = _resolve_work_item(invite.task_id)
    project = Project.query.get(task.project_id) if task and task.project_id else None
    if action in {"accept", "decline"}:
        _apply_invite_response(invite, action, calendar_opt_in)
    elif action == "calendar":
        invite.calendar_opt_in = True
    elif action == "complete":
        _set_collaborator_work_item_status(task, collaborator.email, "complete")
    elif action == "uncomplete":
        _set_collaborator_work_item_status(task, collaborator.email, "open")

    sent_calendar_invite = False
    if action == "calendar":
        sent_calendar_invite = _send_collaborator_calendar_invite(collaborator, invite, task, project)
    elif invite.status == "accepted" and invite.calendar_opt_in and not invite.calendar_invite_sent_at:
        sent_calendar_invite = _send_collaborator_calendar_invite(collaborator, invite, task, project)

    db.session.commit()

    if task and (sent_calendar_invite or (invite.status == "accepted" and invite.calendar_opt_in)):
        _safe_ensure_task_event(task.id, invite.email)
    if task and action in {"complete", "uncomplete"}:
        emit_task_updated(task)
    if invite.assignment_id and task:
        assignment = Assignment.query.get(invite.assignment_id)
        if assignment:
            emit_assignment_updated(task, assignment)
    return redirect(url_for("ui.collaborator_portal", token=token))


@ui_bp.post("/collaborators/<token>/assignments/<int:assignment_id>")
def update_collaborator_assignment(token: str, assignment_id: int):
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return render_template("collaborator_portal.html", status="invalid", collaborator=None, entries=[])

    assignment = Assignment.query.filter_by(id=assignment_id, email=collaborator.email).first()
    if not assignment:
        return redirect(url_for("ui.collaborator_portal", token=token))

    calendar_opt_in = request.form.get("calendar_opt_in") == "on"
    invite = _ensure_assignment_invite(assignment, calendar_opt_in=calendar_opt_in)
    action = request.form.get("action")
    task = _resolve_work_item(assignment.task_id)
    project = Project.query.get(task.project_id) if task and task.project_id else None
    if action in {"accept", "decline"}:
        _apply_invite_response(invite, action, calendar_opt_in)
    elif action == "calendar":
        invite.calendar_opt_in = True
    elif action == "complete":
        _set_collaborator_work_item_status(task, collaborator.email, "complete")
    elif action == "uncomplete":
        _set_collaborator_work_item_status(task, collaborator.email, "open")

    sent_calendar_invite = False
    if action == "calendar":
        sent_calendar_invite = _send_collaborator_calendar_invite(collaborator, invite, task, project)
    elif invite.status == "accepted" and invite.calendar_opt_in and not invite.calendar_invite_sent_at:
        sent_calendar_invite = _send_collaborator_calendar_invite(collaborator, invite, task, project)

    db.session.commit()

    if task and (sent_calendar_invite or (invite.status == "accepted" and invite.calendar_opt_in)):
        _safe_ensure_task_event(task.id, invite.email)
    if task and action in {"complete", "uncomplete"}:
        emit_task_updated(task)
    if task:
        emit_assignment_updated(task, assignment)
    return redirect(url_for("ui.collaborator_portal", token=token))


@ui_bp.get("/collaborators/<token>/tasks/<int:task_id>/comments")
def collaborator_task_comments(token: str, task_id: int):
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return {"error": "invalid collaborator"}, 404
    if task_id not in _collaborator_task_ids(collaborator):
        return {"error": "unauthorized"}, 403
    comments = TaskComment.query.filter_by(task_id=task_id).order_by(TaskComment.created_at.asc(), TaskComment.id.asc()).all()
    user_ids = {comment.user_id for comment in comments if comment.user_id}
    collaborator_ids = {comment.collaborator_id for comment in comments if comment.collaborator_id}
    users = {row.id: row for row in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
    collaborators = {row.id: row for row in CollaboratorProfile.query.filter(CollaboratorProfile.id.in_(collaborator_ids)).all()} if collaborator_ids else {}
    return {"comments": [_serialize_portal_comment(comment, users, collaborators) for comment in comments]}


@ui_bp.post("/collaborators/<token>/tasks/<int:task_id>/comments")
def collaborator_post_task_comment(token: str, task_id: int):
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return {"error": "invalid collaborator"}, 404
    if task_id not in _collaborator_task_ids(collaborator):
        return {"error": "unauthorized"}, 403
    payload = request.get_json(silent=True) or {}
    body = (payload.get("body") or "").strip()
    if not body:
        return {"error": "body is required"}, 400
    comment = TaskComment(task_id=task_id, collaborator_id=collaborator.id, body=body)
    db.session.add(comment)
    db.session.flush()
    task = Task.query.get(task_id)
    activity_rows = []
    if task:
        activity_rows = upsert_discussion_activity(
            user_ids=task_discussion_user_ids(task),
            entity_type="task",
            entity_id=task.id,
            task_id=task.id,
            project_id=task.project_id,
            group_id=task.group_id,
            comment_id=comment.id,
            actor_collaborator_id=collaborator.id,
            preview=body,
        )
        queue_task_notifications(task, kind="comment", comment_id=comment.id)
    db.session.commit()
    if task:
        emit_task_comment_created(task_id, _serialize_portal_comment(comment, {}, {collaborator.id: collaborator}))
        _emit_collaborator_comment_previews(task, collaborator, body)
        for row in activity_rows:
            emit_discussion_activity_updated(row.user_id, row.id)
        emit_task_notification_updates(task)
    return {"comment": _serialize_portal_comment(comment, {}, {collaborator.id: collaborator})}, 201


@ui_bp.post("/collaborators/<token>/tasks/<int:task_id>/comments/read")
def collaborator_mark_task_comments_read(token: str, task_id: int):
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return {"error": "invalid collaborator"}, 404
    if task_id not in _collaborator_task_ids(collaborator):
        return {"error": "unauthorized"}, 403
    row = CollaboratorTaskRead.query.filter_by(collaborator_id=collaborator.id, task_id=task_id).first()
    if not row:
        row = CollaboratorTaskRead(collaborator_id=collaborator.id, task_id=task_id, last_read_at=datetime.utcnow())
        db.session.add(row)
    else:
        row.last_read_at = datetime.utcnow()
    db.session.commit()
    return {"status": "ok"}
