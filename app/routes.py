from datetime import datetime

from flask import Blueprint, request

from app.auth import login_required
from app.extensions import db
import secrets

from app.models import Task, Project, Subtask, Assignment, Invite, User
from app.utils import current_user


api_bp = Blueprint("api", __name__)


@api_bp.get("/me")
@login_required
def me():
    user = current_user()
    return {"user": {"id": user.id, "email": user.email, "display_name": user.display_name}}


@api_bp.post("/tasks")
@login_required
def create_task():
    payload = request.get_json(silent=True) or {}
    print("DEBUG /api/tasks payload:", payload)
    user = current_user()
    title = (payload.get("title") or "").strip()
    project_id = payload.get("project_id")
    due_at = payload.get("due_at")
    assignee_email = payload.get("assignee_email")

    if not title or not project_id:
        return {"error": "title and project_id are required"}, 400

    project = Project.query.filter_by(id=project_id, owner_id=user.id).first()
    if not project:
        return {"error": "project not found"}, 404

    due_value = None
    if due_at:
        try:
            due_value = datetime.fromisoformat(due_at)
        except ValueError:
            return {"error": "Invalid due_at format"}, 400

    task = Task(
        project_id=project.id,
        title=title,
        description=payload.get("description"),
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

    project = Project.query.filter_by(id=task.project_id, owner_id=user.id).first()
    if not project:
        return {"error": "unauthorized"}, 403

    title = payload.get("title")
    status = payload.get("status")
    due_at = payload.get("due_at")

    if title is not None:
        task.title = title.strip()
    if status is not None:
        task.status = status.strip() or task.status
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


@api_bp.delete("/tasks/<int:task_id>")
@login_required
def delete_task(task_id: int):
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404

    project = Project.query.filter_by(id=task.project_id, owner_id=user.id).first()
    if not project:
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
        project = Project.query.filter_by(id=task.project_id, owner_id=user.id).first()
        if not project:
            return {"error": "unauthorized"}, 403
    else:
        subtask = Subtask.query.get(target_id)
        if not subtask:
            return {"error": "subtask not found"}, 404
        task = Task.query.get(subtask.task_id)
        if not task:
            return {"error": "task not found"}, 404
        project = Project.query.filter_by(id=task.project_id, owner_id=user.id).first()
        if not project:
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

    project = Project.query.filter_by(id=task.project_id, owner_id=user.id).first()
    if not project:
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

    project = Project.query.filter_by(id=task.project_id, owner_id=user.id).first()
    if not project:
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

    project = Project.query.filter_by(id=task.project_id, owner_id=user.id).first()
    if not project:
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

    project = Project.query.filter_by(id=task.project_id, owner_id=user.id).first()
    if not project:
        return {"error": "unauthorized"}, 403

    title = payload.get("title")
    status = payload.get("status")
    due_at = payload.get("due_at")

    if title is not None:
        subtask.title = title.strip()
    if status is not None:
        subtask.status = status.strip() or subtask.status
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

    # Limit scope to current user's project space
    project_ids = [p.id for p in Project.query.filter_by(owner_id=user.id).all()]
    task_ids = [t.id for t in Task.query.filter(Task.project_id.in_(project_ids)).all()] if project_ids else []
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
