from datetime import datetime
import secrets

from flask import Blueprint, redirect, render_template, request, url_for

from app.auth import login_required
from app.calendar_sync import ensure_task_event
from app.extensions import db
from app.models import Project, Task, Invite, Subtask, Assignment, User
from app.utils import current_user


ui_bp = Blueprint("ui", __name__)


@ui_bp.get("/")
@login_required
def dashboard():
    user = current_user()
    projects = Project.query.filter_by(owner_id=user.id).all()
    selected_project = None
    selected_id = request.args.get("project_id")
    if projects:
        if selected_id:
            try:
                selected_id_int = int(selected_id)
            except ValueError:
                selected_id_int = None
            if selected_id_int:
                selected_project = next((p for p in projects if p.id == selected_id_int), None)
        if not selected_project:
            selected_project = projects[0]

    tasks = []
    if selected_project:
        tasks = Task.query.filter_by(project_id=selected_project.id).all()
    project_ids = [p.id for p in projects]
    all_task_ids = [t.id for t in Task.query.filter(Task.project_id.in_(project_ids)).all()] if project_ids else []
    all_subtask_ids = (
        [s.id for s in Subtask.query.filter(Subtask.task_id.in_(all_task_ids)).all()] if all_task_ids else []
    )
    invites = Invite.query.filter(
        (Invite.task_id.in_(all_task_ids)) | (Invite.subtask_id.in_(all_subtask_ids))
    ).all()
    subtasks_by_task = {}
    assignments_by_task = {}
    assignments_by_subtask = {}
    if tasks:
        subtask_rows = Subtask.query.filter(Subtask.task_id.in_([t.id for t in tasks])).all()
        for sub in subtask_rows:
            subtasks_by_task.setdefault(sub.task_id, []).append(sub)
        assignment_rows = Assignment.query.filter(Assignment.task_id.in_([t.id for t in tasks])).all()
        for assignment in assignment_rows:
            assignments_by_task.setdefault(assignment.task_id, []).append(assignment)

        if subtask_rows:
            assignment_rows = Assignment.query.filter(Assignment.subtask_id.in_([s.id for s in subtask_rows])).all()
            for assignment in assignment_rows:
                assignments_by_subtask.setdefault(assignment.subtask_id, []).append(assignment)

    user_map = {u.id: u for u in User.query.all()}
    return render_template(
        "dashboard.html",
        user=user,
        projects=projects,
        tasks=tasks,
        invites=invites,
        selected_project=selected_project,
        subtasks_by_task=subtasks_by_task,
        assignments_by_task=assignments_by_task,
        assignments_by_subtask=assignments_by_subtask,
        user_map=user_map,
    )


@ui_bp.post("/projects")
@login_required
def create_project():
    user = current_user()
    name = (request.form.get("name") or "").strip()
    default_owner_calendar_opt_in = request.form.get("default_owner_calendar_opt_in") == "on"
    default_invitee_calendar_opt_in = request.form.get("default_invitee_calendar_opt_in") == "on"
    if not name:
        return redirect(url_for("ui.dashboard"))

    project = Project(
        name=name,
        owner_id=user.id,
        default_owner_calendar_opt_in=default_owner_calendar_opt_in,
        default_invitee_calendar_opt_in=default_invitee_calendar_opt_in,
    )
    db.session.add(project)
    db.session.commit()
    return redirect(url_for("ui.dashboard"))


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

    project = Project.query.filter_by(id=int(project_id), owner_id=user.id).first()
    if not project:
        return redirect(url_for("ui.dashboard", project_id=project_id))

    due_at = None
    if due_raw:
        try:
            due_at = datetime.fromisoformat(due_raw)
        except ValueError:
            due_at = None

    if use_project_defaults:
        owner_calendar_opt_in = project.default_owner_calendar_opt_in

    task = Task(
        project_id=int(project_id),
        title=title,
        due_at=due_at,
        owner_calendar_opt_in=owner_calendar_opt_in,
    )
    db.session.add(task)
    db.session.commit()

    if owner_calendar_opt_in:
        ensure_task_event(task.id)
    return redirect(url_for("ui.dashboard", project_id=project.id))


@ui_bp.post("/tasks/<int:task_id>/invite")
@login_required
def invite_to_task(task_id: int):
    email = (request.form.get("email") or "").strip().lower()
    if not email:
        return redirect(url_for("ui.dashboard"))

    task = Task.query.get(task_id)
    project = Project.query.get(task.project_id) if task else None
    invitee_default = project.default_invitee_calendar_opt_in if project else False

    token = secrets.token_hex(24)
    invite = Invite(task_id=task_id, email=email, token=token, calendar_opt_in=invitee_default)
    db.session.add(invite)
    db.session.commit()

    return redirect(url_for("ui.dashboard"))


@ui_bp.get("/invites/<token>")
def accept_invite(token: str):
    invite = Invite.query.filter_by(token=token).first()
    if not invite:
        return render_template("invite.html", status="invalid")

    task = Task.query.get(invite.task_id) if invite.task_id else None
    subtask = Subtask.query.get(invite.subtask_id) if invite.subtask_id else None
    return render_template(
        "invite.html",
        status=invite.status,
        invite=invite,
        task=task,
        subtask=subtask,
    )


@ui_bp.post("/invites/<token>/respond")
def respond_invite(token: str):
    invite = Invite.query.filter_by(token=token).first()
    if not invite:
        return render_template("invite.html", status="invalid")

    action = request.form.get("action")
    calendar_opt_in = request.form.get("calendar_opt_in") == "on"

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

    db.session.commit()
    task = Task.query.get(invite.task_id) if invite.task_id else None
    subtask = Subtask.query.get(invite.subtask_id) if invite.subtask_id else None
    if invite.status == "accepted" and invite.calendar_opt_in and task:
        ensure_task_event(task.id, invite.email)
    return render_template("invite.html", status=invite.status, invite=invite, task=task, subtask=subtask)
