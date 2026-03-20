from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, request, send_from_directory

from app.auth import login_required
from app.collaborators import get_or_create_collaborator_profile
from app.emailer import MailDeliveryError, send_magic_link_digest_email, send_magic_link_email
from app.extensions import db
from app.github_sync import GitHubSyncError, github_identity_for_user, should_sync_github_issues, sync_github_issues_for_user
from app.identity import find_user_by_email, search_users_by_identity
from app.info_utils import load_info_payload, normalize_info_payload, save_uploaded_file
from app.sidebar_layout import (
    apply_top_level_project_order,
    ensure_sidebar_preference,
    insert_project_position,
    resequence_division_projects,
    resequence_top_level_items,
    sidebar_preference_map,
    top_level_sidebar_items,
)
import secrets

from app.models import Division, Task, Project, ProjectSidebarPreference, Subtask, Assignment, Invite, User, Group, ProjectMember, GroupMember, TaskComment, TaskNotification, CollaboratorProfile
from app.utils import current_user


api_bp = Blueprint("api", __name__)


@api_bp.get("/me")
@login_required
def me():
    user = current_user()
    return {"user": {"id": user.id, "email": user.email, "display_name": user.display_name}}


@api_bp.get("/notifications")
@login_required
def notifications_feed():
    user = current_user()
    return _notification_payload_for_user(user.id)


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
    if Project.query.filter_by(id=task.project_id, owner_id=user.id).first():
        return True
    if ProjectMember.query.filter_by(project_id=task.project_id, user_id=user.id).first():
        return True
    if task.group_id and GroupMember.query.filter_by(group_id=task.group_id, user_id=user.id).first():
        return True
    return Assignment.query.filter_by(task_id=task.id, user_id=user.id).first() is not None


def _can_access_subtask(user, subtask: Subtask) -> bool:
    if not subtask:
        return False
    if Assignment.query.filter_by(subtask_id=subtask.id, user_id=user.id).first():
        return True
    task = Task.query.get(subtask.task_id)
    return _can_access_task(user, task)


def _task_notification_user_ids(task: Task) -> set[int]:
    project = Project.query.get(task.project_id)
    user_ids = {project.owner_id} if project else set()
    user_ids.update(row.user_id for row in ProjectMember.query.filter_by(project_id=task.project_id).all())
    if task.group_id:
        user_ids.update(row.user_id for row in GroupMember.query.filter_by(group_id=task.group_id).all())
    user_ids.update(
        row.user_id
        for row in Assignment.query.filter_by(task_id=task.id).all()
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
        label = (author.display_name or author.email) if author else "Unknown user"
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
        "created_at": comment.created_at.isoformat(),
        "updated_at": comment.updated_at.isoformat(),
        "author": author_payload,
    }


def _notification_payload_for_user(user_id: int) -> dict:
    notification_rows = (
        TaskNotification.query.filter_by(user_id=user_id)
        .filter(TaskNotification.read_at.is_(None))
        .order_by(TaskNotification.created_at.desc(), TaskNotification.id.desc())
        .limit(12)
        .all()
    )
    unread_task_ids = sorted({row.task_id for row in notification_rows if row.task_id})
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
        if comment and comment.body:
            preview = comment.body[:120] + ("…" if len(comment.body) > 120 else "")
        notifications.append(
            {
                "id": row.id,
                "task_id": row.task_id,
                "task_title": task.title if task else "Task",
                "project_id": project.id if project else None,
                "project_name": project.name if project else None,
                "comment_preview": preview,
                "created_at": row.created_at.isoformat(),
            }
        )
    return {
        "unread_total": len(notification_rows),
        "unread_task_ids": unread_task_ids,
        "notifications": notifications,
    }


