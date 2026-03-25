from datetime import datetime
from collections import defaultdict
from sqlalchemy import or_

from flask import request
from flask_socketio import emit, join_room, leave_room

from app.extensions import db, socketio

from app.info_utils import load_info_payload
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
    User,
)
from app.utils import current_user

_handlers_registered = False
_sid_user = {}
_sid_projects = defaultdict(set)
_sid_tasks = defaultdict(set)
_project_user_counts = defaultdict(lambda: defaultdict(int))
_task_user_counts = defaultdict(lambda: defaultdict(int))
_user_socket_counts = defaultdict(int)
_user_active_projects = defaultdict(set)


def user_room(user_id: int) -> str:
    return f"user:{user_id}"


def task_room(task_id: int) -> str:
    return f"task:{task_id}"


def project_room(project_id: int) -> str:
    return f"project:{project_id}"


def is_user_viewing_project(user_id: int, project_id: int) -> bool:
    return _project_user_counts.get(project_id, {}).get(user_id, 0) > 0


def is_user_viewing_task(user_id: int, task_id: int) -> bool:
    return _task_user_counts.get(task_id, {}).get(user_id, 0) > 0


def _can_access_task(user, task: Task) -> bool:
    if not user or not task:
        return False
    if Project.query.filter_by(id=task.project_id, owner_id=user.id).first():
        return True
    if ProjectMember.query.filter_by(project_id=task.project_id, user_id=user.id).first():
        return True
    if task.group_id and GroupMember.query.filter_by(group_id=task.group_id, user_id=user.id).first():
        return True
    return Assignment.query.filter_by(task_id=task.id, user_id=user.id).first() is not None


def _task_notification_user_ids(task: Task) -> set[int]:
    project = Project.query.get(task.project_id)
    user_ids = {project.owner_id} if project else set()
    user_ids.update(row.user_id for row in ProjectMember.query.filter_by(project_id=task.project_id).all())
    if task.group_id:
        user_ids.update(row.user_id for row in GroupMember.query.filter_by(group_id=task.group_id).all())
    user_ids.update(row.user_id for row in Assignment.query.filter_by(task_id=task.id).all() if row.user_id)
    return {user_id for user_id in user_ids if user_id}


def _project_comment_user_ids(project_id: int) -> set[int]:
    project = Project.query.get(project_id)
    user_ids = {project.owner_id} if project else set()
    user_ids.update(row.user_id for row in ProjectMember.query.filter_by(project_id=project_id).all())
    group_ids = [group_id for (group_id,) in db.session.query(Group.id).filter(Group.project_id == project_id).all()]
    if group_ids:
        user_ids.update(row.user_id for row in GroupMember.query.filter(GroupMember.group_id.in_(group_ids)).all())
        task_ids = [task_id for (task_id,) in db.session.query(Task.id).filter(Task.project_id == project_id).all()]
        if task_ids:
            user_ids.update(
                row.user_id
                for row in Assignment.query.filter(Assignment.task_id.in_(task_ids), Assignment.user_id.isnot(None)).all()
            )
    return {user_id for user_id in user_ids if user_id}


def _group_comment_user_ids(group_id: int) -> set[int]:
    group = Group.query.get(group_id)
    if not group:
        return set()
    project = Project.query.get(group.project_id)
    user_ids = {project.owner_id} if project else set()
    user_ids.update(row.user_id for row in ProjectMember.query.filter_by(project_id=group.project_id).all())
    user_ids.update(row.user_id for row in GroupMember.query.filter_by(group_id=group_id).all())
    task_ids = [task_id for (task_id,) in db.session.query(Task.id).filter(Task.group_id == group_id).all()]
    if task_ids:
        user_ids.update(
            row.user_id
            for row in Assignment.query.filter(Assignment.task_id.in_(task_ids), Assignment.user_id.isnot(None)).all()
        )
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
        task_ids = [task_id for (task_id,) in db.session.query(Task.id).filter(Task.project_id == project_id).all()]
        if task_ids:
            user_ids.update(
                row.user_id
                for row in Assignment.query.filter(Assignment.task_id.in_(task_ids), Assignment.user_id.isnot(None)).all()
            )
    return {user_id for user_id in user_ids if user_id}


