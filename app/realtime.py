from datetime import datetime
from collections import defaultdict
from html import escape
import json
import re
from markdown import markdown as render_markdown
from sqlalchemy import func, or_
from sqlalchemy.exc import OperationalError

from flask import current_app, request
from flask_socketio import emit, join_room, leave_room

from app.extensions import db, socketio

from app.group_assignments import serialize_group_assignment_members
from app.info_utils import load_info_payload, sanitize_info_html
from app.discussion_activity import build_discussion_activity_items
from app.debug_tools import debug_print
from app.notification_emailer import deliver_due_notification_emails_for_user
from app.notification_preferences import notification_event_key_for_kind, user_notification_channel_enabled
from app.models import (
    Assignment,
    CollaboratorProfile,
    Group,
    GroupComment,
    GroupMember,
    Invite,
    Project,
    ProjectComment,
    ProjectMember,
    ProjectSidebarPreference,
    Task,
    TaskComment,
    TaskFollower,
    TaskPrerequisite,
    TaskNotification,
    UserDiscussionActivity,
    User,
)
from app.themes import DEFAULT_THEME_NAME, division_effective_color, normalize_theme_name
from app.task_status import normalize_task_status_mode, task_status_meta, task_status_percentage
from app.utils import current_user, display_name_for_user
from app.web_push import send_web_push_notification

_handlers_registered = False
_sid_user = {}
_sid_collaborator = {}
_sid_projects = defaultdict(set)
_sid_tasks = defaultdict(set)
_sid_discussions = defaultdict(set)
_project_user_counts = defaultdict(lambda: defaultdict(int))
_task_user_counts = defaultdict(lambda: defaultdict(int))
_discussion_user_counts = defaultdict(lambda: defaultdict(int))
_user_socket_counts = defaultdict(int)
_user_active_projects = defaultdict(set)


def _debug_socket_summary(payload) -> dict | str:
    if not isinstance(payload, dict):
        return str(payload)
    summary: dict[str, object] = {}
    for key in ("action", "actor_user_id", "project_id", "group_id", "task_id", "notification_id", "kind"):
        if payload.get(key) is not None:
            summary[key] = payload.get(key)
    project_ids = payload.get("project_ids")
    if isinstance(project_ids, list):
        summary["project_count"] = len(project_ids)
    projects = payload.get("projects")
    if isinstance(projects, list):
        summary["project_count"] = len(projects)
    task = payload.get("task")
    if isinstance(task, dict):
        summary["task"] = {
            "id": task.get("id"),
            "project_id": task.get("project_id"),
            "group_id": task.get("group_id"),
            "status": task.get("status"),
            "status_mode": task.get("status_mode"),
            "assignment_count": len(task.get("assignments") or []),
        }
    assignment = payload.get("assignment")
    if isinstance(assignment, dict):
        summary["assignment"] = {
            "id": assignment.get("id"),
            "task_id": assignment.get("task_id"),
            "user_id": assignment.get("user_id"),
            "email": assignment.get("email"),
            "status": assignment.get("status"),
        }
    notifications = payload.get("notifications")
    if isinstance(notifications, list):
        summary["notification_count"] = len(notifications)
    return summary


def _debug_socket_emit(event: str, payload, *, room: str | None = None, sid: str | None = None) -> None:
    debug_print("socket-send", event, sid or room or "", _debug_socket_summary(payload))


def _debug_socket_recv(event: str, payload) -> None:
    debug_print("socket-recv", event, request.sid, _debug_socket_summary(payload or {}))


def user_room(user_id: int) -> str:
    return f"user:{user_id}"


def collaborator_room(collaborator_id: int) -> str:
    return f"collaborator:{collaborator_id}"


def task_room(task_id: int) -> str:
    return f"task:{task_id}"


def project_room(project_id: int) -> str:
    return f"project:{project_id}"


def discussion_room(entity_type: str, entity_id: int) -> str:
    return f"discussion:{entity_type}:{entity_id}"


def discussion_key(entity_type: str, entity_id: int) -> str:
    return f"{entity_type}:{entity_id}"


def _task_due_mode(task: Task | None) -> str:
    if not task:
        return "none"
    info_payload = load_info_payload(getattr(task, "info", None), getattr(task, "link", None))
    mode = str(info_payload.get("meta", {}).get("due_mode") or "").strip().lower()
    if mode in {"asap", "date"}:
        return mode
    if task.due_at:
        return "date"
    return "none"


def _task_start_date(task: Task | None) -> str:
    if not task:
        return ""
    info_payload = load_info_payload(getattr(task, "info", None), getattr(task, "link", None))
    return str((info_payload.get("meta") or {}).get("start_date") or "").strip()


