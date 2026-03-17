from datetime import datetime
import secrets

from flask import Blueprint, redirect, render_template, request, url_for

from app.auth import login_required
from app.calendar_sync import ensure_task_event
from app.extensions import db
from app.models import Project, Task, Invite, Subtask, Assignment, User, Group, ProjectMember, GroupMember
from app.utils import current_user


ui_bp = Blueprint("ui", __name__)


@ui_bp.get("/")
@login_required
def dashboard():
    user = current_user()
    owned_projects = Project.query.filter_by(owner_id=user.id).all()
    member_project_ids = [
        m.project_id for m in ProjectMember.query.filter_by(user_id=user.id).all()
    ]
    member_group_project_ids = [
        row.project_id
        for row in db.session.query(Group.project_id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(GroupMember.user_id == user.id)
        .all()
    ]
    assigned_task_ids = [
        a.task_id for a in Assignment.query.filter_by(user_id=user.id).filter(Assignment.task_id.isnot(None)).all()
    ]
    assigned_subtask_ids = [
        a.subtask_id
        for a in Assignment.query.filter_by(user_id=user.id).filter(Assignment.subtask_id.isnot(None)).all()
    ]
    assigned_subtasks = (
        Subtask.query.filter(Subtask.id.in_(assigned_subtask_ids)).all() if assigned_subtask_ids else []
    )
    parent_task_ids = [s.task_id for s in assigned_subtasks]
    assigned_task_ids = list(set(assigned_task_ids + parent_task_ids))
    assigned_tasks = Task.query.filter(Task.id.in_(assigned_task_ids)).all() if assigned_task_ids else []
    assigned_project_ids = [t.project_id for t in assigned_tasks]
    accessible_project_ids = list(
        {p.id for p in owned_projects}
        .union(assigned_project_ids)
        .union(member_project_ids)
        .union(member_group_project_ids)
    )
    projects = (
        Project.query.filter(Project.id.in_(accessible_project_ids)).all()
        if accessible_project_ids
        else []
    )
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
    groups = []
    tasks_by_group = {}
    ungrouped_tasks = []
    is_owner = False
    is_project_member = False
    can_manage_project = False
    if selected_project:
        is_owner = selected_project.owner_id == user.id
        is_project_member = (
            ProjectMember.query.filter_by(project_id=selected_project.id, user_id=user.id).first()
            is not None
        )
        can_manage_project = is_owner or is_project_member
        if can_manage_project:
            tasks = Task.query.filter_by(project_id=selected_project.id).all()
            groups = Group.query.filter_by(project_id=selected_project.id).order_by(Group.position.asc(), Group.id.asc()).all()
            tasks_by_group = {g.id: [] for g in groups}
            for task in tasks:
                if task.group_id and task.group_id in tasks_by_group:
                    tasks_by_group[task.group_id].append(task)
                else:
                    ungrouped_tasks.append(task)
        else:
            group_ids = [
                m.group_id
                for m in GroupMember.query.join(Group).filter(
                    GroupMember.user_id == user.id, Group.project_id == selected_project.id
                )
            ]
            group_tasks = Task.query.filter(Task.group_id.in_(group_ids)).all() if group_ids else []
            tasks = list(
                {
                    t.id: t
                    for t in group_tasks + [t for t in assigned_tasks if t.project_id == selected_project.id]
                }.values()
            )
            group_ids = [t.group_id for t in tasks if t.group_id]
            groups = (
                Group.query.filter(Group.id.in_(group_ids))
                .order_by(Group.position.asc(), Group.id.asc())
                .all()
                if group_ids
                else []
            )
            tasks_by_group = {g.id: [] for g in groups}
            for task in tasks:
                if task.group_id and task.group_id in tasks_by_group:
                    tasks_by_group[task.group_id].append(task)
                else:
                    ungrouped_tasks.append(task)
    project_ids = [p.id for p in projects]
    all_task_ids = [t.id for t in Task.query.filter(Task.project_id.in_(project_ids)).all()] if project_ids else []
    all_subtask_ids = (
        [s.id for s in Subtask.query.filter(Subtask.task_id.in_(all_task_ids)).all()] if all_task_ids else []
    )
    invites = []
    if owned_projects:
        invites = Invite.query.filter(
            (Invite.task_id.in_(all_task_ids)) | (Invite.subtask_id.in_(all_subtask_ids))
        ).all()
    subtasks_by_task = {}
    assignments_by_task = {}
    assignments_by_subtask = {}
    if tasks:
        if is_owner or is_project_member:
            subtask_rows = Subtask.query.filter(Subtask.task_id.in_([t.id for t in tasks])).all()
        else:
            subtask_rows = [s for s in assigned_subtasks if s.task_id in [t.id for t in tasks]]
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
    project_viewers = {}
    group_viewers = {}
    if projects:
        project_ids = [p.id for p in projects]
        project_member_rows = ProjectMember.query.filter(ProjectMember.project_id.in_(project_ids)).all()
        for project in projects:
            viewers = {project.owner_id}
            for row in project_member_rows:
                if row.project_id == project.id:
                    viewers.add(row.user_id)
            project_viewers[project.id] = [user_map.get(uid) for uid in viewers if user_map.get(uid)]

        group_ids = [g.id for g in Group.query.filter(Group.project_id.in_(project_ids)).all()]
        group_member_rows = GroupMember.query.filter(GroupMember.group_id.in_(group_ids)).all() if group_ids else []
        group_member_map = {}
        for row in group_member_rows:
            group_member_map.setdefault(row.group_id, set()).add(row.user_id)
        for group in Group.query.filter(Group.id.in_(group_ids)).all():
            viewers = set()
            project_viewers_ids = [u.id for u in project_viewers.get(group.project_id, [])]
            viewers.update(project_viewers_ids)
            viewers.update(group_member_map.get(group.id, set()))
            group_viewers[group.id] = [user_map.get(uid) for uid in viewers if user_map.get(uid)]
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
        groups=groups,
        tasks_by_group=tasks_by_group,
        ungrouped_tasks=ungrouped_tasks,
        is_owner=is_owner,
        can_manage_project=can_manage_project,
        member_project_ids=member_project_ids,
        project_viewers=project_viewers,
        group_viewers=group_viewers,
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
    max_pos = db.session.query(db.func.max(Group.position)).filter_by(project_id=project.id).scalar() or 0
    palette = ["#4cc9f0", "#7dd3fc", "#f59e0b", "#34d399", "#f472b6", "#a78bfa", "#f87171", "#38bdf8"]
    group = Group(
        project_id=project.id,
        name="General",
        position=max_pos + 1,
        color=palette[(max_pos) % len(palette)],
    )
    db.session.add(group)
    db.session.commit()
    return redirect(url_for("ui.dashboard"))


@ui_bp.post("/groups")
@login_required
def create_group():
    user = current_user()
    name = (request.form.get("name") or "").strip()
    project_id = request.form.get("project_id")
    if not name or not project_id:
        return redirect(url_for("ui.dashboard", project_id=project_id))
    try:
        project_id_int = int(project_id)
    except ValueError:
        return redirect(url_for("ui.dashboard"))

    project = Project.query.filter_by(id=project_id_int).first()
    if not project:
        return redirect(url_for("ui.dashboard"))
    if project.owner_id != user.id and not ProjectMember.query.filter_by(project_id=project.id, user_id=user.id).first():
        return redirect(url_for("ui.dashboard"))

    max_pos = db.session.query(db.func.max(Group.position)).filter_by(project_id=project.id).scalar() or 0
    palette = ["#4cc9f0", "#7dd3fc", "#f59e0b", "#34d399", "#f472b6", "#a78bfa", "#f87171", "#38bdf8"]
    group = Group(
        project_id=project.id,
        name=name,
        position=max_pos + 1,
        color=palette[(max_pos) % len(palette)],
    )
    db.session.add(group)
    db.session.commit()
    return redirect(url_for("ui.dashboard", project_id=project.id))


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

    project = Project.query.filter_by(id=int(project_id)).first()
    if not project:
        return redirect(url_for("ui.dashboard", project_id=project_id))
    if project.owner_id != user.id and not ProjectMember.query.filter_by(project_id=project.id, user_id=user.id).first():
        return redirect(url_for("ui.dashboard", project_id=project_id))

    due_at = None
    if due_raw:
        try:
            due_at = datetime.fromisoformat(due_raw)
        except ValueError:
            due_at = None

    if use_project_defaults:
        owner_calendar_opt_in = project.default_owner_calendar_opt_in

    group = Group.query.filter_by(project_id=project.id).first()
    task = Task(
        project_id=int(project_id),
        group_id=group.id if group else None,
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