def _can_manage_project(user, project_id: int) -> bool:
    return Project.query.filter_by(id=project_id, owner_id=user.id).first() is not None or (
        ProjectMember.query.filter_by(project_id=project_id, user_id=user.id).first() is not None
    )


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
    if (
        db.session.query(Task.id)
        .join(Assignment, Assignment.task_id == Task.id)
        .filter(Task.project_id == project_id, Assignment.user_id == user.id)
        .first()
        is not None
    ):
        return True
    return (
        db.session.query(Subtask.id)
        .join(Assignment, Assignment.subtask_id == Subtask.id)
        .join(Task, Task.id == Subtask.task_id)
        .filter(Task.project_id == project_id, Assignment.user_id == user.id)
        .first()
        is not None
    )


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
    assigned_task_project_ids = [
        row.project_id
        for row in db.session.query(Task.project_id)
        .join(Assignment, Assignment.task_id == Task.id)
        .filter(Assignment.user_id == user.id)
        .all()
    ]
    assigned_subtask_project_ids = [
        row.project_id
        for row in db.session.query(Task.project_id)
        .join(Subtask, Subtask.task_id == Task.id)
        .join(Assignment, Assignment.subtask_id == Subtask.id)
        .filter(Assignment.user_id == user.id)
        .all()
    ]
    accessible_project_ids = list(
        {project.id for project in owned_projects}
        .union(member_project_ids)
        .union(member_group_project_ids)
        .union(assigned_task_project_ids)
        .union(assigned_subtask_project_ids)
    )
    return Project.query.filter(Project.id.in_(accessible_project_ids)).all() if accessible_project_ids else []


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
    subtask_id: int | None = None,
    account_user_id: int | None = None,
    email: str | None = None,
) -> Assignment | None:
    query = Assignment.query
    if task_id is None:
        query = query.filter(Assignment.task_id.is_(None))
    else:
        query = query.filter_by(task_id=task_id)
    if subtask_id is None:
        query = query.filter(Assignment.subtask_id.is_(None))
    else:
        query = query.filter_by(subtask_id=subtask_id)

    normalized_email = (email or "").strip().lower()
    if account_user_id:
        existing = query.filter_by(user_id=account_user_id).first()
        if existing:
            return existing
    if normalized_email:
        return query.filter(db.func.lower(Assignment.email) == normalized_email).first()
    return None


