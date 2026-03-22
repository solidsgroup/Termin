from datetime import datetime
from collections import defaultdict

from flask import request
from flask_socketio import emit, join_room, leave_room

from app.extensions import db, socketio

from app.models import Assignment, Group, GroupMember, Project, ProjectMember, Task, TaskComment, TaskNotification, Subtask
from app.utils import current_user

_handlers_registered = False
_sid_user = {}
_sid_projects = defaultdict(set)
_project_user_counts = defaultdict(lambda: defaultdict(int))


def user_room(user_id: int) -> str:
    return f"user:{user_id}"


def task_room(task_id: int) -> str:
    return f"task:{task_id}"


def project_room(project_id: int) -> str:
    return f"project:{project_id}"


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


def notification_payload_for_user(user_id: int) -> dict:
    notification_rows = (
        TaskNotification.query.filter_by(user_id=user_id)
        .filter(TaskNotification.read_at.is_(None))
        .order_by(TaskNotification.created_at.desc(), TaskNotification.id.desc())
        .limit(12)
        .all()
    )
    unread_task_ids = sorted({row.task_id for row in notification_rows if row.task_id})
    unread_comment_task_ids = sorted({row.task_id for row in notification_rows if row.task_id and row.kind == "comment"})
    comment_ids = [row.comment_id for row in notification_rows if row.comment_id]
    task_ids = [row.task_id for row in notification_rows if row.task_id]
    comments = {
        row.id: row
        for row in TaskComment.query.filter(TaskComment.id.in_(comment_ids)).all()
    } if comment_ids else {}
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
    for row in notification_rows:
        task = tasks.get(row.task_id)
        comment = comments.get(row.comment_id)
        project = projects.get(task.project_id) if task else None
        preview = ""
        if row.kind == "comment":
            if comment and comment.body:
                preview = comment.body[:120] + ("..." if len(comment.body) > 120 else "")
            else:
                preview = "New comment"
        elif row.kind == "subtask_update":
            preview = "Subtask updated"
        elif row.kind == "task_moved":
            preview = "Task moved"
        elif row.kind == "task_created":
            preview = "Task created"
        else:
            preview = "Task updated"
        notifications.append(
            {
                "id": row.id,
                "task_id": row.task_id,
                "task_title": task.title if task else "Task",
                "project_id": project.id if project else None,
                "project_name": project.name if project else None,
                "kind": row.kind,
                "preview": preview,
                "comment_preview": preview,
                "created_at": row.created_at.isoformat(),
            }
        )
    return {
        "unread_total": len(notification_rows),
        "unread_task_ids": unread_task_ids,
        "unread_comment_task_ids": unread_comment_task_ids,
        "notifications": notifications,
    }


def emit_notification_state(user_id: int) -> None:
    socketio.emit("notification_state", notification_payload_for_user(user_id), room=user_room(user_id))


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
        "subtask_id": assignment.subtask_id,
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
    from app.models import User
    project = Project.query.get(project_id)
    if not project:
        return {"project_id": project_id, "members": []}
    member_rows = ProjectMember.query.filter_by(project_id=project_id).all()
    users = [User.query.get(project.owner_id)]
    users.extend(User.query.get(row.user_id) for row in member_rows)
    deduped = []
    seen = set()
    for user in users:
        if not user or user.id in seen:
            continue
        seen.add(user.id)
        deduped.append(user)
    return {"project_id": project_id, "members": _serialize_members(deduped)}


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
    deduped = []
    seen = set()
    for user in users:
        if not user or user.id in seen:
            continue
        seen.add(user.id)
        deduped.append(user)
    return {"group_id": group_id, "project_id": group.project_id, "members": _serialize_members(deduped)}


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


def _serialize_task_payload(task: Task, *, action: str = "updated", old_project_id: int | None = None, old_group_id: int | None = None, actor_user_id: int | None = None) -> dict:
    return {
        "action": action,
        "actor_user_id": actor_user_id,
        "task": {
            "id": task.id,
            "project_id": task.project_id,
            "group_id": task.group_id,
            "title": task.title,
            "due_at": task.due_at.isoformat() if task.due_at else None,
            "status": task.status,
            "created_at": task.created_at.isoformat() if task.created_at else None,
        },
        "old_project_id": old_project_id,
        "old_group_id": old_group_id,
    }


def _serialize_subtask_payload(task: Task, subtask: Subtask, *, action: str = "updated", actor_user_id: int | None = None) -> dict:
    return {
        "action": action,
        "actor_user_id": actor_user_id,
        "task": {
            "id": task.id,
            "project_id": task.project_id,
            "group_id": task.group_id,
        },
        "subtask": {
            "id": subtask.id,
            "task_id": subtask.task_id,
            "title": subtask.title,
            "due_at": subtask.due_at.isoformat() if subtask.due_at else None,
            "status": subtask.status,
            "created_at": subtask.created_at.isoformat() if subtask.created_at else None,
        },
    }


def emit_task_updated(task: Task, *, action: str = "updated", old_project_id: int | None = None, old_group_id: int | None = None, actor_user_id: int | None = None) -> None:
    payload = _serialize_task_payload(task, action=action, old_project_id=old_project_id, old_group_id=old_group_id, actor_user_id=actor_user_id)
    socketio.emit("task_updated", payload, room=project_room(task.project_id))
    if old_project_id and old_project_id != task.project_id:
        socketio.emit("task_updated", payload, room=project_room(old_project_id))
    for recipient_id in _task_notification_user_ids(task):
        socketio.emit("task_updated", payload, room=user_room(recipient_id))


def emit_subtask_updated(task: Task, subtask: Subtask, *, action: str = "updated", actor_user_id: int | None = None) -> None:
    payload = _serialize_subtask_payload(task, subtask, action=action, actor_user_id=actor_user_id)
    socketio.emit("subtask_updated", payload, room=project_room(task.project_id))
    for recipient_id in _task_notification_user_ids(task):
        socketio.emit("subtask_updated", payload, room=user_room(recipient_id))


def queue_task_notifications(task: Task, *, exclude_user_id: int | None = None, kind: str = "task_update") -> None:
    now = datetime.utcnow()
    for recipient_id in _task_notification_user_ids(task):
        if exclude_user_id and recipient_id == exclude_user_id:
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
        join_room(task_room(task_id))
        emit("task_room_joined", {"task_id": task_id})

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
            or db.session.query(Subtask.id)
            .join(Task, Task.id == Subtask.task_id)
            .join(Assignment, Assignment.subtask_id == Subtask.id)
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
        leave_room(task_room(task_id))
        emit("task_room_left", {"task_id": task_id})

    @socketio.on("disconnect")
    def handle_disconnect():
        sid = request.sid
        user_id = _sid_user.pop(sid, None)
        project_ids = list(_sid_projects.pop(sid, set()))
        if user_id is None:
            return
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
            emit_project_presence(project_id)
