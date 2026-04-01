from datetime import datetime
from collections import defaultdict
from html import escape
import json
from markdown import markdown as render_markdown
from sqlalchemy import or_

from flask import request
from flask_socketio import emit, join_room, leave_room

from app.extensions import db, socketio

from app.group_assignments import serialize_group_assignment_members
from app.info_utils import load_info_payload, sanitize_info_html
from app.discussion_activity import build_discussion_activity_items
from app.models import (
    Assignment,
    CollaboratorProfile,
    Group,
    GroupComment,
    GroupMember,
    Project,
    ProjectComment,
    ProjectMember,
    ProjectSidebarPreference,
    Task,
    TaskComment,
    TaskNotification,
    UserDiscussionActivity,
    User,
)
from app.task_status import task_status_meta
from app.utils import current_user, display_name_for_user

_handlers_registered = False
_sid_user = {}
_sid_projects = defaultdict(set)
_sid_tasks = defaultdict(set)
_sid_discussions = defaultdict(set)
_project_user_counts = defaultdict(lambda: defaultdict(int))
_task_user_counts = defaultdict(lambda: defaultdict(int))
_discussion_user_counts = defaultdict(lambda: defaultdict(int))
_user_socket_counts = defaultdict(int)
_user_active_projects = defaultdict(set)


def user_room(user_id: int) -> str:
    return f"user:{user_id}"


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
        return
    db.session.add(
        TaskNotification(
            user_id=user_id,
            task_id=task_id,
            comment_id=comment_id,
            kind=kind,
            notification_data=detail_json,
            created_at=now,
        )
    )


def _user_sids(user_id: int) -> list[str]:
    return [sid for sid, sid_user_id in _sid_user.items() if int(sid_user_id) == int(user_id)]


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
    return {user_id for user_id in user_ids if user_id}


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
    }


def _serialize_division_payload(division) -> dict:
    return {"id": division.id, "name": division.name, "color": division.color, "position": division.position}