def _serialize_project_payload(project: Project) -> dict:
    info_payload = load_info_payload(getattr(project, "info", None), getattr(project, "link", None))
    return {
        "id": project.id,
        "name": project.name,
        "links": info_payload.get("links", []),
        "division_id": project.division_id,
    }


def _serialize_project_payload_for_user(project: Project, user_id: int) -> dict:
    payload = _serialize_project_payload(project)
    pref = ProjectSidebarPreference.query.filter_by(user_id=user_id, project_id=project.id).first()
    if pref:
        payload["division_id"] = pref.division_id
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
        "position": task.position,
        "due_at": task.due_at.isoformat() if task.due_at else None,
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
                "partial": False,
                "group_member": False,
                "task_assignee": False,
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
                member["partial"] = True
                member["group_member"] = True

        task_ids = [task_id for (task_id,) in db.session.query(Task.id).filter(Task.project_id == project_id).all()]
        if task_ids:
            for row in Assignment.query.filter(Assignment.task_id.in_(task_ids), Assignment.user_id.isnot(None)).all():
                member = ensure(row.user_id)
                if member:
                    member["partial"] = True
                    member["task_assignee"] = True

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
    notifications = []
    regular_notifications = []
    message_notifications = []
    for row in notification_rows:
        task = tasks.get(row.task_id)
        comment = comments.get(row.comment_id)
        project = projects.get(task.project_id) if task else None
        sender_name = None
        if comment:
            if comment.user_id:
                author = authors.get(comment.user_id)
                sender_name = (author.display_name or author.email) if author else None
            elif comment.collaborator_id:
                collaborator = collaborators.get(comment.collaborator_id)
                sender_name = (collaborator.display_name or collaborator.email) if collaborator else None
        preview = ""
        if row.kind == "comment":
            if comment and comment.body:
                preview = comment.body[:120] + ("..." if len(comment.body) > 120 else "")
            else:
                preview = "New comment"
        elif row.kind == "task_moved":
            preview = "Task moved"
        elif row.kind == "task_created":
            preview = "Task created"
        else:
            preview = "Task updated"
        payload = {
            "id": row.id,
            "task_id": row.task_id,
            "task_title": task.title if task else "Task",
            "task_data": _task_summary(task),
            "project_id": project.id if project else None,
            "project_name": project.name if project else None,
            "kind": row.kind,
            "pinned": bool(row.pinned),
            "read": row.read_at is not None,
            "sender_name": sender_name or "New message",
            "preview": preview,
            "comment_preview": preview,
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
    socketio.emit("notification_state", notification_payload_for_user(user_id), room=user_room(user_id))


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
    members.sort(key=lambda item: (0 if item.get("owner") else 1, 0 if item.get("project_member") else 1, (item.get("display_name") or item.get("email") or "").lower()))
    return {"project_id": project_id, "members": members}


def group_members_payload(group_id: int) -> dict:
    from app.models import User
    group = Group.query.get(group_id)
    if not group:
        return {"group_id": group_id, "project_id": None, "members": []}
    project = Project.query.get(group.project_id)
    users = [User.query.get(project.owner_id)] if project else []
    if project:
        users.extend(User.query.get(row.user_id) for row in ProjectMember.query.filter_by(project_id=project.id).all())
    users.extend(User.query.get(row.user_id) for row in GroupMember.query.filter_by(group_id=group_id).all())
    task_ids = [task_id for (task_id,) in db.session.query(Task.id).filter(Task.group_id == group_id).all()]
    if task_ids:
        users.extend(
            User.query.get(row.user_id)
            for row in Assignment.query.filter(Assignment.task_id.in_(task_ids), Assignment.user_id.isnot(None)).all()
        )
    deduped = []
    seen = set()
    for user in users:
        if not user or user.id in seen:
            continue
        seen.add(user.id)
        deduped.append(user)
    return {"group_id": group_id, "project_id": group.project_id, "members": _serialize_members(deduped)}


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


def emit_group_members_updated(group_id: int, *, actor_user_id: int | None = None) -> None:
    payload = group_members_payload(group_id)
    payload["actor_user_id"] = actor_user_id
    project_id = payload.get("project_id")
    if project_id:
        socketio.emit("group_members_updated", payload, room=project_room(project_id))


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
    payload = {"group_id": group_id, "comment": comment_payload, "comment_count": comment_count}
    group = Group.query.get(group_id)
    if group:
        socketio.emit("group_comment_created", payload, room=project_room(group.project_id))
    for recipient_id in _group_comment_user_ids(group_id):
        socketio.emit("group_comment_created", payload, room=user_room(recipient_id))


def emit_group_comment_updated(group_id: int, comment_payload: dict) -> None:
    payload = {"group_id": group_id, "comment": comment_payload}
    group = Group.query.get(group_id)
    if group:
        socketio.emit("group_comment_updated", payload, room=project_room(group.project_id))
    for recipient_id in _group_comment_user_ids(group_id):
        socketio.emit("group_comment_updated", payload, room=user_room(recipient_id))


def emit_group_comment_deleted(group_id: int, comment_id: int) -> None:
    comment_count = GroupComment.query.filter_by(group_id=group_id).count()
    payload = {"group_id": group_id, "comment_id": comment_id, "comment_count": comment_count}
    group = Group.query.get(group_id)
    if group:
        socketio.emit("group_comment_deleted", payload, room=project_room(group.project_id))
    for recipient_id in _group_comment_user_ids(group_id):
        socketio.emit("group_comment_deleted", payload, room=user_room(recipient_id))


def _serialize_task_payload(task: Task, *, action: str = "updated", old_project_id: int | None = None, old_group_id: int | None = None, actor_user_id: int | None = None) -> dict:
    info_payload = load_info_payload(getattr(task, "info", None), getattr(task, "link", None))
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
            "status": task.status,
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

def queue_task_notifications(task: Task, *, exclude_user_id: int | None = None, kind: str = "task_update") -> None:
    now = datetime.utcnow()
    for recipient_id in _task_notification_user_ids(task):
        if exclude_user_id and recipient_id == exclude_user_id:
            continue
        if is_user_viewing_project(recipient_id, task.project_id):
            continue
        existing = (
            TaskNotification.query.filter_by(user_id=recipient_id, task_id=task.id, kind=kind)
            .filter(TaskNotification.read_at.is_(None))
            .order_by(TaskNotification.created_at.desc(), TaskNotification.id.desc())
            .first()
        )
        if existing:
            existing.created_at = now
            existing.comment_id = None
        else:
            db.session.add(
                TaskNotification(
                    user_id=recipient_id,
                    task_id=task.id,
                    comment_id=None,
                    kind=kind,
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
    else:
        for recipient_id in _project_update_user_ids(project.id):
            payload = {"project": _serialize_project_payload_for_user(project, recipient_id), "actor_user_id": actor_user_id}
            socketio.emit("project_created", payload, room=user_room(recipient_id))


def emit_project_deleted(project_id: int, *, actor_user_id: int | None = None, task_ids: list[int] | None = None) -> None:
    payload = {"project_id": project_id, "task_ids": task_ids or [], "actor_user_id": actor_user_id}
    socketio.emit("project_deleted", payload, room=project_room(project_id))
    for recipient_id in _project_update_user_ids(project_id):
        socketio.emit("project_deleted", payload, room=user_room(recipient_id))


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
            or db.session.query(Task.id)
            .join(Assignment, Assignment.task_id == Task.id)
            .filter(Task.project_id == project_id, Assignment.user_id == user.id)
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
        emit_global_presence()
