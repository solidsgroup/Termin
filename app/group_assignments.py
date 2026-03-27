from app.extensions import db
from app.models import Assignment, Project, ProjectMember, Task, User


def group_assignment_candidate_users(task: Task) -> list[User]:
    if not task or not task.project_id:
        return []
    user_ids: list[int] = []
    project = Project.query.get(task.project_id)
    if project and project.owner_id:
        user_ids.append(int(project.owner_id))
    user_ids.extend(int(row.user_id) for row in ProjectMember.query.filter_by(project_id=task.project_id).all() if row.user_id)
    seen: set[int] = set()
    users: list[User] = []
    for user_id in user_ids:
        if user_id in seen:
            continue
        seen.add(user_id)
        member_user = User.query.get(user_id)
        if member_user and member_user.email:
            users.append(member_user)
    return users


def serialize_group_assignment_members(task: Task) -> list[dict]:
    return [
        {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
        }
        for user in group_assignment_candidate_users(task)
    ]


def sync_group_task_assignments(task: Task) -> dict:
    candidate_users = group_assignment_candidate_users(task)
    candidate_ids = {user.id for user in candidate_users}
    existing_rows = Assignment.query.filter_by(task_id=task.id).all()
    rows_by_user_id: dict[int, Assignment] = {}
    created: list[Assignment] = []
    deleted: list[Assignment] = []

    for row in existing_rows:
        if row.user_id and row.user_id in rows_by_user_id:
            deleted.append(row)
            continue
        if row.user_id:
            rows_by_user_id[row.user_id] = row
        # Keep explicit email collaborators; group mode only manages account users.
        if row.user_id is not None and row.user_id not in candidate_ids:
            deleted.append(row)

    for user in candidate_users:
        if user.id in rows_by_user_id:
            continue
        row = Assignment(task_id=task.id, user_id=user.id, email=None, status="assigned")
        db.session.add(row)
        db.session.flush()
        created.append(row)

    for row in deleted:
        db.session.delete(row)

    return {"created": created, "deleted": deleted}