def _task_summary(task: Task | None) -> dict | None:
    if not task:
        return None
    info_payload = load_info_payload(getattr(task, "info", None), getattr(task, "link", None))
    return {
        "id": task.id,
        "project_id": task.project_id,
        "group_id": task.group_id,
        "title": task.title,
        "link": task.link,
        "links": info_payload.get("links", []),
        "status": task.status,
        "per_user_status_enabled": bool(task.per_user_status_enabled),
        "assign_group_members": bool(task.assign_group_members),
        "group_assignment_members": serialize_group_assignment_members(task) if task.assign_group_members else [],
        "status_meta": task_status_meta(task),
        "position": task.position,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "due_mode": _task_due_mode(task),
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
    socketio.emit("notification_state", payload, room=user_room(user_id))
    for sid in _user_sids(user_id):
        socketio.emit("notification_state", payload, room=sid)


def emit_user_notification_preview(user_id: int, payload: dict) -> None:
    socketio.emit("notification_preview", payload, room=user_room(user_id))
    for sid in _user_sids(user_id):
        socketio.emit("notification_preview", payload, room=sid)


def emit_notification_preview_dismissed(user_id: int, notification_id: str) -> None:
    payload = {"notification_id": str(notification_id or "")}
    socketio.emit("notification_preview_dismissed", payload, room=user_room(user_id))
    for sid in _user_sids(user_id):
        socketio.emit("notification_preview_dismissed", payload, room=sid)


def emit_discussion_activity_updated(user_id: int, activity_row_id: int) -> None:
    row = UserDiscussionActivity.query.get(activity_row_id)
    if not row or int(row.user_id) != int(user_id):
        return
    items = build_discussion_activity_items([row], user_id=user_id)
    if not items:
        return
    payload = items[0]
    socketio.emit("discussion_activity_updated", payload, room=user_room(user_id))
    for sid in _user_sids(user_id):
        socketio.emit("discussion_activity_updated", payload, room=sid)


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
    socketio.emit("assignment_updated", payload, room=project_room(task.project_id))
    for recipient_id in _task_notification_user_ids(task):
        socketio.emit("assignment_updated", payload, room=user_room(recipient_id))


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
    socketio.emit(
        "task_comment_created",
        {
            "task_id": task_id,
            "comment": comment_payload,
            "comment_count": comment_count,
        },
        room=task_room(task_id),
    )


def emit_task_comment_updated(task_id: int, comment_payload: dict) -> None:
    socketio.emit(
        "task_comment_updated",
        {
            "task_id": task_id,
            "comment": comment_payload,
        },
        room=task_room(task_id),
    )


def emit_task_comment_deleted(task_id: int, comment_id: int) -> None:
    comment_count = TaskComment.query.filter_by(task_id=task_id).count()
    socketio.emit(
        "task_comment_deleted",
        {
            "task_id": task_id,
            "comment_id": comment_id,
            "comment_count": comment_count,
        },
        room=task_room(task_id),
    )


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


def _serialize_task_payload(task: Task, *, action: str = "updated", old_project_id: int | None = None, old_group_id: int | None = None, actor_user_id: int | None = None) -> dict:
    info_payload = load_info_payload(getattr(task, "info", None), getattr(task, "link", None))
    assignments = (
        Assignment.query.filter_by(task_id=task.id)
        .order_by(Assignment.created_at.asc(), Assignment.id.asc())
        .all()
    )
    assignment_users = {
        user_id: User.query.get(user_id)
        for user_id in {row.user_id for row in assignments if row.user_id}
    }
    return {
        "action": action,
        "actor_user_id": actor_user_id,
        "task": {
            "id": task.id,
            "project_id": task.project_id,
            "group_id": task.group_id,
            "title": task.title,
            "link": task.link,
            "links": info_payload.get("links", []),
            "due_at": task.due_at.isoformat() if task.due_at else None,
            "due_mode": _task_due_mode(task),
            "status": task.status,
            "per_user_status_enabled": bool(task.per_user_status_enabled),
            "assign_group_members": bool(task.assign_group_members),
            "group_assignment_members": serialize_group_assignment_members(task) if task.assign_group_members else [],
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
            "status_meta": task_status_meta(task),
            "created_at": task.created_at.isoformat() if task.created_at else None,
        },
        "old_project_id": old_project_id,
        "old_group_id": old_group_id,
    }


def emit_task_updated(task: Task, *, action: str = "updated", old_project_id: int | None = None, old_group_id: int | None = None, actor_user_id: int | None = None) -> None:
    payload = _serialize_task_payload(task, action=action, old_project_id=old_project_id, old_group_id=old_group_id, actor_user_id=actor_user_id)
    socketio.emit("task_updated", payload, room=project_room(task.project_id))
    if old_project_id and old_project_id != task.project_id:
        socketio.emit("task_updated", payload, room=project_room(old_project_id))
    for recipient_id in _task_notification_user_ids(task):
        socketio.emit("task_updated", payload, room=user_room(recipient_id))

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
    for recipient_id in _task_notification_user_ids(task):
        if exclude_user_id and recipient_id == exclude_user_id:
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
        else:
            db.session.add(
                TaskNotification(
                    user_id=recipient_id,
                    task_id=task.id,
                    comment_id=comment_id,
                    kind=kind,
                    notification_data=detail_json,
                    created_at=now,
                )
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
    payload = {"division": _serialize_division_payload(division), "actor_user_id": actor_user_id}
    if recipient_user_id is not None:
        socketio.emit("division_updated", payload, room=user_room(recipient_user_id))


def emit_division_created(division, *, actor_user_id: int | None = None, recipient_user_id: int | None = None) -> None:
    payload = {"division": _serialize_division_payload(division), "actor_user_id": actor_user_id}
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
        user = current_user()
        if not user:
            return False
        _sid_user[request.sid] = user.id
        _user_socket_counts[user.id] += 1
        join_room(user_room(user.id))
        emit("socket_ready", {"user_id": user.id})

    @socketio.on("join_task")
    def handle_join_task(data):
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
        user = current_user()
        if not user:
            return
        try:
            project_id = int((data or {}).get("project_id"))
        except (TypeError, ValueError):
            return
        project = Project.query.get(project_id)
        if not project:
            return
        if not (
            Project.query.filter_by(id=project_id, owner_id=user.id).first()
            or ProjectMember.query.filter_by(project_id=project_id, user_id=user.id).first()
            or db.session.query(Group.id)
            .join(GroupMember, GroupMember.group_id == Group.id)
            .filter(Group.project_id == project_id, GroupMember.user_id == user.id)
            .first()
        ):
            return
        sid = request.sid
        user_id = _sid_user.get(sid)
        if user_id is None:
            return
        join_room(project_room(project_id))
        if project_id not in _sid_projects[sid]:
            _sid_projects[sid].add(project_id)
            _project_user_counts[project_id][user_id] += 1
            _recompute_user_active_projects(user_id)
            emit_global_presence()
        now = datetime.utcnow()
        task_ids = db.session.query(Task.id).filter(Task.project_id == project_id)
        (
            TaskNotification.query.filter_by(user_id=user_id, pinned=False)
            .filter(TaskNotification.kind != "comment")
            .filter(TaskNotification.task_id.in_(task_ids))
            .filter(TaskNotification.read_at.is_(None))
            .update({"read_at": now}, synchronize_session=False)
        )
        db.session.commit()
        emit_notification_state(user_id)
        emit("project_room_joined", {"project_id": project_id})
        emit_project_presence(project_id)

    @socketio.on("leave_discussion_presence")
    def handle_leave_discussion_presence(data):
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
        sid = request.sid
        user_id = _sid_user.pop(sid, None)
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