@api_bp.post("/tasks")
@login_required
def create_task():
    payload = request.get_json(silent=True) or {}
    user = current_user()
    title = (payload.get("title") or "").strip()
    project_id = payload.get("project_id")
    group_id = payload.get("group_id")
    due_at = payload.get("due_at")
    assignee_email = payload.get("assignee_email")

    if not title or not project_id:
        return {"error": "title and project_id are required"}, 400

    if not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404

    group = None
    if group_id:
        group = Group.query.filter_by(id=group_id, project_id=project.id).first()
        if not group:
            return {"error": "group not found"}, 404
    else:
        group = Group.query.filter_by(project_id=project.id).first()

    due_value = None
    if due_at:
        try:
            due_value = datetime.fromisoformat(due_at)
        except ValueError:
            return {"error": "Invalid due_at format"}, 400

    task = Task(
        project_id=project.id,
        group_id=group.id if group else None,
        position=_next_task_position(project.id, group.id if group else None),
        title=title,
        description=payload.get("description"),
        info=normalize_info_payload(payload.get("info"), payload.get("link")),
        due_at=due_value,
        owner_calendar_opt_in=project.default_owner_calendar_opt_in,
    )
    db.session.add(task)
    db.session.commit()
    assignment_payload = None
    if assignee_email:
        email = assignee_email.strip().lower()
        account_user = find_user_by_email(email)
        existing_assignment = _existing_assignment_for_target(
            task_id=task.id,
            account_user_id=account_user.id if account_user else None,
            email=email,
        )
        if existing_assignment:
            return {
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

    return {"id": task.id, "title": task.title, "assignment": assignment_payload, "info": _info_payload_for(task)}, 201


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

    title = payload.get("title")
    status = payload.get("status")
    due_at = payload.get("due_at")
    info = payload.get("info")

    if title is not None:
        task.title = title.strip()
    if status is not None:
        task.status = status.strip() or task.status
    if info is not None:
        task.info = normalize_info_payload(info, task.link)
    if due_at is not None:
        if due_at == "":
            task.due_at = None
        else:
            try:
                task.due_at = datetime.fromisoformat(due_at)
            except ValueError:
                return {"error": "Invalid due_at format"}, 400
    db.session.commit()
    return {"id": task.id, "title": task.title, "status": task.status, "info": _info_payload_for(task)}, 200


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

    for recipient_id in _task_notification_user_ids(task):
        if recipient_id == user.id:
            continue
        db.session.add(
            TaskNotification(
                user_id=recipient_id,
                task_id=task.id,
                comment_id=comment.id,
                kind="comment",
            )
        )

    db.session.commit()
    return {"comment": _serialize_task_comment(comment, user)}, 201


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
    TaskNotification.query.filter_by(user_id=user.id, task_id=task.id).filter(TaskNotification.read_at.is_(None)).update(
        {"read_at": now},
        synchronize_session=False,
    )
    db.session.commit()
    unread_total = TaskNotification.query.filter_by(user_id=user.id).filter(TaskNotification.read_at.is_(None)).count()
    return {"status": "ok", "unread_total": unread_total}


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

    existing = TaskNotification.query.filter_by(user_id=user.id, task_id=task.id).filter(TaskNotification.read_at.is_(None)).first()
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

    unread_total = TaskNotification.query.filter_by(user_id=user.id).filter(TaskNotification.read_at.is_(None)).count()
    return {"status": "ok", "unread_total": unread_total}


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
    if not _can_manage_project(user, source_project_id) or not _can_manage_project(user, target_project_id):
        return {"error": "unauthorized"}, 403

    target_project = Project.query.get(target_project_id)
    if not target_project:
        return {"error": "project not found"}, 404

    target_group_id = None if target_group_raw in (None, "", "null") else int(target_group_raw)
    if target_group_id is not None:
        target_group = Group.query.filter_by(id=target_group_id, project_id=target_project.id).first()
        if not target_group:
            return {"error": "group not found"}, 404

    before_task = None
    if before_task_id:
        before_task = Task.query.get(before_task_id)
        if not before_task:
            return {"error": "before_task not found"}, 404
        if before_task.id == task.id:
            before_task = None
        elif before_task.project_id != target_project.id or before_task.group_id != target_group_id:
            return {"error": "before_task bucket mismatch"}, 400

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
    if name is None:
        return {"error": "name is required"}, 400

    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404

    if not _can_manage_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    group.name = name.strip() or group.name
    db.session.commit()
    return {"id": group.id, "name": group.name}, 200


@api_bp.patch("/projects/<int:project_id>")
@login_required
def update_project(project_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()
    name = payload.get("name")
    division_id = payload.get("division_id", "__missing__")
    if name is None and division_id == "__missing__":
        return {"error": "name or division_id is required"}, 400
    if name is not None and not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    if division_id != "__missing__" and not _can_access_project(user, project_id):
        return {"error": "unauthorized"}, 403
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if name is not None:
        project.name = name.strip() or project.name
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
    db.session.commit()
    current_pref = ProjectSidebarPreference.query.filter_by(user_id=user.id, project_id=project.id).first()
    return {"id": project.id, "name": project.name, "division_id": current_pref.division_id if current_pref else None}, 200


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
    return {"status": "ok", "id": project.id, "division_id": moving_pref.division_id, "position": moving_pref.position}, 200


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

    group_ids = [g.id for g in Group.query.filter_by(project_id=project.id).all()]
    task_ids = [t.id for t in Task.query.filter_by(project_id=project.id).all()]
    subtask_ids = [s.id for s in Subtask.query.filter(Subtask.task_id.in_(task_ids)).all()] if task_ids else []
    Assignment.query.filter(Assignment.task_id.in_(task_ids)).delete(synchronize_session=False)
    Assignment.query.filter(Assignment.subtask_id.in_(subtask_ids)).delete(synchronize_session=False)
    Invite.query.filter(Invite.task_id.in_(task_ids)).delete(synchronize_session=False)
    Invite.query.filter(Invite.subtask_id.in_(subtask_ids)).delete(synchronize_session=False)
    Subtask.query.filter(Subtask.task_id.in_(task_ids)).delete(synchronize_session=False)
    Task.query.filter(Task.project_id == project.id).delete(synchronize_session=False)
    GroupMember.query.filter(GroupMember.group_id.in_(group_ids)).delete(synchronize_session=False)
    ProjectMember.query.filter(ProjectMember.project_id == project.id).delete(synchronize_session=False)
    ProjectSidebarPreference.query.filter_by(project_id=project.id).delete(synchronize_session=False)
    Group.query.filter(Group.project_id == project.id).delete(synchronize_session=False)
    db.session.delete(project)
    db.session.commit()
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

    for subtask in Subtask.query.filter(Subtask.task_id.in_(task_map.keys())).all():
        new_subtask = Subtask(
            task_id=task_map.get(subtask.task_id),
            title=subtask.title,
            due_at=subtask.due_at,
            status=subtask.status,
        )
        db.session.add(new_subtask)

    db.session.commit()
    return {"id": copy.id, "name": copy.name}, 201


@api_bp.delete("/groups/<int:group_id>")
@login_required
def delete_group(group_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if group.project_id is None or not _can_manage_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    task_ids = [t.id for t in Task.query.filter_by(group_id=group.id).all()]
    subtask_ids = [s.id for s in Subtask.query.filter(Subtask.task_id.in_(task_ids)).all()] if task_ids else []
    Assignment.query.filter(Assignment.task_id.in_(task_ids)).delete(synchronize_session=False)
    Assignment.query.filter(Assignment.subtask_id.in_(subtask_ids)).delete(synchronize_session=False)
    Invite.query.filter(Invite.task_id.in_(task_ids)).delete(synchronize_session=False)
    Invite.query.filter(Invite.subtask_id.in_(subtask_ids)).delete(synchronize_session=False)
    Subtask.query.filter(Subtask.task_id.in_(task_ids)).delete(synchronize_session=False)
    Task.query.filter(Task.group_id == group.id).delete(synchronize_session=False)
    GroupMember.query.filter(GroupMember.group_id == group.id).delete(synchronize_session=False)
    db.session.delete(group)
    db.session.commit()
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

    for subtask in Subtask.query.filter(Subtask.task_id.in_(task_map.keys())).all():
        new_subtask = Subtask(
            task_id=task_map.get(subtask.task_id),
            title=subtask.title,
            due_at=subtask.due_at,
            status=subtask.status,
        )
        db.session.add(new_subtask)

    db.session.commit()
    return {"id": new_group.id, "name": new_group.name}, 201


@api_bp.get("/users")
@login_required
def list_users():
    user = current_user()
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return {"results": []}
    users = search_users_by_identity(q, exclude_user_id=user.id, limit=10)
    results = []
    for u in users:
        results.append(
            {
                "id": u.id,
                "email": u.email,
                "display_name": u.display_name,
                "avatar_url": u.avatar_url,
            }
        )
    return {"results": results}


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
    db.session.commit()
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
    existing = GroupMember.query.filter_by(group_id=group_id, user_id=user_id).first()
    if existing:
        return {"status": "ok"}, 200
    db.session.add(GroupMember(group_id=group_id, user_id=user_id))
    db.session.commit()
    return {"status": "ok"}, 201


@api_bp.get("/projects/<int:project_id>/members")
@login_required
def list_project_members(project_id: int):
    user = current_user()
    if not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    member_rows = ProjectMember.query.filter_by(project_id=project_id).all()
    members = []
    for row in member_rows:
        member_user = User.query.get(row.user_id)
        if member_user:
            members.append(
                {
                    "id": member_user.id,
                    "email": member_user.email,
                    "display_name": member_user.display_name,
                    "avatar_url": member_user.avatar_url,
                }
            )
    owner = User.query.get(project.owner_id)
    if owner:
        members.insert(
            0,
            {
                "id": owner.id,
                "email": owner.email,
                "display_name": owner.display_name,
                "avatar_url": owner.avatar_url,
                "owner": True,
            },
        )
    return {"members": members}, 200


@api_bp.get("/groups/<int:group_id>/members")
@login_required
def list_group_members(group_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_manage_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    project = Project.query.get(group.project_id)
    members = []
    if project:
        owner = User.query.get(project.owner_id)
        if owner:
            members.append(
                {
                    "id": owner.id,
                    "email": owner.email,
                    "display_name": owner.display_name,
                    "avatar_url": owner.avatar_url,
                    "owner": True,
                }
            )
    project_member_rows = ProjectMember.query.filter_by(project_id=group.project_id).all()
    for row in project_member_rows:
        member_user = User.query.get(row.user_id)
        if member_user:
            members.append(
                {
                    "id": member_user.id,
                    "email": member_user.email,
                    "display_name": member_user.display_name,
                    "avatar_url": member_user.avatar_url,
                    "project_member": True,
                }
            )
    group_member_rows = GroupMember.query.filter_by(group_id=group_id).all()
    for row in group_member_rows:
        member_user = User.query.get(row.user_id)
        if member_user:
            members.append(
                {
                    "id": member_user.id,
                    "email": member_user.email,
                    "display_name": member_user.display_name,
                    "avatar_url": member_user.avatar_url,
                    "group_member": True,
                }
            )
    # de-dup by user id preserving first occurrence
    seen = set()
    unique = []
    for m in members:
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        unique.append(m)
    return {"members": unique}, 200


@api_bp.delete("/projects/<int:project_id>/members/<int:user_id>")
@login_required
def remove_project_member(project_id: int, user_id: int):
    user = current_user()
    if not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    ProjectMember.query.filter_by(project_id=project_id, user_id=user_id).delete()
    db.session.commit()
    return {"status": "deleted"}, 200


@api_bp.delete("/groups/<int:group_id>/members/<int:user_id>")
@login_required
def remove_group_member(group_id: int, user_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_manage_project(user, group.project_id):
        return {"error": "unauthorized"}, 403
    GroupMember.query.filter_by(group_id=group_id, user_id=user_id).delete()
    db.session.commit()
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
    return {"status": "ok"}, 200


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

    subtask_ids = [s.id for s in Subtask.query.filter_by(task_id=task.id).all()]
    subtask_count = len(subtask_ids)
    invite_query = Invite.query.filter(
        (Invite.task_id == task.id) | (Invite.subtask_id.in_(subtask_ids))
    )
    invite_count = invite_query.filter(Invite.status != "draft").count()

    requires_confirm = subtask_count > 0 or invite_count > 0
    if request.args.get("confirm") != "1":
        if requires_confirm:
            return {
                "requires_confirm": True,
                "subtask_count": subtask_count,
                "invite_count": invite_count,
            }, 200
        # No confirm needed; proceed with delete

    Subtask.query.filter_by(task_id=task.id).delete()
    Assignment.query.filter_by(task_id=task.id).delete()
    Invite.query.filter_by(task_id=task.id).delete()
    db.session.delete(task)
    db.session.commit()
    return {"status": "deleted"}, 200


@api_bp.post("/assignments")
@login_required
def create_assignment():
    payload = request.get_json(silent=True) or {}
    user = current_user()
    target_type = payload.get("target_type")
    target_id = payload.get("target_id")
    email = (payload.get("email") or "").strip().lower()

    if target_type not in {"task", "subtask"} or not target_id or not email:
        return {"error": "target_type, target_id, and email are required"}, 400
    if "@" not in email:
        return {"error": "invalid email"}, 400

    task = None
    subtask = None
    if target_type == "task":
        task = Task.query.get(target_id)
        if not task:
            return {"error": "task not found"}, 404
        if not _can_access_task(user, task):
            return {"error": "unauthorized"}, 403
    else:
        subtask = Subtask.query.get(target_id)
        if not subtask:
            return {"error": "subtask not found"}, 404
        task = Task.query.get(subtask.task_id)
        if not task:
            return {"error": "task not found"}, 404
        if not _can_access_subtask(user, subtask):
            return {"error": "unauthorized"}, 403

    account_user = find_user_by_email(email)
    if not account_user:
        project = Project.query.get(task.project_id) if task else None
        get_or_create_collaborator_profile(
            email=email,
            default_calendar_opt_in=project.default_invitee_calendar_opt_in if project else False,
        )
    existing_assignment = _existing_assignment_for_target(
        task_id=task.id if target_type == "task" else None,
        subtask_id=subtask.id if target_type == "subtask" else None,
        account_user_id=account_user.id if account_user else None,
        email=email,
    )
    if existing_assignment:
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
        task_id=task.id if target_type == "task" else None,
        subtask_id=subtask.id if target_type == "subtask" else None,
        user_id=account_user.id if account_user else None,
        email=email if not account_user else None,
        status="assigned" if account_user else "draft",
    )
    db.session.add(assignment)
    db.session.commit()

    return {
        "id": assignment.id,
        "user_id": assignment.user_id,
        "email": assignment.email,
        "status": assignment.status,
        "display_name": account_user.display_name if account_user else None,
        "display_email": account_user.email if account_user else assignment.email,
        "avatar_url": account_user.avatar_url if account_user else None,
    }, 201


@api_bp.post("/assignments/<int:assignment_id>/send_link")
@login_required
def send_assignment_link(assignment_id: int):
    user = current_user()
    assignment = Assignment.query.get(assignment_id)
    if not assignment:
        return {"error": "assignment not found"}, 404

    task = Task.query.get(assignment.task_id) if assignment.task_id else None
    if assignment.subtask_id:
        subtask = Subtask.query.get(assignment.subtask_id)
        task = Task.query.get(subtask.task_id) if subtask else task
    if not task:
        return {"error": "task not found"}, 404

    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    email = assignment.email or (User.query.get(assignment.user_id).email if assignment.user_id else None)
    if not email:
        return {"error": "assignment has no email"}, 400

    subtask = Subtask.query.get(assignment.subtask_id) if assignment.subtask_id else None
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
            subtask_id=assignment.subtask_id,
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
            accept_url=accept_url,
            decline_url=decline_url,
            project_name=project.name if project else None,
            task_title=task.title if task else None,
            subtask_title=subtask.title if subtask else None,
            inviter_email=user.email,
            inviter_name=user.display_name or user.email,
        )
    except MailDeliveryError as exc:
        db.session.rollback()
        return {"error": f"email delivery failed: {exc}"}, 503

    invite.status = "sent"
    assignment.status = "link_sent"
    db.session.commit()

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
        subtask = Subtask.query.get(assignment.subtask_id) if assignment.subtask_id else None
        if subtask and not task:
            task = Task.query.get(subtask.task_id)
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
                subtask_id=assignment.subtask_id,
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
                "subtask_title": subtask.title if subtask else None,
                "invite_url": manage_url,
                "portal_url": manage_url,
                "accept_url": f"{base_url}/invites/{invite.token}/quick/accept",
                "decline_url": f"{base_url}/invites/{invite.token}/quick/decline",
                "manage_url": manage_url,
            }
        )
        prepared_assignments.append((assignment, invite))

    try:
        invite_id_list = ",".join(str(invite.id) for _assignment, invite in prepared_assignments)
        bulk_base_url = f"{base_url}/collaborators/{collaborator.access_token}/quick" if collaborator else ""
        send_magic_link_digest_email(
            to_email=first_email,
            manage_url=prepared_items[0].get("manage_url") if prepared_items else None,
            accept_all_url=f"{bulk_base_url}/accept?invites={invite_id_list}" if bulk_base_url and invite_id_list else None,
            decline_all_url=f"{bulk_base_url}/decline?invites={invite_id_list}" if bulk_base_url and invite_id_list else None,
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
    return {"status": "link_sent", "assignment_ids": [assignment.id for assignment, _ in prepared_assignments]}, 200


@api_bp.delete("/assignments/<int:assignment_id>")
@login_required
def delete_assignment(assignment_id: int):
    user = current_user()
    assignment = Assignment.query.get(assignment_id)
    if not assignment:
        return {"error": "assignment not found"}, 404

    task = Task.query.get(assignment.task_id) if assignment.task_id else None
    if assignment.subtask_id:
        subtask = Subtask.query.get(assignment.subtask_id)
        task = Task.query.get(subtask.task_id) if subtask else task
    if not task:
        return {"error": "task not found"}, 404

    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    Invite.query.filter_by(assignment_id=assignment.id).delete()
    db.session.delete(assignment)
    db.session.commit()
    return {"status": "deleted"}, 200


@api_bp.post("/tasks/<int:task_id>/subtasks")
@login_required
def create_subtask(task_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()

    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404

    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    title = (payload.get("title") or "").strip()
    if not title:
        return {"error": "title is required"}, 400

    due_at = None
    due_raw = payload.get("due_at")
    if due_raw:
        try:
            due_at = datetime.fromisoformat(due_raw)
        except ValueError:
            return {"error": "Invalid due_at format"}, 400

    subtask = Subtask(
        task_id=task.id,
        title=title,
        info=normalize_info_payload(payload.get("info"), payload.get("link")),
        due_at=due_at,
    )
    db.session.add(subtask)
    db.session.commit()
    assignment_payload = None
    if payload.get("assignee_email"):
        email = payload.get("assignee_email").strip().lower()
        account_user = find_user_by_email(email)
        existing_assignment = _existing_assignment_for_target(
            subtask_id=subtask.id,
            account_user_id=account_user.id if account_user else None,
            email=email,
        )
        if existing_assignment:
            return {
                "id": subtask.id,
                "title": subtask.title,
                "assignment": {
                    "id": existing_assignment.id,
                    "user_id": existing_assignment.user_id,
                    "email": existing_assignment.email,
                    "status": existing_assignment.status,
                    "display_name": account_user.display_name if account_user else None,
                    "display_email": account_user.email if account_user else existing_assignment.email,
                },
                "info": _info_payload_for(subtask),
            }, 200
        assignment = Assignment(
            subtask_id=subtask.id,
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
            "avatar_url": account_user.avatar_url if account_user else None,
        }
    return {"id": subtask.id, "title": subtask.title, "assignment": assignment_payload, "info": _info_payload_for(subtask)}, 201


@api_bp.patch("/subtasks/<int:subtask_id>")
@login_required
def update_subtask(subtask_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()

    subtask = Subtask.query.get(subtask_id)
    if not subtask:
        return {"error": "subtask not found"}, 404

    task = Task.query.get(subtask.task_id)
    if not task:
        return {"error": "task not found"}, 404

    if not _can_access_subtask(user, subtask):
        return {"error": "unauthorized"}, 403

    title = payload.get("title")
    status = payload.get("status")
    due_at = payload.get("due_at")
    info = payload.get("info")

    if title is not None:
        subtask.title = title.strip()
    if status is not None:
        subtask.status = status.strip() or subtask.status
    if info is not None:
        subtask.info = normalize_info_payload(info, subtask.link)
    if due_at is not None:
        if due_at == "":
            subtask.due_at = None
        else:
            try:
                subtask.due_at = datetime.fromisoformat(due_at)
            except ValueError:
                return {"error": "Invalid due_at format"}, 400
    db.session.commit()
    return {"id": subtask.id, "title": subtask.title, "status": subtask.status, "info": _info_payload_for(subtask)}, 200


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


@api_bp.post("/subtasks/<int:subtask_id>/info/attachments")
@login_required
def upload_subtask_attachment(subtask_id: int):
    user = current_user()
    subtask = Subtask.query.get(subtask_id)
    if not subtask:
        return {"error": "subtask not found"}, 404
    if not _can_access_subtask(user, subtask):
        return {"error": "unauthorized"}, 403
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return {"error": "file is required"}, 400
    attachment = save_uploaded_file(upload, _upload_root(), "subtask", subtask.id)
    info = _info_payload_for(subtask)
    info.setdefault("attachments", []).append(attachment)
    subtask.info = normalize_info_payload(info, subtask.link)
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


@api_bp.delete("/subtasks/<int:subtask_id>/info/attachments/<attachment_id>")
@login_required
def delete_subtask_attachment(subtask_id: int, attachment_id: str):
    user = current_user()
    subtask = Subtask.query.get(subtask_id)
    if not subtask:
        return {"error": "subtask not found"}, 404
    if not _can_access_subtask(user, subtask):
        return {"error": "unauthorized"}, 403
    info = _info_payload_for(subtask)
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
    subtask.info = normalize_info_payload(info, subtask.link)
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
    elif item_type == "subtask":
        item = Subtask.query.get(item_id)
        if not item or not _can_access_subtask(user, item):
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
    if not q:
        return {"results": []}

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
    assigned_subtask_ids = [
        a.subtask_id
        for a in Assignment.query.filter_by(user_id=user.id).filter(Assignment.subtask_id.isnot(None)).all()
    ]
    parent_task_ids = (
        [s.task_id for s in Subtask.query.filter(Subtask.id.in_(assigned_subtask_ids)).all()]
        if assigned_subtask_ids
        else []
    )
    task_ids = list(set(owned_task_ids + assigned_task_ids + parent_task_ids))
    subtask_ids = (
        [s.id for s in Subtask.query.filter(Subtask.task_id.in_(task_ids)).all()] if task_ids else []
    )

    # Users by email/display_name
    users = search_users_by_identity(q, limit=10)

    # Prior emails from assignments/invites within scope
    emails = set()
    if task_ids:
        for row in Assignment.query.filter(Assignment.task_id.in_(task_ids)).all():
            if row.email:
                emails.add(row.email)
    if subtask_ids:
        for row in Assignment.query.filter(Assignment.subtask_id.in_(subtask_ids)).all():
            if row.email:
                emails.add(row.email)
    if task_ids or subtask_ids:
        invite_rows = Invite.query.filter(
            (Invite.task_id.in_(task_ids)) | (Invite.subtask_id.in_(subtask_ids))
        ).all()
        for row in invite_rows:
            if row.email:
                emails.add(row.email)

    filtered_emails = [e for e in emails if q in e.lower()]

    # Count accepts per email
    accept_counts = {}
    if task_ids or subtask_ids:
        invite_rows = Invite.query.filter(
            ((Invite.task_id.in_(task_ids)) | (Invite.subtask_id.in_(subtask_ids)))
            & (Invite.status == "accepted")
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
                "accepted_count": accept_counts.get(u.email, 0),
            }
        )
    for e in filtered_emails[:10]:
        results.append(
            {
                "type": "email",
                "email": e,
                "accepted_count": accept_counts.get(e, 0),
            }
        )

    return {"results": results}