def _load_notification_data(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _dump_notification_data(payload: dict | None) -> str | None:
    if not payload:
        return None
    return json.dumps(payload, sort_keys=True)


def _absolute_url(path: str) -> str:
    base = (current_app.config.get("PUBLIC_BASE_URL") or "").rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return (base + path) if base else path


def _mark_project_notifications_read(user_id: int, project_id: int) -> None:
    now = datetime.utcnow()
    task_ids = db.session.query(Task.id).filter(Task.project_id == project_id)
    try:
        (
            TaskNotification.query.filter_by(user_id=user_id, pinned=False)
            .filter(TaskNotification.kind != "comment")
            .filter(TaskNotification.task_id.in_(task_ids))
            .filter(TaskNotification.read_at.is_(None))
            .update({"read_at": now}, synchronize_session=False)
        )
        db.session.commit()
    except OperationalError:
        db.session.rollback()
        current_app.logger.warning(
            "Skipping project notification read update due to SQLite lock",
            extra={"user_id": user_id, "project_id": project_id},
        )


def _user_can_join_project(user_id: int, project_id: int) -> bool:
    return bool(
        Project.query.filter_by(id=project_id, owner_id=user_id).first()
        or ProjectMember.query.filter_by(project_id=project_id, user_id=user_id).first()
        or db.session.query(Group.id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(Group.project_id == project_id, GroupMember.user_id == user_id)
        .first()
    )


def _join_project_for_sid(
    *,
    sid: str,
    user_id: int,
    project_id: int,
    passive: bool,
) -> tuple[bool, bool]:
    project = Project.query.get(project_id)
    if not project:
        return False, False
    if not _user_can_join_project(user_id, project_id):
        return False, False
    join_room(project_room(project_id))
    is_new_join = project_id not in _sid_projects[sid]
    if is_new_join:
        _sid_projects[sid].add(project_id)
        _project_user_counts[project_id][user_id] += 1
    return True, is_new_join


def _web_push_payload_for_notification(
    *,
    kind: str,
    task: Task | None = None,
    project: Project | None = None,
    group: Group | None = None,
    detail_payload: dict | None = None,
) -> dict:
    detail = detail_payload or {}
    actor_name = str(detail.get("actor_name") or detail.get("sender_name") or "Termin").strip() or "Termin"
    task_title = (getattr(task, "title", None) or "a task").strip()
    project_id = getattr(project, "id", None) or detail.get("project_id")
    group_id = getattr(group, "id", None) or detail.get("group_id")
    group_project_id = getattr(group, "project_id", None) or detail.get("project_id")
    project_name = (getattr(project, "name", None) or detail.get("project_name") or "").strip()
    group_name = (getattr(group, "name", None) or detail.get("group_name") or "").strip()
    title = "Termin"
    body = "You have a new update."
    url = "/"
    tag = f"termin:{kind}"

    if task is not None:
        url = f"/task/{task.id}"
        tag = f"termin:{kind}:task:{task.id}"
        if kind == "comment":
            title = f"{actor_name} commented"
            body = f'On "{task_title}"'
        elif kind == "task_completed":
            title = "Task completed"
            body = f'{actor_name} completed "{task_title}"'
        elif kind == "task_due_changed":
            title = "Due date changed"
            new_due = str(detail.get("new_due_label") or detail.get("new_due_at") or "").strip()
            if actor_name and new_due:
                body = actor_name + " changed " + task_title + " due date to " + new_due
            elif actor_name:
                body = actor_name + " changed " + task_title + " due date"
            else:
                body = ("Due date changed to " + new_due) if new_due else "Due date changed"
        elif kind == "task_due_cleared":
            title = "Due date removed"
            body = (actor_name + " removed the due date from " + task_title) if actor_name else "Due date removed"
        elif kind == "task_due_asap":
            title = "Marked ASAP"
            body = (actor_name + " marked " + task_title + " ASAP") if actor_name else "Marked ASAP"
        elif kind == "task_status_changed":
            title = "Status changed"
            new_status = str(detail.get("new_status") or "").strip()
            if actor_name and new_status:
                body = actor_name + " changed " + task_title + " status to " + new_status
            elif actor_name:
                body = actor_name + " changed " + task_title + " status"
            else:
                body = ("Status changed to " + new_status) if new_status else "Status changed"
        elif kind == "task_renamed":
            title = "Task renamed"
            new_title = str(detail.get("new_title") or "").strip()
            if actor_name and new_title:
                body = actor_name + " renamed this task to " + new_title
            elif actor_name:
                body = actor_name + " renamed " + task_title
            else:
                body = ("Renamed to " + new_title) if new_title else "Task renamed"
        elif kind == "assignment_added":
            title = "Assigned to task"
            if detail.get("group_assign"):
                project_label = str(detail.get("project_name") or task_title or "Task").strip()
                other_count = max(int(detail.get("group_assign_other_count") or 0), 0)
                if actor_name:
                    body = (
                        actor_name + " assigned " + project_label + " to you and " + str(other_count) + " other(s)"
                        if other_count
                        else actor_name + " assigned " + project_label + " to you"
                    )
                else:
                    body = (
                        project_label + " was assigned to you and " + str(other_count) + " other(s)"
                        if other_count
                        else project_label + " was assigned to you"
                    )
            else:
                recipient_phrase = str(detail.get("recipient_assignment_phrase") or "").strip()
                assignee = str(detail.get("assignee_label") or "").strip()
                if actor_name and recipient_phrase:
                    body = actor_name + " assigned " + task_title + " to " + recipient_phrase
                elif actor_name and assignee:
                    body = actor_name + " assigned " + task_title + " to " + assignee
                elif actor_name:
                    body = actor_name + " assigned " + task_title
                elif recipient_phrase:
                    body = task_title + " was assigned to " + recipient_phrase
                else:
                    body = ("Assigned to " + assignee) if assignee else "Assigned to task"
        elif kind == "task_moved":
            title = "Task moved"
            body = (actor_name + " moved " + task_title) if actor_name else "Task moved"
        elif kind == "task_created":
            title = "Task created"
            body = f'{actor_name} created "{task_title}"'
        else:
            title = "Task updated"
            changed_fields = [str(value).strip() for value in (detail.get("changed_fields") or []) if str(value).strip()]
            if actor_name and changed_fields:
                body = actor_name + " updated " + task_title + " (" + ", ".join(changed_fields) + ")"
            elif changed_fields:
                body = "Updated " + ", ".join(changed_fields)
            else:
                body = f'{actor_name} updated "{task_title}"'
    elif project is not None or project_id:
        url = f"/tree/project/{project_id}"
        tag = f"termin:{kind}:project:{project_id}"
        if kind == "project_comment":
            title = f"{actor_name} commented"
            body = f'In project "{project_name or "Project"}"'
        elif kind == "project_shared":
            title = "Added to project"
            body = ((actor_name + " added you to " + (project_name or "Project")) if actor_name else ("Added to " + (project_name or "Project")))
        elif kind == "project_removed":
            title = "Removed from project"
            body = ((actor_name + " removed you from " + (project_name or "Project")) if actor_name else ("Removed from " + (project_name or "Project")))
        else:
            title = "Project updated"
            body = f'{actor_name} updated "{project_name or "Project"}"'
    elif group is not None or group_id:
        url = f"/tree/project/{group_project_id}/group/{group_id}"
        tag = f"termin:{kind}:group:{group_id}"
        if kind == "group_comment":
            title = f"{actor_name} commented"
            body = f'In group "{group_name or "Group"}"'
        else:
            title = "Group updated"
            body = f'{actor_name} updated "{group_name or "Group"}"'

    return {
        "title": title,
        "body": body,
        "url": _absolute_url(url),
        "tag": tag,
    }


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


def queue_user_notification(
    *,
    user_id: int,
    kind: str,
    task_id: int | None = None,
    comment_id: int | None = None,
    detail_payload: dict | None = None,
) -> None:
    event_key = notification_event_key_for_kind(kind, task_id=task_id)
    task = Task.query.get(task_id) if task_id else None
    push_enabled = user_notification_channel_enabled(user_id, event_key, "push", task=task)
    email_enabled = user_notification_channel_enabled(user_id, event_key, "email", task=task)
    if not push_enabled and not email_enabled:
        return
    now = datetime.utcnow()
    detail_json = _dump_notification_data(detail_payload)
    existing_query = TaskNotification.query.filter_by(user_id=user_id, kind=kind)
    if task_id is None:
        existing_query = existing_query.filter(TaskNotification.task_id.is_(None))
    else:
        existing_query = existing_query.filter_by(task_id=task_id)
    existing = (
        existing_query
        .filter(TaskNotification.read_at.is_(None))
        .order_by(TaskNotification.created_at.desc(), TaskNotification.id.desc())
        .first()
    )
    if existing:
        existing.created_at = now
        existing.comment_id = comment_id
        existing.notification_data = detail_json
        existing.emailed_at = None
    else:
        db.session.add(
            TaskNotification(
                user_id=user_id,
                task_id=task_id,
                comment_id=comment_id,
                kind=kind,
                notification_data=detail_json,
                emailed_at=None,
                created_at=now,
            )
        )
    if push_enabled:
        send_web_push_notification(
            user_id=user_id,
            payload=_web_push_payload_for_notification(
                kind=kind,
                task=task,
                detail_payload=detail_payload,
            ),
        )


def _user_sids(user_id: int) -> list[str]:
    return [sid for sid, sid_user_id in _sid_user.items() if int(sid_user_id) == int(user_id)]


def _collaborator_sids(collaborator_id: int) -> list[str]:
    return [sid for sid, sid_collaborator_id in _sid_collaborator.items() if int(sid_collaborator_id) == int(collaborator_id)]


def _project_sids(project_id: int) -> list[str]:
    normalized_project_id = int(project_id)
    return [
        sid
        for sid, project_ids in _sid_projects.items()
        if normalized_project_id in {int(value) for value in project_ids}
    ]


def is_user_viewing_project(user_id: int, project_id: int) -> bool:
    return _project_user_counts.get(project_id, {}).get(user_id, 0) > 0


def is_user_viewing_task(user_id: int, task_id: int) -> bool:
    return _task_user_counts.get(task_id, {}).get(user_id, 0) > 0


def is_user_viewing_discussion(user_id: int, entity_type: str, entity_id: int) -> bool:
    return _discussion_user_counts.get(discussion_key(entity_type, entity_id), {}).get(user_id, 0) > 0


def _can_access_task(user, task: Task) -> bool:
    if not user or not task:
        return False
    if Project.query.filter_by(id=task.project_id, owner_id=user.id).first():
        return True
    if ProjectMember.query.filter_by(project_id=task.project_id, user_id=user.id).first():
        return True
    return (
        db.session.query(Group.id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(Group.project_id == task.project_id, GroupMember.user_id == user.id)
        .first()
        is not None
    )


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


def _task_collaborator_ids(task: Task) -> set[int]:
    if not task:
        return set()
    emails = {
        (row.email or "").strip().lower()
        for row in Assignment.query.filter_by(task_id=task.id).all()
        if (row.email or "").strip()
    }
    emails.update(
        (row.email or "").strip().lower()
        for row in Invite.query.filter_by(task_id=task.id).all()
        if (row.email or "").strip()
    )
    if not emails:
        return set()
    collaborators = (
        CollaboratorProfile.query
        .filter(func.lower(CollaboratorProfile.email).in_(list(emails)))
        .all()
    )
    return {row.id for row in collaborators if row and row.access_token}


def _project_comment_user_ids(project_id: int) -> set[int]:
    project = Project.query.get(project_id)
    user_ids = {project.owner_id} if project else set()
    user_ids.update(row.user_id for row in ProjectMember.query.filter_by(project_id=project_id).all())
    group_ids = [group_id for (group_id,) in db.session.query(Group.id).filter(Group.project_id == project_id).all()]
    if group_ids:
        user_ids.update(row.user_id for row in GroupMember.query.filter(GroupMember.group_id.in_(group_ids)).all())
    return {user_id for user_id in user_ids if user_id}


def _group_comment_user_ids(group_id: int) -> set[int]:
    group = Group.query.get(group_id)
    if not group:
        return set()
    project = Project.query.get(group.project_id)
    user_ids = {project.owner_id} if project else set()
    user_ids.update(row.user_id for row in ProjectMember.query.filter_by(project_id=group.project_id).all())
    user_ids.update(row.user_id for row in GroupMember.query.filter_by(group_id=group_id).all())
    return {user_id for user_id in user_ids if user_id}


def _project_update_user_ids(project_id: int) -> set[int]:
    project = Project.query.get(project_id)
    if not project:
        return set()
    user_ids = {project.owner_id}
    user_ids.update(row.user_id for row in ProjectMember.query.filter_by(project_id=project_id).all())
    group_ids = [group_id for (group_id,) in db.session.query(Group.id).filter(Group.project_id == project_id).all()]
    if group_ids:
        user_ids.update(row.user_id for row in GroupMember.query.filter(GroupMember.group_id.in_(group_ids)).all())
    return {user_id for user_id in user_ids if user_id}


def _render_description(description: str | None, description_format: str | None, default_format: str) -> str:
    if not description:
        return ""
    fmt = (description_format or default_format or "").strip().lower()
    if fmt == "markdown":
        rendered = render_markdown(description, extensions=["extra", "sane_lists"])
        return sanitize_info_html(rendered)
    if fmt == "html":
        return sanitize_info_html(description)
    paragraphs = []
    for block in re.split(r"\n\s*\n", escape(description).strip()):
        if not block:
            continue
        paragraphs.append(f"<p>{block.replace('\\n', '<br />')}</p>")
    return "".join(paragraphs)


def _serialize_project_payload(project: Project) -> dict:
    info_payload = load_info_payload(getattr(project, "info", None), getattr(project, "link", None))
    return {
        "id": project.id,
        "name": project.name,
        "owner_id": project.owner_id,
        "links": info_payload.get("links", []),
        "division_id": project.division_id,
        "is_direct": bool(getattr(project, "is_direct", False)),
        "description": project.description,
        "description_format": project.description_format or "markdown",
        "start_date": project.start_date.isoformat() if getattr(project, "start_date", None) else None,
        "end_date": project.end_date.isoformat() if getattr(project, "end_date", None) else None,
    }


def _serialize_project_payload_for_user(project: Project, user_id: int) -> dict:
    payload = _serialize_project_payload(project)
    access = project_access_map(project.id)
    current_access = access.get(user_id) or {}
    other_member_ids = [member_id for member_id in access.keys() if int(member_id) != int(user_id)]
    payload["can_manage"] = bool(
        int(project.owner_id or 0) == int(user_id)
        or current_access.get("project_member")
    )
    payload["is_shared_with_me"] = bool(int(project.owner_id or 0) != int(user_id))
    payload["is_shared_out"] = bool(int(project.owner_id or 0) == int(user_id) and other_member_ids)
    if project.is_direct:
        peer_id = None
        if project.direct_user_a_id and int(project.direct_user_a_id) != int(user_id):
            peer_id = project.direct_user_a_id
        elif project.direct_user_b_id and int(project.direct_user_b_id) != int(user_id):
            peer_id = project.direct_user_b_id
        peer = User.query.get(peer_id) if peer_id else None
        payload["direct_peer"] = (
            {
                "id": peer.id,
                "email": peer.email,
                "display_name": display_name_for_user(peer),
                "avatar_url": peer.avatar_url,
            }
            if peer
            else None
        )
        payload["display_name"] = display_name_for_user(peer) if peer else project.name
        payload["division_id"] = None
    else:
        pref = ProjectSidebarPreference.query.filter_by(user_id=user_id, project_id=project.id).first()
        if pref:
            payload["division_id"] = pref.division_id
    payload["description"] = project.description
    payload["description_format"] = project.description_format or "markdown"
    payload["rendered_description"] = _render_description(project.description, project.description_format, "markdown")
    return payload


def _serialize_group_payload(group: Group) -> dict:
    info_payload = load_info_payload(getattr(group, "info", None), getattr(group, "link", None))
    return {
        "id": group.id,
        "project_id": group.project_id,
        "name": group.name,
        "color": group.color,
        "position": group.position,
        "links": info_payload.get("links", []),
        "description": group.description,
        "description_format": group.description_format or "markdown",
        "rendered_description": _render_description(group.description, group.description_format, "markdown"),
    }


def _serialize_division_payload(division, *, theme_name: str | None = None) -> dict:
    return {
        "id": division.id,
        "name": division.name,
        "color": division_effective_color(division, normalize_theme_name(theme_name or DEFAULT_THEME_NAME)),
        "color_slot": getattr(division, "color_slot", None),
        "custom_color": getattr(division, "color", None),
        "position": division.position,
    }


def _task_summary(task: Task | None) -> dict | None:
    if not task:
        return None
    info_payload = load_info_payload(getattr(task, "info", None), getattr(task, "link", None))
    meta = info_payload.get("meta") or {}
    follow_project_members = str(((info_payload.get("meta") or {}).get("follow_project_members") or "")).strip().lower() in {"1", "true", "yes", "on"}
    raw_status_mode = normalize_task_status_mode(getattr(task, "status_mode", None) or meta.get("status_mode"), default="")
    status_mode = raw_status_mode if raw_status_mode in {"single", "multi", "percent"} else ("multi" if bool(getattr(task, "per_user_status_enabled", False)) else "single")
    raw_percentage = str((meta.get("status_percentage") or "")).strip()
    try:
        status_percentage = max(0, min(100, int(float(raw_percentage))))
    except (TypeError, ValueError):
        status_percentage = 100 if str(task.status or "").strip().lower() in {"complete", "completed", "done", "closed", "pr closed", "pr merged"} else 0
    return {
        "id": task.id,
        "project_id": task.project_id,
        "group_id": task.group_id,
        "locked": bool(task.locked),
        "title": task.title,
        "link": task.link,
        "links": info_payload.get("links", []),
        "status": task.status,
        "status_mode": status_mode,
        "status_percentage": status_percentage,
        "per_user_status_enabled": status_mode == "multi",
        "assign_group_members": bool(task.assign_group_members),
        "group_assignment_members": serialize_group_assignment_members(task) if task.assign_group_members else [],
        "follow_project_members": follow_project_members,
        "project_follower_members": list(project_access_map(task.project_id).values()) if follow_project_members else [],
        "status_meta": task_status_meta(task),
        "position": task.position,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "due_mode": _task_due_mode(task),
        "start_date": _task_start_date(task),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "prerequisites": _serialize_task_prerequisites(task.id),
    }


def _serialize_task_prerequisites(task_id: int) -> list[dict]:
    rows = (
        TaskPrerequisite.query.filter_by(task_id=task_id)
        .order_by(TaskPrerequisite.created_at.asc(), TaskPrerequisite.id.asc())
        .all()
    )
    payload: list[dict] = []
    for row in rows:
        prerequisite_task = Task.query.get(row.prerequisite_task_id)
        if not prerequisite_task:
            continue
        payload.append(
            {
                "id": row.id,
                "task_id": row.task_id,
                "prerequisite_task_id": row.prerequisite_task_id,
                "title": prerequisite_task.title,
                "project_id": prerequisite_task.project_id,
                "group_id": prerequisite_task.group_id,
                "status": prerequisite_task.status,
                "status_mode": normalize_task_status_mode(getattr(prerequisite_task, "status_mode", None), default="single"),
                "status_percentage": task_status_percentage(prerequisite_task),
                "status_meta": task_status_meta(prerequisite_task),
                "prereq_blocked": bool((task_status_meta(prerequisite_task) or {}).get("prereq_blocked")),
                "due_at": prerequisite_task.due_at.isoformat() if prerequisite_task.due_at else None,
                "due_mode": _task_due_mode(prerequisite_task),
                "start_date": _task_start_date(prerequisite_task),
                "locked": bool(prerequisite_task.locked),
            }
        )
    return payload


def _serialize_task_dependents(task_id: int) -> list[dict]:
    rows = (
        TaskPrerequisite.query.filter_by(prerequisite_task_id=task_id)
        .order_by(TaskPrerequisite.created_at.asc(), TaskPrerequisite.id.asc())
        .all()
    )
    payload: list[dict] = []
    for row in rows:
        dependent_task = Task.query.get(row.task_id)
        if not dependent_task:
            continue
        status_meta = task_status_meta(dependent_task)
        payload.append(
            {
                "id": row.id,
                "task_id": row.task_id,
                "prerequisite_task_id": row.prerequisite_task_id,
                "dependent_task_id": row.task_id,
                "title": dependent_task.title,
                "project_id": dependent_task.project_id,
                "group_id": dependent_task.group_id,
                "status": dependent_task.status,
                "status_mode": normalize_task_status_mode(getattr(dependent_task, "status_mode", None), default="single"),
                "status_percentage": task_status_percentage(dependent_task),
                "status_meta": status_meta,
                "prereq_blocked": bool((status_meta or {}).get("prereq_blocked")),
                "due_at": dependent_task.due_at.isoformat() if dependent_task.due_at else None,
                "due_mode": _task_due_mode(dependent_task),
                "start_date": _task_start_date(dependent_task),
                "locked": bool(dependent_task.locked),
            }
        )
    return payload


def _serialize_task_data(
    task: Task,
    *,
    viewer_user_id: int | None = None,
    viewer_email: str | None = None,
) -> dict:
    info_payload = load_info_payload(getattr(task, "info", None), getattr(task, "link", None))
    follow_project_members = str(((info_payload.get("meta") or {}).get("follow_project_members") or "")).strip().lower() in {"1", "true", "yes", "on"}
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
    assignment_users = {
        user_id: User.query.get(user_id)
        for user_id in {row.user_id for row in assignments if row.user_id}
    }
    follower_users = {
        user_id: User.query.get(user_id)
        for user_id in {row.user_id for row in followers if row.user_id}
    }
    return {
        "id": task.id,
        "project_id": task.project_id,
        "group_id": task.group_id,
        "locked": bool(task.locked),
        "title": task.title,
        "link": task.link,
        "links": info_payload.get("links", []),
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "due_mode": _task_due_mode(task),
        "status": task.status,
        "status_mode": normalize_task_status_mode(getattr(task, "status_mode", None), default="single"),
        "status_percentage": task_status_percentage(task),
        "per_user_status_enabled": normalize_task_status_mode(getattr(task, "status_mode", None), default="single") == "multi",
        "assign_group_members": bool(task.assign_group_members),
        "group_assignment_members": serialize_group_assignment_members(task) if task.assign_group_members else [],
        "follow_project_members": follow_project_members,
        "project_follower_members": list(project_access_map(task.project_id).values()) if follow_project_members else [],
        "assignments": [
            {
                "id": row.id,
                "task_id": row.task_id,
                "user_id": row.user_id,
                "email": row.email,
                "status": row.status,
                "display_name": (assignment_users.get(row.user_id).display_name if row.user_id and assignment_users.get(row.user_id) else None),
                "display_email": (assignment_users.get(row.user_id).email if row.user_id and assignment_users.get(row.user_id) else row.email),
                "avatar_url": (assignment_users.get(row.user_id).avatar_url if row.user_id and assignment_users.get(row.user_id) else None),
            }
            for row in assignments
        ],
        "followers": [
            {
                "id": row.id,
                "task_id": row.task_id,
                "user_id": row.user_id,
                "display_name": (follower_users.get(row.user_id).display_name if row.user_id and follower_users.get(row.user_id) else None),
                "display_email": (follower_users.get(row.user_id).email if row.user_id and follower_users.get(row.user_id) else None),
                "avatar_url": (follower_users.get(row.user_id).avatar_url if row.user_id and follower_users.get(row.user_id) else None),
            }
            for row in followers
        ],
        "prerequisites": _serialize_task_prerequisites(task.id),
        "dependents": _serialize_task_dependents(task.id),
        "status_meta": task_status_meta(task, viewer_user_id=viewer_user_id, viewer_email=viewer_email),
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


def project_access_map(project_id: int) -> dict[int, dict]:
    from app.models import User

    project = Project.query.get(project_id)
    if not project:
        return {}

    access: dict[int, dict] = {}

    def ensure(user_id: int | None):
        if not user_id:
            return None
        row = access.get(user_id)
        if row is None:
            user = User.query.get(user_id)
            if not user:
                return None
            row = {
                "id": user.id,
                "email": user.email,
                "display_name": user.display_name,
                "avatar_url": user.avatar_url,
                "owner": False,
                "project_member": False,
            }
            access[user_id] = row
        return row

    owner = ensure(project.owner_id)
    if owner:
        owner["owner"] = True
        owner["project_member"] = True

    for row in ProjectMember.query.filter_by(project_id=project_id).all():
        member = ensure(row.user_id)
        if member:
            member["project_member"] = True

    group_ids = [group_id for (group_id,) in db.session.query(Group.id).filter(Group.project_id == project_id).all()]
    if group_ids:
        for row in GroupMember.query.filter(GroupMember.group_id.in_(group_ids)).all():
            member = ensure(row.user_id)
            if member:
                member["project_member"] = True

    return access


def notification_payload_for_user(user_id: int) -> dict:
    notification_rows = (
        TaskNotification.query.filter_by(user_id=user_id)
        .filter(or_(TaskNotification.read_at.is_(None), TaskNotification.pinned.is_(True)))
        .order_by(TaskNotification.pinned.desc(), TaskNotification.created_at.desc(), TaskNotification.id.desc())
        .limit(24)
        .all()
    )
    unread_rows = [row for row in notification_rows if row.read_at is None]
    unread_regular_rows = [row for row in unread_rows if row.kind != "comment"]
    unread_message_rows = [row for row in unread_rows if row.kind == "comment"]
    unread_task_ids = sorted({row.task_id for row in unread_rows if row.task_id})
    unread_comment_task_ids = sorted({row.task_id for row in unread_message_rows if row.task_id})
    unread_comment_task_set = {row.task_id for row in unread_message_rows if row.task_id}
    comment_ids = [row.comment_id for row in notification_rows if row.comment_id]
    task_ids = [row.task_id for row in notification_rows if row.task_id]
    comments = {
        row.id: row
        for row in TaskComment.query.filter(TaskComment.id.in_(comment_ids)).all()
    } if comment_ids else {}
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
    tasks = {
        row.id: row
        for row in Task.query.filter(Task.id.in_(task_ids)).all()
    } if task_ids else {}
    project_ids = {task.project_id for task in tasks.values()}
    projects = {
        row.id: row
        for row in Project.query.filter(Project.id.in_(project_ids)).all()
    } if project_ids else {}
    latest_read_by_task = {}
    unread_comment_count_by_task = {}
    if unread_comment_task_set:
        latest_read_rows = (
            db.session.query(TaskNotification.task_id, db.func.max(TaskNotification.read_at))
            .filter(
                TaskNotification.user_id == user_id,
                TaskNotification.kind == "comment",
                TaskNotification.task_id.in_(unread_comment_task_set),
                TaskNotification.read_at.isnot(None),
            )
            .group_by(TaskNotification.task_id)
            .all()
        )
        latest_read_by_task = {task_id: read_at for task_id, read_at in latest_read_rows if task_id}
        unread_comment_count_by_task = {
            task_id: 0
            for task_id in unread_comment_task_set
        }
        comment_rows = TaskComment.query.filter(TaskComment.task_id.in_(unread_comment_task_set)).all()
        for comment_row in comment_rows:
            if comment_row.user_id and int(comment_row.user_id) == int(user_id):
                continue
            threshold = latest_read_by_task.get(comment_row.task_id)
            if threshold and comment_row.created_at <= threshold:
                continue
            unread_comment_count_by_task[comment_row.task_id] = unread_comment_count_by_task.get(comment_row.task_id, 0) + 1
    notifications = []
    regular_notifications = []
    message_notifications = []
    specific_task_ids = {
        row.task_id
        for row in notification_rows
        if row.task_id and row.kind in _SPECIFIC_TASK_NOTIFICATION_KINDS
    }
    for row in notification_rows:
        if row.kind == "task_update" and row.task_id and row.task_id in specific_task_ids:
            continue
        task = tasks.get(row.task_id)
        comment = comments.get(row.comment_id)
        detail = _load_notification_data(getattr(row, "notification_data", None))
        project_id = detail.get("project_id") if detail.get("project_id") is not None else (task.project_id if task else None)
        try:
            project_id = int(project_id) if project_id is not None and project_id != "" else None
        except (TypeError, ValueError):
            project_id = None
        project = projects.get(project_id) if project_id else (projects.get(task.project_id) if task else None)
        sender_name = None
        if comment:
            if comment.user_id:
                author = authors.get(comment.user_id)
                sender_name = (author.display_name or author.email) if author else None
            elif comment.collaborator_id:
                collaborator = collaborators.get(comment.collaborator_id)
                sender_name = (collaborator.display_name or collaborator.email) if collaborator else None
        actor_name = str(detail.get("actor_name") or "").strip()
        task_label = str((task.title if task else (detail.get("project_name") or (project.name if project else "Project"))) or "Task").strip()
        summary = "Task updated"
        preview = ""
        inbox_preview = ""
        if row.kind == "comment":
            summary = (sender_name or "New message") + " commented"
            if comment and comment.body:
                preview = comment.body[:120] + ("..." if len(comment.body) > 120 else "")
            else:
                preview = "New comment"
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
                    project_label = str(detail.get("project_name") or payload.get("project_name") or payload.get("task_title") or "Task").strip()
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
                changed_fields = [str(value).strip() for value in (detail.get("changed_fields") or []) if str(value).strip()]
                if actor_name and changed_fields:
                    preview = actor_name + " updated " + task_label + " (" + ", ".join(changed_fields) + ")"
                elif changed_fields:
                    preview = "Updated " + ", ".join(changed_fields)
                else:
                    preview = "Task updated"
                inbox_preview = ("updated " + ", ".join(changed_fields)) if changed_fields else "updated"
        payload = {
            "id": row.id,
            "task_id": row.task_id,
            "task_title": task.title if task else (str(detail.get("project_name") or (project.name if project else "Project"))),
            "task_data": _task_summary(task),
            "project_id": project.id if project else project_id,
            "project_name": project.name if project else (detail.get("project_name") or None),
            "kind": row.kind,
            "detail_payload": detail,
            "summary": summary,
            "pinned": bool(row.pinned),
            "read": row.read_at is not None,
            "sender_name": sender_name or "New message",
            "preview": preview,
            "inbox_preview": inbox_preview or preview,
            "comment_preview": preview,
            "unread_count": unread_comment_count_by_task.get(row.task_id, 0) if row.kind == "comment" else 0,
            "created_at": row.created_at.isoformat(),
        }
        notifications.append(payload)
        if row.kind == "comment":
            message_notifications.append(payload)
        else:
            regular_notifications.append(payload)
    return {
        "unread_total": len(unread_rows),
        "unread_regular_total": len(unread_regular_rows),
        "unread_message_total": len(unread_message_rows),
        "unread_task_ids": unread_task_ids,
        "unread_comment_task_ids": unread_comment_task_ids,
        "notifications": notifications,
        "regular_notifications": regular_notifications,
        "message_notifications": message_notifications,
    }


def emit_notification_state(user_id: int) -> None:
    payload = notification_payload_for_user(user_id)
    _debug_socket_emit("notification_state", payload, room=user_room(user_id))
    socketio.emit("notification_state", payload, room=user_room(user_id))
    try:
        deliver_due_notification_emails_for_user(user_id)
    except Exception:
        pass


def emit_user_notification_preview(user_id: int, payload: dict) -> None:
    _debug_socket_emit("notification_preview", payload, room=user_room(user_id))
    socketio.emit("notification_preview", payload, room=user_room(user_id))


def emit_notification_preview_dismissed(user_id: int, notification_id: str) -> None:
    payload = {"notification_id": str(notification_id or "")}
    _debug_socket_emit("notification_preview_dismissed", payload, room=user_room(user_id))
    socketio.emit("notification_preview_dismissed", payload, room=user_room(user_id))


def emit_discussion_activity_updated(user_id: int, activity_row_id: int) -> None:
    row = UserDiscussionActivity.query.get(activity_row_id)
    if not row or int(row.user_id) != int(user_id):
        return
    items = build_discussion_activity_items([row], user_id=user_id)
    if not items:
        return
    payload = dict(items[0])
    created_at = payload.get("created_at")
    if getattr(created_at, "isoformat", None):
        payload["created_at"] = created_at.isoformat()
    _debug_socket_emit("discussion_activity_updated", payload, room=user_room(user_id))
    socketio.emit("discussion_activity_updated", payload, room=user_room(user_id))


def _recompute_user_active_projects(user_id: int) -> None:
    active_projects = set()
    for project_id, counts in _project_user_counts.items():
        if counts.get(user_id, 0) > 0:
            active_projects.add(project_id)
    if active_projects:
        _user_active_projects[user_id] = active_projects
    else:
        _user_active_projects.pop(user_id, None)


def emit_global_presence() -> None:
    user_projects = {
        str(user_id): sorted(project_ids)
        for user_id, project_ids in _user_active_projects.items()
        if project_ids
    }
    socketio.emit("global_presence", {"user_projects": user_projects})


def emit_project_presence(project_id: int) -> None:
    user_counts = _project_user_counts.get(project_id, {})
    online_user_ids = sorted(user_id for user_id, count in user_counts.items() if count > 0)
    socketio.emit(
        "project_presence",
        {
            "project_id": project_id,
            "online_user_ids": online_user_ids,
        },
        room=project_room(project_id),
    )


def _can_access_project_discussion(user, project_id: int) -> bool:
    if not user or not project_id:
        return False
    return bool(
        Project.query.filter_by(id=project_id, owner_id=user.id).first()
        or ProjectMember.query.filter_by(project_id=project_id, user_id=user.id).first()
        or db.session.query(Group.id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(Group.project_id == project_id, GroupMember.user_id == user.id)
        .first()
    )


def discussion_presence_payload(entity_type: str, entity_id: int) -> dict:
    counts = _discussion_user_counts.get(discussion_key(entity_type, entity_id), {})
    user_ids = [user_id for user_id, count in counts.items() if count > 0]
    users = [User.query.get(user_id) for user_id in user_ids]
    members = _serialize_members([user for user in users if user])
    members.sort(key=lambda item: (item.get("display_name") or item.get("email") or "").lower())
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "members": members,
    }


def emit_discussion_presence(entity_type: str, entity_id: int) -> None:
    socketio.emit(
        "discussion_presence",
        discussion_presence_payload(entity_type, entity_id),
        room=discussion_room(entity_type, entity_id),
    )


def _serialize_assignment_payload(assignment: Assignment) -> dict:
    from app.models import User
    account_user = None
    if assignment.user_id:
        account_user = User.query.get(assignment.user_id)
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


def emit_assignment_updated(task: Task, assignment: Assignment, *, action: str = "updated", actor_user_id: int | None = None) -> None:
    payload = {
        "action": action,
        "assignment": _serialize_assignment_payload(assignment),
        "actor_user_id": actor_user_id,
    }
    recipient_sids: set[str] = set(_project_sids(task.project_id))
    for recipient_id in _task_notification_user_ids(task):
        recipient_sids.update(_user_sids(recipient_id))
    for collaborator_id in _task_collaborator_ids(task):
        recipient_sids.update(_collaborator_sids(collaborator_id))
    assignment_email = (assignment.email or "").strip().lower()
    if assignment_email:
        collaborator = (
            CollaboratorProfile.query
            .filter(func.lower(CollaboratorProfile.email) == assignment_email)
            .first()
        )
        if collaborator and collaborator.access_token:
            recipient_sids.update(_collaborator_sids(collaborator.id))
    for sid in recipient_sids:
        _debug_socket_emit("assignment_updated", payload, sid=sid)
        socketio.emit("assignment_updated", payload, room=sid)


def _serialize_members(users) -> list[dict]:
    return [
        {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
        }
        for user in users
        if user
    ]


def project_members_payload(project_id: int) -> dict:
    members = list(project_access_map(project_id).values())
    members.sort(key=lambda item: (0 if item.get("owner") else 1, (item.get("display_name") or item.get("email") or "").lower()))
    return {"project_id": project_id, "members": members}


def group_members_payload(group_id: int) -> dict:
    group = Group.query.get(group_id)
    if not group:
        return {"group_id": group_id, "project_id": None, "members": []}
    return {"group_id": group_id, "project_id": group.project_id, "members": list(project_access_map(group.project_id).values())}


def task_presence_payload(task_id: int) -> dict:
    from app.models import User

    counts = _task_user_counts.get(task_id, {})
    user_ids = [user_id for user_id, count in counts.items() if count > 0]
    users = [User.query.get(user_id) for user_id in user_ids]
    members = _serialize_members([user for user in users if user])
    members.sort(key=lambda item: (item.get("display_name") or item.get("email") or "").lower())
    return {"task_id": task_id, "members": members}


def emit_task_presence(task_id: int) -> None:
    socketio.emit("task_presence", task_presence_payload(task_id), room=task_room(task_id))


def emit_project_members_updated(project_id: int, *, actor_user_id: int | None = None) -> None:
    payload = project_members_payload(project_id)
    payload["actor_user_id"] = actor_user_id
    socketio.emit("project_members_updated", payload, room=project_room(project_id))
    for recipient_id in _project_update_user_ids(project_id):
        socketio.emit("project_members_updated", payload, room=user_room(recipient_id))
        for sid in _user_sids(recipient_id):
            socketio.emit("project_members_updated", payload, room=sid)


def emit_group_members_updated(group_id: int, *, actor_user_id: int | None = None) -> None:
    payload = group_members_payload(group_id)
    payload["actor_user_id"] = actor_user_id
    project_id = payload.get("project_id")
    if project_id:
        socketio.emit("group_members_updated", payload, room=project_room(project_id))
        for recipient_id in _project_update_user_ids(project_id):
            socketio.emit("group_members_updated", payload, room=user_room(recipient_id))
            for sid in _user_sids(recipient_id):
                socketio.emit("group_members_updated", payload, room=sid)


def emit_task_comment_created(task_id: int, comment_payload: dict) -> None:
    comment_count = TaskComment.query.filter_by(task_id=task_id).count()
    payload = {
        "task_id": task_id,
        "comment": comment_payload,
        "comment_count": comment_count,
    }
    socketio.emit("task_comment_created", payload, room=task_room(task_id))
    task = Task.query.get(task_id)
    if task:
        for collaborator_id in _task_collaborator_ids(task):
            socketio.emit("task_comment_created", payload, room=collaborator_room(collaborator_id))


def emit_task_comment_updated(task_id: int, comment_payload: dict) -> None:
    payload = {
        "task_id": task_id,
        "comment": comment_payload,
    }
    socketio.emit("task_comment_updated", payload, room=task_room(task_id))
    task = Task.query.get(task_id)
    if task:
        for collaborator_id in _task_collaborator_ids(task):
            socketio.emit("task_comment_updated", payload, room=collaborator_room(collaborator_id))


def emit_task_comment_deleted(task_id: int, comment_id: int) -> None:
    comment_count = TaskComment.query.filter_by(task_id=task_id).count()
    payload = {
        "task_id": task_id,
        "comment_id": comment_id,
        "comment_count": comment_count,
    }
    socketio.emit("task_comment_deleted", payload, room=task_room(task_id))
    task = Task.query.get(task_id)
    if task:
        for collaborator_id in _task_collaborator_ids(task):
            socketio.emit("task_comment_deleted", payload, room=collaborator_room(collaborator_id))


def emit_project_comment_created(project_id: int, comment_payload: dict) -> None:
    comment_count = ProjectComment.query.filter_by(project_id=project_id).count()
    payload = {"project_id": project_id, "comment": comment_payload, "comment_count": comment_count}
    socketio.emit("project_comment_created", payload, room=project_room(project_id))
    for recipient_id in _project_comment_user_ids(project_id):
        socketio.emit("project_comment_created", payload, room=user_room(recipient_id))


def emit_project_comment_updated(project_id: int, comment_payload: dict) -> None:
    payload = {"project_id": project_id, "comment": comment_payload}
    socketio.emit("project_comment_updated", payload, room=project_room(project_id))
    for recipient_id in _project_comment_user_ids(project_id):
        socketio.emit("project_comment_updated", payload, room=user_room(recipient_id))


def emit_project_comment_deleted(project_id: int, comment_id: int) -> None:
    comment_count = ProjectComment.query.filter_by(project_id=project_id).count()
    payload = {"project_id": project_id, "comment_id": comment_id, "comment_count": comment_count}
    socketio.emit("project_comment_deleted", payload, room=project_room(project_id))
    for recipient_id in _project_comment_user_ids(project_id):
        socketio.emit("project_comment_deleted", payload, room=user_room(recipient_id))


def emit_group_comment_created(group_id: int, comment_payload: dict) -> None:
    comment_count = GroupComment.query.filter_by(group_id=group_id).count()
    payload = {"group_id": group_id, "project_id": None, "comment": comment_payload, "comment_count": comment_count}
    group = Group.query.get(group_id)
    if group:
        payload["project_id"] = group.project_id
        socketio.emit("group_comment_created", payload, room=project_room(group.project_id))
    for recipient_id in _group_comment_user_ids(group_id):
        socketio.emit("group_comment_created", payload, room=user_room(recipient_id))


def emit_group_comment_updated(group_id: int, comment_payload: dict) -> None:
    payload = {"group_id": group_id, "project_id": None, "comment": comment_payload}
    group = Group.query.get(group_id)
    if group:
        payload["project_id"] = group.project_id
        socketio.emit("group_comment_updated", payload, room=project_room(group.project_id))
    for recipient_id in _group_comment_user_ids(group_id):
        socketio.emit("group_comment_updated", payload, room=user_room(recipient_id))


def emit_group_comment_deleted(group_id: int, comment_id: int) -> None:
    comment_count = GroupComment.query.filter_by(group_id=group_id).count()
    payload = {"group_id": group_id, "project_id": None, "comment_id": comment_id, "comment_count": comment_count}
    group = Group.query.get(group_id)
    if group:
        payload["project_id"] = group.project_id
        socketio.emit("group_comment_deleted", payload, room=project_room(group.project_id))
    for recipient_id in _group_comment_user_ids(group_id):
        socketio.emit("group_comment_deleted", payload, room=user_room(recipient_id))


def _serialize_task_payload(
    task: Task,
    *,
    action: str = "updated",
    old_project_id: int | None = None,
    old_group_id: int | None = None,
    actor_user_id: int | None = None,
    viewer_user_id: int | None = None,
    viewer_email: str | None = None,
) -> dict:
    return {
        "action": action,
        "actor_user_id": actor_user_id,
        "task": _serialize_task_data(task, viewer_user_id=viewer_user_id, viewer_email=viewer_email),
        "old_project_id": old_project_id,
        "old_group_id": old_group_id,
    }


def emit_task_updated(task: Task, *, action: str = "updated", old_project_id: int | None = None, old_group_id: int | None = None, actor_user_id: int | None = None) -> None:
    relevant_project_ids = {int(task.project_id)}
    if old_project_id and int(old_project_id) != int(task.project_id):
        relevant_project_ids.add(int(old_project_id))

    recipient_sids: set[str] = set()
    for sid, project_ids in _sid_projects.items():
        if any(int(project_id) in relevant_project_ids for project_id in project_ids):
            recipient_sids.add(sid)

    for project_id in relevant_project_ids:
        for recipient_id in project_access_map(int(project_id)).keys():
            recipient_sids.update(_user_sids(recipient_id))

    for recipient_id in _task_notification_user_ids(task):
        recipient_sids.update(_user_sids(recipient_id))

    collaborator_ids = _task_collaborator_ids(task)
    for collaborator_id in collaborator_ids:
        recipient_sids.update(_collaborator_sids(collaborator_id))

    collaborator_emails = {
        row.id: (row.email or "").strip()
        for row in CollaboratorProfile.query.filter(CollaboratorProfile.id.in_(list(collaborator_ids))).all()
    } if collaborator_ids else {}

    payload_cache: dict[tuple[str, str], dict] = {}
    for sid in recipient_sids:
        viewer_user_id = _sid_user.get(sid)
        viewer_collaborator_id = _sid_collaborator.get(sid)
        viewer_email = collaborator_emails.get(viewer_collaborator_id, "") if viewer_collaborator_id is not None else ""
        cache_key = (
            f"user:{int(viewer_user_id)}" if viewer_user_id is not None else "",
            f"email:{viewer_email.strip().lower()}" if viewer_email else "",
        )
        payload = payload_cache.get(cache_key)
        if payload is None:
            payload = _serialize_task_payload(
                task,
                action=action,
                old_project_id=old_project_id,
                old_group_id=old_group_id,
                actor_user_id=actor_user_id,
                viewer_user_id=viewer_user_id,
                viewer_email=viewer_email,
            )
            payload_cache[cache_key] = payload
        _debug_socket_emit("task_updated", payload, sid=sid)
        socketio.emit("task_updated", payload, room=sid)


def emit_tasks_updated(tasks: list[Task], *, action: str = "updated", actor_user_id: int | None = None) -> None:
    normalized_tasks = [task for task in (tasks or []) if task and getattr(task, "id", None)]
    if not normalized_tasks:
        return
    relevant_project_ids = {int(task.project_id) for task in normalized_tasks if getattr(task, "project_id", None)}
    recipient_sids: set[str] = set()
    for sid, project_ids in _sid_projects.items():
        if any(int(project_id) in relevant_project_ids for project_id in project_ids):
            recipient_sids.add(sid)
    for project_id in relevant_project_ids:
        for recipient_id in project_access_map(int(project_id)).keys():
            recipient_sids.update(_user_sids(recipient_id))
    collaborator_ids: set[int] = set()
    for task in normalized_tasks:
        for recipient_id in _task_notification_user_ids(task):
            recipient_sids.update(_user_sids(recipient_id))
        collaborator_ids.update(_task_collaborator_ids(task))
    for collaborator_id in collaborator_ids:
        recipient_sids.update(_collaborator_sids(collaborator_id))
    collaborator_emails = {
        row.id: (row.email or "").strip()
        for row in CollaboratorProfile.query.filter(CollaboratorProfile.id.in_(list(collaborator_ids))).all()
    } if collaborator_ids else {}
    payload_cache: dict[tuple[str, str], dict] = {}
    for sid in recipient_sids:
        viewer_user_id = _sid_user.get(sid)
        viewer_collaborator_id = _sid_collaborator.get(sid)
        viewer_email = collaborator_emails.get(viewer_collaborator_id, "") if viewer_collaborator_id is not None else ""
        cache_key = (
            f"user:{int(viewer_user_id)}" if viewer_user_id is not None else "",
            f"email:{viewer_email.strip().lower()}" if viewer_email else "",
        )
        payload = payload_cache.get(cache_key)
        if payload is None:
            payload = {
                "action": action,
                "actor_user_id": actor_user_id,
                "tasks": [
                    _serialize_task_data(task, viewer_user_id=viewer_user_id, viewer_email=viewer_email)
                    for task in normalized_tasks
                ],
            }
            payload_cache[cache_key] = payload
        _debug_socket_emit("tasks_updated", payload, sid=sid)
        socketio.emit("tasks_updated", payload, room=sid)

def queue_task_notifications(
    task: Task,
    *,
    exclude_user_id: int | None = None,
    kind: str = "task_update",
    comment_id: int | None = None,
    detail_payload: dict | None = None,
) -> None:
    now = datetime.utcnow()
    detail_json = _dump_notification_data(detail_payload)
    event_key = notification_event_key_for_kind(kind, task_id=task.id, project_id=task.project_id, group_id=task.group_id)
    for recipient_id in _task_notification_user_ids(task):
        if exclude_user_id and recipient_id == exclude_user_id:
            continue
        push_enabled = user_notification_channel_enabled(recipient_id, event_key, "push", task=task)
        email_enabled = user_notification_channel_enabled(recipient_id, event_key, "email", task=task)
        if not push_enabled and not email_enabled:
            continue
        if kind == "comment":
            if is_user_viewing_discussion(recipient_id, "task", task.id):
                continue
        existing = (
            TaskNotification.query.filter_by(user_id=recipient_id, task_id=task.id, kind=kind)
            .filter(TaskNotification.read_at.is_(None))
            .order_by(TaskNotification.created_at.desc(), TaskNotification.id.desc())
            .first()
        )
        if existing:
            existing.created_at = now
            existing.comment_id = comment_id
            existing.notification_data = detail_json
            existing.emailed_at = None
        else:
            db.session.add(
                TaskNotification(
                    user_id=recipient_id,
                    task_id=task.id,
                    comment_id=comment_id,
                    kind=kind,
                    notification_data=detail_json,
                    emailed_at=None,
                    created_at=now,
                )
            )
        if push_enabled:
            send_web_push_notification(
                user_id=recipient_id,
                payload=_web_push_payload_for_notification(
                    kind=kind,
                    task=task,
                    detail_payload=detail_payload,
                ),
            )


def emit_task_notification_updates(task: Task, *, exclude_user_id: int | None = None) -> None:
    for recipient_id in _task_notification_user_ids(task):
        if exclude_user_id and recipient_id == exclude_user_id:
            continue
        emit_notification_state(recipient_id)


def emit_project_updated(project: Project, *, actor_user_id: int | None = None) -> None:
    for recipient_id in _project_update_user_ids(project.id):
        payload = {"project": _serialize_project_payload_for_user(project, recipient_id), "actor_user_id": actor_user_id}
        socketio.emit("project_updated", payload, room=user_room(recipient_id))


def emit_project_created(project: Project, *, actor_user_id: int | None = None, recipient_user_id: int | None = None) -> None:
    if recipient_user_id is not None:
        payload = {"project": _serialize_project_payload_for_user(project, recipient_user_id), "actor_user_id": actor_user_id}
        socketio.emit("project_created", payload, room=user_room(recipient_user_id))
        for sid in _user_sids(recipient_user_id):
            socketio.emit("project_created", payload, room=sid)
    else:
        for recipient_id in _project_update_user_ids(project.id):
            payload = {"project": _serialize_project_payload_for_user(project, recipient_id), "actor_user_id": actor_user_id}
            socketio.emit("project_created", payload, room=user_room(recipient_id))
            for sid in _user_sids(recipient_id):
                socketio.emit("project_created", payload, room=sid)


def emit_project_deleted(project_id: int, *, actor_user_id: int | None = None, task_ids: list[int] | None = None, recipient_user_ids: list[int] | None = None, include_project_room: bool = True) -> None:
    payload = {"project_id": project_id, "task_ids": task_ids or [], "actor_user_id": actor_user_id}
    if include_project_room:
        socketio.emit("project_deleted", payload, room=project_room(project_id))
    recipients = recipient_user_ids if recipient_user_ids is not None else list(_project_update_user_ids(project_id))
    for recipient_id in recipients:
        socketio.emit("project_deleted", payload, room=user_room(recipient_id))
        for sid in _user_sids(recipient_id):
            socketio.emit("project_deleted", payload, room=sid)


def emit_group_updated(group: Group, *, actor_user_id: int | None = None) -> None:
    payload = {"group": _serialize_group_payload(group), "actor_user_id": actor_user_id}
    socketio.emit("group_updated", payload, room=project_room(group.project_id))
    for recipient_id in _group_comment_user_ids(group.id):
        socketio.emit("group_updated", payload, room=user_room(recipient_id))


def emit_group_created(group: Group, *, actor_user_id: int | None = None) -> None:
    payload = {"group": _serialize_group_payload(group), "actor_user_id": actor_user_id}
    socketio.emit("group_created", payload, room=project_room(group.project_id))
    for recipient_id in _group_comment_user_ids(group.id):
        socketio.emit("group_created", payload, room=user_room(recipient_id))


def emit_group_deleted(group_id: int, project_id: int, *, actor_user_id: int | None = None, task_ids: list[int] | None = None) -> None:
    payload = {"group_id": group_id, "project_id": project_id, "task_ids": task_ids or [], "actor_user_id": actor_user_id}
    socketio.emit("group_deleted", payload, room=project_room(project_id))
    for recipient_id in _group_comment_user_ids(group_id):
        socketio.emit("group_deleted", payload, room=user_room(recipient_id))


def emit_division_updated(division, *, actor_user_id: int | None = None, recipient_user_id: int | None = None) -> None:
    recipient = User.query.get(recipient_user_id) if recipient_user_id else None
    payload = {"division": _serialize_division_payload(division, theme_name=getattr(recipient, "theme_name", None)), "actor_user_id": actor_user_id}
    if recipient_user_id is not None:
        socketio.emit("division_updated", payload, room=user_room(recipient_user_id))


def emit_division_created(division, *, actor_user_id: int | None = None, recipient_user_id: int | None = None) -> None:
    recipient = User.query.get(recipient_user_id) if recipient_user_id else None
    payload = {"division": _serialize_division_payload(division, theme_name=getattr(recipient, "theme_name", None)), "actor_user_id": actor_user_id}
    if recipient_user_id is not None:
        socketio.emit("division_created", payload, room=user_room(recipient_user_id))


def emit_division_deleted(division_id: int, *, actor_user_id: int | None = None, recipient_user_id: int | None = None) -> None:
    payload = {"division_id": division_id, "actor_user_id": actor_user_id}
    if recipient_user_id is not None:
        socketio.emit("division_deleted", payload, room=user_room(recipient_user_id))


def emit_group_reordered(project_id: int, order: list[int], *, actor_user_id: int | None = None) -> None:
    payload = {"project_id": project_id, "order": order, "actor_user_id": actor_user_id}
    socketio.emit("group_reordered", payload, room=project_room(project_id))


def emit_sidebar_reordered(user_id: int, order: list[str], *, actor_user_id: int | None = None) -> None:
    payload = {"order": order, "actor_user_id": actor_user_id}
    socketio.emit("sidebar_reordered", payload, room=user_room(user_id))


def register_socket_handlers() -> None:
    global _handlers_registered
    if _handlers_registered:
        return
    _handlers_registered = True

    @socketio.on("connect")
    def handle_connect():
        _debug_socket_recv("connect", {"args": dict(request.args or {})})
        user = current_user()
        if user:
            _sid_user[request.sid] = user.id
            _user_socket_counts[user.id] += 1
            join_room(user_room(user.id))
            emit("socket_ready", {"user_id": user.id})
            return
        collaborator_token = str(request.args.get("collaborator_token") or "").strip()
        if not collaborator_token:
            return False
        collaborator = CollaboratorProfile.query.filter_by(access_token=collaborator_token).first()
        if not collaborator:
            return False
        _sid_collaborator[request.sid] = collaborator.id
        join_room(collaborator_room(collaborator.id))
        emit("socket_ready", {"collaborator_id": collaborator.id})

    @socketio.on("join_task")
    def handle_join_task(data):
        _debug_socket_recv("join_task", data)
        user = current_user()
        if not user:
            return
        try:
            task_id = int((data or {}).get("task_id"))
        except (TypeError, ValueError):
            return
        task = Task.query.get(task_id)
        if not _can_access_task(user, task):
            return
        sid = request.sid
        user_id = _sid_user.get(sid)
        if user_id is None:
            return
        join_room(task_room(task_id))
        if task_id not in _sid_tasks[sid]:
            _sid_tasks[sid].add(task_id)
            _task_user_counts[task_id][user_id] += 1
        now = datetime.utcnow()
        (
            TaskNotification.query.filter_by(user_id=user_id, task_id=task_id, kind="comment", pinned=False)
            .filter(TaskNotification.read_at.is_(None))
            .update({"read_at": now}, synchronize_session=False)
        )
        db.session.commit()
        emit_notification_state(user_id)
        emit("task_room_joined", {"task_id": task_id})
        emit_task_presence(task_id)

    @socketio.on("join_discussion_presence")
    def handle_join_discussion_presence(data):
        _debug_socket_recv("join_discussion_presence", data)
        user = current_user()
        if not user:
            return
        entity_type = str((data or {}).get("entity_type") or "").strip().lower()
        try:
            entity_id = int((data or {}).get("entity_id"))
        except (TypeError, ValueError):
            return
        if entity_type not in {"task", "project", "group"}:
            return
        if entity_type == "task":
            task = Task.query.get(entity_id)
            if not _can_access_task(user, task):
                return
        elif entity_type == "project":
            project = Project.query.get(entity_id)
            if not project or not _can_access_project_discussion(user, entity_id):
                return
        else:
            group = Group.query.get(entity_id)
            if not group or not _can_access_project_discussion(user, group.project_id):
                return
        sid = request.sid
        user_id = _sid_user.get(sid)
        if user_id is None:
            return
        key = discussion_key(entity_type, entity_id)
        join_room(discussion_room(entity_type, entity_id))
        if key not in _sid_discussions[sid]:
            _sid_discussions[sid].add(key)
            _discussion_user_counts[key][user_id] += 1
        emit("discussion_presence_joined", {"entity_type": entity_type, "entity_id": entity_id})
        emit_discussion_presence(entity_type, entity_id)

    @socketio.on("join_project")
    def handle_join_project(data):
        _debug_socket_recv("join_project", data)
        user = current_user()
        if not user:
            return
        try:
            project_id = int((data or {}).get("project_id"))
        except (TypeError, ValueError):
            return
        passive = bool((data or {}).get("passive"))
        sid = request.sid
        user_id = _sid_user.get(sid)
        if user_id is None:
            return
        joined, is_new_join = _join_project_for_sid(
            sid=sid,
            user_id=user_id,
            project_id=project_id,
            passive=passive,
        )
        if not joined:
            return
        if is_new_join:
            _recompute_user_active_projects(user_id)
            emit_global_presence()
        if not passive:
            _mark_project_notifications_read(user_id, project_id)
            emit_notification_state(user_id)
            emit_project_presence(project_id)
        emit("project_room_joined", {"project_id": project_id, "passive": passive})

    @socketio.on("join_projects")
    def handle_join_projects(data):
        _debug_socket_recv("join_projects", data)
        user = current_user()
        if not user:
            return
        sid = request.sid
        user_id = _sid_user.get(sid)
        if user_id is None:
            return
        raw_projects = (data or {}).get("projects")
        if not isinstance(raw_projects, list):
            return
        joined_payloads: list[dict[str, object]] = []
        active_project_ids: list[int] = []
        any_new_join = False
        for entry in raw_projects:
            if not isinstance(entry, dict):
                continue
            try:
                project_id = int(entry.get("project_id"))
            except (TypeError, ValueError):
                continue
            passive = bool(entry.get("passive"))
            joined, is_new_join = _join_project_for_sid(
                sid=sid,
                user_id=user_id,
                project_id=project_id,
                passive=passive,
            )
            if not joined:
                continue
            joined_payloads.append({"project_id": project_id, "passive": passive})
            if is_new_join:
                any_new_join = True
            if not passive:
                active_project_ids.append(project_id)
        if not joined_payloads:
            return
        if any_new_join:
            _recompute_user_active_projects(user_id)
            emit_global_presence()
        if active_project_ids:
            for project_id in active_project_ids:
                _mark_project_notifications_read(user_id, project_id)
            emit_notification_state(user_id)
            for project_id in active_project_ids:
                emit_project_presence(project_id)
        emit("project_rooms_joined", {"projects": joined_payloads})

    @socketio.on("leave_discussion_presence")
    def handle_leave_discussion_presence(data):
        _debug_socket_recv("leave_discussion_presence", data)
        user = current_user()
        if not user:
            return
        entity_type = str((data or {}).get("entity_type") or "").strip().lower()
        try:
            entity_id = int((data or {}).get("entity_id"))
        except (TypeError, ValueError):
            return
        if entity_type not in {"task", "project", "group"}:
            return
        sid = request.sid
        user_id = _sid_user.get(sid)
        key = discussion_key(entity_type, entity_id)
        leave_room(discussion_room(entity_type, entity_id))
        if user_id is not None and key in _sid_discussions.get(sid, set()):
            _sid_discussions[sid].discard(key)
            counts = _discussion_user_counts.get(key)
            if counts:
                next_count = max(0, counts.get(user_id, 0) - 1)
                if next_count:
                    counts[user_id] = next_count
                else:
                    counts.pop(user_id, None)
                if not counts:
                    _discussion_user_counts.pop(key, None)
            emit_discussion_presence(entity_type, entity_id)
        emit("discussion_presence_left", {"entity_type": entity_type, "entity_id": entity_id})

    @socketio.on("leave_task")
    def handle_leave_task(data):
        _debug_socket_recv("leave_task", data)
        user = current_user()
        if not user:
            return
        try:
            task_id = int((data or {}).get("task_id"))
        except (TypeError, ValueError):
            return
        sid = request.sid
        user_id = _sid_user.get(sid)
        leave_room(task_room(task_id))
        if user_id is not None and task_id in _sid_tasks.get(sid, set()):
            _sid_tasks[sid].discard(task_id)
            counts = _task_user_counts.get(task_id)
            if counts:
                next_count = max(0, counts.get(user_id, 0) - 1)
                if next_count:
                    counts[user_id] = next_count
                else:
                    counts.pop(user_id, None)
                if not counts:
                    _task_user_counts.pop(task_id, None)
            emit_task_presence(task_id)
        emit("task_room_left", {"task_id": task_id})

    @socketio.on("disconnect")
    def handle_disconnect():
        _debug_socket_recv("disconnect", {})
        sid = request.sid
        user_id = _sid_user.pop(sid, None)
        _sid_collaborator.pop(sid, None)
        project_ids = list(_sid_projects.pop(sid, set()))
        task_ids = list(_sid_tasks.pop(sid, set()))
        discussion_keys = list(_sid_discussions.pop(sid, set()))
        if user_id is None:
            return
        next_socket_count = max(0, _user_socket_counts.get(user_id, 0) - 1)
        if next_socket_count:
            _user_socket_counts[user_id] = next_socket_count
        else:
            _user_socket_counts.pop(user_id, None)
        emit_global_presence()
        for task_id in task_ids:
            counts = _task_user_counts.get(task_id)
            if not counts:
                continue
            next_count = max(0, counts.get(user_id, 0) - 1)
            if next_count:
                counts[user_id] = next_count
            else:
                counts.pop(user_id, None)
            if not counts:
                _task_user_counts.pop(task_id, None)
            emit_task_presence(task_id)
        for project_id in project_ids:
            counts = _project_user_counts.get(project_id)
            if not counts:
                continue
            next_count = max(0, counts.get(user_id, 0) - 1)
            if next_count:
                counts[user_id] = next_count
            else:
                counts.pop(user_id, None)
            if not counts:
                _project_user_counts.pop(project_id, None)
            _recompute_user_active_projects(user_id)
            emit_project_presence(project_id)
        for key in discussion_keys:
            entity_type, _, entity_id_raw = key.partition(":")
            try:
                entity_id = int(entity_id_raw)
            except (TypeError, ValueError):
                continue
            counts = _discussion_user_counts.get(key)
            if not counts:
                continue
            next_count = max(0, counts.get(user_id, 0) - 1)
            if next_count:
                counts[user_id] = next_count
            else:
                counts.pop(user_id, None)
            if not counts:
                _discussion_user_counts.pop(key, None)
            emit_discussion_presence(entity_type, entity_id)
        emit_global_presence()
