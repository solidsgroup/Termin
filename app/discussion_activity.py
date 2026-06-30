from datetime import datetime

from app.extensions import db
from app.models import (
    Assignment,
    CollaboratorProfile,
    Group,
    Project,
    Task,
    TaskComment,
    TaskNotification,
    User,
    UserDiscussionActivity,
)
from app.team_shares import project_access_user_ids
from app.utils import display_name_for_user


def task_discussion_user_ids(task: Task) -> set[int]:
    user_ids: set[int] = set()
    if not task:
        return user_ids
    if task.creator_user_id:
        user_ids.add(int(task.creator_user_id))
    user_ids.update(
        int(row.user_id)
        for row in Assignment.query.filter_by(task_id=task.id).all()
        if row.user_id
    )
    return {user_id for user_id in user_ids if user_id}


def project_discussion_user_ids(project_id: int) -> set[int]:
    return project_access_user_ids(project_id)


def group_discussion_user_ids(group_id: int) -> set[int]:
    group = Group.query.get(group_id)
    if not group:
        return set()
    return project_discussion_user_ids(group.project_id)


def upsert_discussion_activity(
    *,
    user_ids: set[int] | list[int],
    entity_type: str,
    entity_id: int,
    comment_id: int,
    task_id: int | None = None,
    project_id: int | None = None,
    group_id: int | None = None,
    actor_user_id: int | None = None,
    actor_collaborator_id: int | None = None,
    preview: str = "",
    exclude_user_id: int | None = None,
) -> list[UserDiscussionActivity]:
    normalized_ids = sorted(
        {
            int(user_id)
            for user_id in (user_ids or [])
            if user_id and (exclude_user_id is None or int(user_id) != int(exclude_user_id))
        }
    )
    if not normalized_ids:
        return []
    existing_rows = UserDiscussionActivity.query.filter(
        UserDiscussionActivity.user_id.in_(normalized_ids),
        UserDiscussionActivity.entity_type == entity_type,
        UserDiscussionActivity.entity_id == entity_id,
    ).all()
    rows_by_user_id = {int(row.user_id): row for row in existing_rows if row.user_id}
    now = datetime.utcnow()
    updated_rows: list[UserDiscussionActivity] = []
    for user_id in normalized_ids:
        row = rows_by_user_id.get(user_id)
        if not row:
            row = UserDiscussionActivity(
                user_id=user_id,
                entity_type=entity_type,
                entity_id=entity_id,
                created_at=now,
            )
            db.session.add(row)
        row.task_id = task_id
        row.project_id = project_id
        row.group_id = group_id
        row.last_comment_id = comment_id
        row.last_actor_user_id = actor_user_id
        row.last_actor_collaborator_id = actor_collaborator_id
        row.last_preview = preview or ""
        row.updated_at = now
        updated_rows.append(row)
    db.session.flush()
    return updated_rows


def build_discussion_activity_items(rows: list[UserDiscussionActivity], *, user_id: int) -> list[dict]:
    if not rows:
        return []
    task_ids = {row.task_id for row in rows if row.task_id}
    project_ids = {row.project_id for row in rows if row.project_id}
    group_ids = {row.group_id for row in rows if row.group_id}
    actor_user_ids = {row.last_actor_user_id for row in rows if row.last_actor_user_id}
    actor_collaborator_ids = {row.last_actor_collaborator_id for row in rows if row.last_actor_collaborator_id}

    tasks = {row.id: row for row in Task.query.filter(Task.id.in_(task_ids)).all()} if task_ids else {}
    projects = {row.id: row for row in Project.query.filter(Project.id.in_(project_ids)).all()} if project_ids else {}
    groups = {row.id: row for row in Group.query.filter(Group.id.in_(group_ids)).all()} if group_ids else {}
    actor_users = {row.id: row for row in User.query.filter(User.id.in_(actor_user_ids)).all()} if actor_user_ids else {}
    actor_collaborators = {
        row.id: row
        for row in CollaboratorProfile.query.filter(CollaboratorProfile.id.in_(actor_collaborator_ids)).all()
    } if actor_collaborator_ids else {}
    unread_comment_counts_by_task = {}
    task_ids_for_unread = sorted({int(task_id) for task_id in task_ids if task_id})
    if task_ids_for_unread:
        unread_rows = (
            db.session.query(TaskNotification.task_id, db.func.count(TaskNotification.id))
            .filter(
                TaskNotification.user_id == user_id,
                TaskNotification.kind == "comment",
                TaskNotification.task_id.in_(task_ids_for_unread),
                TaskNotification.read_at.is_(None),
            )
            .group_by(TaskNotification.task_id)
            .all()
        )
        unread_comment_counts_by_task = {
            int(task_id): int(count or 0)
            for task_id, count in unread_rows
            if task_id
        }

    items = []
    for row in rows:
        task = tasks.get(row.task_id) if row.task_id else None
        project = projects.get(row.project_id) if row.project_id else (projects.get(task.project_id) if task and task.project_id else None)
        group = groups.get(row.group_id) if row.group_id else (groups.get(task.group_id) if task and task.group_id else None)
        actor_user = actor_users.get(row.last_actor_user_id) if row.last_actor_user_id else None
        actor_collaborator = actor_collaborators.get(row.last_actor_collaborator_id) if row.last_actor_collaborator_id else None
        actor_name = (
            display_name_for_user(actor_user)
            if actor_user
            else ((actor_collaborator.display_name or actor_collaborator.email) if actor_collaborator else "")
        ) or "New message"
        actor_avatar_url = actor_user.avatar_url if actor_user and actor_user.avatar_url else ""
        if row.entity_type == "task":
            title = task.title if task else "Task"
        elif row.entity_type == "project":
            title = "Project chat"
        else:
            title = (group.name if group else "Group") + " chat"
        items.append({
            "id": "discussion:" + row.entity_type + ":" + str(row.entity_id),
            "task_id": row.task_id or "",
            "project_id": row.project_id or (project.id if project else ""),
            "project_name": project.name if project else "",
            "group_id": row.group_id or "",
            "group_name": group.name if group else "",
            "task_title": title,
            "sender_name": actor_name,
            "detail_payload": {
                "actor_name": actor_name,
                "actor_avatar_url": actor_avatar_url,
                "actor_user_id": row.last_actor_user_id or "",
                "actor_collaborator_id": row.last_actor_collaborator_id or "",
            },
            "summary": actor_name + " commented",
            "preview": row.last_preview or "New comment",
            "inbox_preview": row.last_preview or "New comment",
            "comment_preview": row.last_preview or "New comment",
            "unread_count": unread_comment_counts_by_task.get(int(row.task_id), 0) if row.task_id else 0,
            "created_at": row.updated_at,
            "created_at_iso": row.updated_at.isoformat() if row.updated_at else "",
            "read": not bool(unread_comment_counts_by_task.get(int(row.task_id), 0)) if row.task_id else True,
            "pinned": False,
            "kind": "comment",
        })
    return items
