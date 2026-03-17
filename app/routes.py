from datetime import datetime

from flask import Blueprint, request

from app.auth import login_required
from app.extensions import db
import secrets

from app.models import Task, Project, Subtask, Assignment, Invite, User, Group, ProjectMember, GroupMember
from app.utils import current_user


api_bp = Blueprint("api", __name__)


@api_bp.get("/me")
@login_required
def me():
    user = current_user()
    return {"user": {"id": user.id, "email": user.email, "display_name": user.display_name}}


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


def _can_manage_project(user, project_id: int) -> bool:
    return Project.query.filter_by(id=project_id, owner_id=user.id).first() is not None or (
        ProjectMember.query.filter_by(project_id=project_id, user_id=user.id).first() is not None
    )


@api_bp.post("/tasks")
@login_required
def create_task():
    payload = request.get_json(silent=True) or {}
    print("DEBUG /api/tasks payload:", payload)
    user = current_user()
    title = (payload.get("title") or "").strip()
    link = (payload.get("link") or "").strip()
    project_id = payload.get("project_id")
    group_id = payload.get("group_id")
    due_at = payload.get("due_at")
    link = payload.get("link")
    link = payload.get("link")
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
        title=title,
        description=payload.get("description"),
        link=link.strip() if link else None,
        due_at=due_value,
        owner_calendar_opt_in=project.default_owner_calendar_opt_in,
    )
    db.session.add(task)
    db.session.commit()
    assignment_payload = None
    if assignee_email:
        email = assignee_email.strip().lower()
        account_user = User.query.filter(User.email.ilike(email)).first()
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

    return {"id": task.id, "title": task.title, "assignment": assignment_payload}, 201


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
    link = payload.get("link")

    if title is not None:
        task.title = title.strip()
    if status is not None:
        task.status = status.strip() or task.status
    if link is not None:
        task.link = link.strip()
    if due_at is not None:
        if due_at == "":
            task.due_at = None
        else:
            try:
                task.due_at = datetime.fromisoformat(due_at)
            except ValueError:
                return {"error": "Invalid due_at format"}, 400
    db.session.commit()
    return {"id": task.id, "title": task.title, "status": task.status}, 200


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
    if name is None:
        return {"error": "name is required"}, 400
    if not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    project.name = name.strip() or project.name
    db.session.commit()
    return {"id": project.id, "name": project.name}, 200


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
    Group.query.filter(Group.project_id == project.id).delete(synchronize_session=False)
    db.session.delete(project)
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
        default_owner_calendar_opt_in=project.default_owner_calendar_opt_in,
        default_invitee_calendar_opt_in=project.default_invitee_calendar_opt_in,
    )
    db.session.add(copy)
    db.session.flush()

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
    for task in Task.query.filter_by(project_id=project.id).all():
        new_task = Task(
            project_id=copy.id,
            group_id=group_map.get(task.group_id),
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
    for task in Task.query.filter_by(group_id=group.id).all():
        new_task = Task(
            project_id=task.project_id,
            group_id=new_group.id,
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
    users = (
        User.query.filter(
            (User.email.ilike(f"%{q}%")) | (User.display_name.ilike(f"%{q}%"))
        )
        .limit(10)
        .all()
    )
    results = []
    for u in users:
        if u.id == user.id:
            continue
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

    account_user = User.query.filter(User.email.ilike(email)).first()
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
        )
        db.session.add(invite)
    assignment.status = "link_sent"
    db.session.commit()

    return {"status": "link_sent", "invite_url": f"{request.host_url.rstrip('/')}/invites/{invite.token}"}, 200


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
        link=link or None,
        due_at=due_at,
    )
    db.session.add(subtask)
    db.session.commit()
    assignment_payload = None
    if payload.get("assignee_email"):
        email = payload.get("assignee_email").strip().lower()
        account_user = User.query.filter(User.email.ilike(email)).first()
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
    return {"id": subtask.id, "title": subtask.title, "assignment": assignment_payload}, 201


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
    link = payload.get("link")

    if title is not None:
        subtask.title = title.strip()
    if status is not None:
        subtask.status = status.strip() or subtask.status
    if link is not None:
        subtask.link = link.strip()
    if due_at is not None:
        if due_at == "":
            subtask.due_at = None
        else:
            try:
                subtask.due_at = datetime.fromisoformat(due_at)
            except ValueError:
                return {"error": "Invalid due_at format"}, 400
    db.session.commit()
    return {"id": subtask.id, "title": subtask.title, "status": subtask.status}, 200


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
    users = (
        User.query.filter(
            (User.email.ilike(f"%{q}%")) | (User.display_name.ilike(f"%{q}%"))
        )
        .limit(10)
        .all()
    )

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
