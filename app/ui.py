from datetime import datetime
import secrets

from flask import Blueprint, current_app, redirect, render_template, request, url_for

from app.auth import login_required
from app.calendar_sync import CalendarSyncError, ensure_task_event
from app.collaborators import get_or_create_collaborator_profile
from app.extensions import db
from app.info_utils import load_info_payload, normalize_info_payload
from app.models import CollaboratorProfile, DevMailboxMessage, Project, Task, Invite, Subtask, Assignment, User, Group, ProjectMember, GroupMember
from app.utils import current_user


ui_bp = Blueprint("ui", __name__)


def _task_ordering(query):
    return query.order_by(Task.position.asc(), Task.id.asc())


def _safe_ensure_task_event(task_id: int, invitee_email: str | None = None) -> None:
    try:
        ensure_task_event(task_id, invitee_email)
    except CalendarSyncError as exc:
        current_app.logger.warning("Calendar sync skipped for task %s: %s", task_id, exc)


def _resolve_work_item(task_id: int | None, subtask_id: int | None) -> tuple[Task | None, Subtask | None]:
    task = Task.query.get(task_id) if task_id else None
    subtask = Subtask.query.get(subtask_id) if subtask_id else None
    if subtask and not task:
        task = Task.query.get(subtask.task_id)
    return task, subtask


def _work_item_status(task: Task | None, subtask: Subtask | None) -> str:
    return (subtask.status if subtask else (task.status if task else "open")) or "open"


def _is_complete_status(status: str | None) -> bool:
    return (status or "").strip().lower() in {"complete", "completed", "done", "closed"}


def _mark_work_item_complete(task: Task | None, subtask: Subtask | None) -> None:
    if subtask:
        subtask.status = "complete"
    elif task:
        task.status = "complete"


def _apply_invite_response(invite: Invite, action: str, calendar_opt_in: bool) -> None:
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


def _ensure_assignment_invite(assignment: Assignment, calendar_opt_in: bool = False) -> Invite:
    invite = Invite.query.filter_by(assignment_id=assignment.id).first()
    if invite:
        return invite
    invite = Invite(
        task_id=assignment.task_id,
        subtask_id=assignment.subtask_id,
        assignment_id=assignment.id,
        email=assignment.email,
        token=secrets.token_hex(24),
        status="sent",
        calendar_opt_in=calendar_opt_in,
    )
    db.session.add(invite)
    db.session.flush()
    return invite


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
            tasks = _task_ordering(Task.query.filter_by(project_id=selected_project.id)).all()
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
            group_tasks = _task_ordering(Task.query.filter(Task.group_id.in_(group_ids))).all() if group_ids else []
            tasks = list(
                {
                    t.id: t
                    for t in group_tasks + [t for t in assigned_tasks if t.project_id == selected_project.id]
                }.values()
            )
            tasks.sort(key=lambda task: (task.position, task.id))
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
    all_task_ids = [t.id for t in _task_ordering(Task.query.filter(Task.project_id.in_(project_ids))).all()] if project_ids else []
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
    info_by_task = {}
    info_by_subtask = {}
    if tasks:
        if is_owner or is_project_member:
            subtask_rows = Subtask.query.filter(Subtask.task_id.in_([t.id for t in tasks])).all()
        else:
            subtask_rows = [s for s in assigned_subtasks if s.task_id in [t.id for t in tasks]]
        for sub in subtask_rows:
            subtasks_by_task.setdefault(sub.task_id, []).append(sub)
            info_by_subtask[sub.id] = load_info_payload(sub.info, sub.link)
        assignment_rows = Assignment.query.filter(Assignment.task_id.in_([t.id for t in tasks])).all()
        for assignment in assignment_rows:
            assignments_by_task.setdefault(assignment.task_id, []).append(assignment)

        if subtask_rows:
            assignment_rows = Assignment.query.filter(Assignment.subtask_id.in_([s.id for s in subtask_rows])).all()
            for assignment in assignment_rows:
                assignments_by_subtask.setdefault(assignment.subtask_id, []).append(assignment)
        for task in tasks:
            info_by_task[task.id] = load_info_payload(task.info, task.link)

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
        info_by_task=info_by_task,
        info_by_subtask=info_by_subtask,
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


@ui_bp.get("/dev/mail")
@login_required
def dev_mailbox():
    if not current_app.config.get("DEV_MAILBOX_ENABLED"):
        return redirect(url_for("ui.dashboard"))
    messages = DevMailboxMessage.query.order_by(DevMailboxMessage.created_at.desc(), DevMailboxMessage.id.desc()).all()
    return render_template("dev_mailbox.html", user=current_user(), messages=messages, title="Dev Mailbox")


@ui_bp.post("/dev/mail/clear")
@login_required
def clear_dev_mailbox():
    if not current_app.config.get("DEV_MAILBOX_ENABLED"):
        return redirect(url_for("ui.dashboard"))
    DevMailboxMessage.query.delete()
    db.session.commit()
    return redirect(url_for("ui.dev_mailbox"))


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
        position=(db.session.query(db.func.max(Task.position)).filter_by(project_id=project.id, group_id=group.id if group else None).scalar() or 0) + 1,
        title=title,
        info=normalize_info_payload(None),
        due_at=due_at,
        owner_calendar_opt_in=owner_calendar_opt_in,
    )
    db.session.add(task)
    db.session.commit()

    if owner_calendar_opt_in:
        _safe_ensure_task_event(task.id)
    return redirect(url_for("ui.dashboard", project_id=project.id))


@ui_bp.post("/tasks/<int:task_id>/invite")
@login_required
def invite_to_task(task_id: int):
    email = (request.form.get("email") or "").strip().lower()
    if not email:
        return redirect(url_for("ui.dashboard"))

    task = Task.query.get(task_id)
    project = Project.query.get(task.project_id) if task else None
    collaborator = get_or_create_collaborator_profile(
        email=email,
        default_calendar_opt_in=project.default_invitee_calendar_opt_in if project else False,
    )

    token = secrets.token_hex(24)
    invite = Invite(
        task_id=task_id,
        email=email,
        token=token,
        calendar_opt_in=collaborator.default_calendar_opt_in,
    )
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
        collaborator=CollaboratorProfile.query.filter_by(email=invite.email).first(),
    )


@ui_bp.post("/invites/<token>/respond")
def respond_invite(token: str):
    invite = Invite.query.filter_by(token=token).first()
    if not invite:
        return render_template("invite.html", status="invalid")

    action = request.form.get("action")
    calendar_opt_in = request.form.get("calendar_opt_in") == "on"
    _apply_invite_response(invite, action, calendar_opt_in)

    db.session.commit()
    task = Task.query.get(invite.task_id) if invite.task_id else None
    subtask = Subtask.query.get(invite.subtask_id) if invite.subtask_id else None
    if invite.status == "accepted" and invite.calendar_opt_in and task:
        _safe_ensure_task_event(task.id, invite.email)
    return render_template(
        "invite.html",
        status=invite.status,
        invite=invite,
        task=task,
        subtask=subtask,
        collaborator=CollaboratorProfile.query.filter_by(email=invite.email).first(),
    )


@ui_bp.get("/collaborators/<token>")
def collaborator_portal(token: str):
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return render_template("collaborator_portal.html", status="invalid", collaborator=None, entries=[])

    invites = Invite.query.filter_by(email=collaborator.email).order_by(Invite.created_at.desc(), Invite.id.desc()).all()
    assignments = Assignment.query.filter_by(email=collaborator.email).order_by(Assignment.created_at.desc(), Assignment.id.desc()).all()
    invite_by_assignment_id = {invite.assignment_id: invite for invite in invites if invite.assignment_id}
    task_ids = {invite.task_id for invite in invites if invite.task_id}
    task_ids.update({assignment.task_id for assignment in assignments if assignment.task_id})
    subtask_ids = {invite.subtask_id for invite in invites if invite.subtask_id}
    subtask_ids.update({assignment.subtask_id for assignment in assignments if assignment.subtask_id})
    tasks = {task.id: task for task in Task.query.filter(Task.id.in_(task_ids)).all()} if task_ids else {}
    subtasks = {subtask.id: subtask for subtask in Subtask.query.filter(Subtask.id.in_(subtask_ids)).all()} if subtask_ids else {}
    project_ids = {task.project_id for task in tasks.values() if task and task.project_id}
    projects = {project.id: project for project in Project.query.filter(Project.id.in_(project_ids)).all()} if project_ids else {}
    entries = []

    def resolve_context(task_id: int | None, subtask_id: int | None) -> tuple[Task | None, Subtask | None, Project | None]:
        task = tasks.get(task_id) if task_id else None
        subtask = subtasks.get(subtask_id) if subtask_id else None
        if subtask and not task:
            task = tasks.get(subtask.task_id) or Task.query.get(subtask.task_id)
            if task:
                tasks[task.id] = task
        project = None
        if task and task.project_id:
            project = projects.get(task.project_id)
            if not project:
                project = Project.query.get(task.project_id)
                if project:
                    projects[project.id] = project
        return task, subtask, project

    for assignment in assignments:
        invite = invite_by_assignment_id.get(assignment.id)
        task, subtask, project = resolve_context(assignment.task_id, assignment.subtask_id)
        entries.append(
            {
                "kind": "assignment",
                "assignment": assignment,
                "invite": invite,
                "task": task,
                "subtask": subtask,
                "project": project,
                "status": invite.status if invite else assignment.status,
                "calendar_opt_in": invite.calendar_opt_in if invite else collaborator.default_calendar_opt_in,
                "created_at": (invite.created_at if invite else assignment.created_at),
                "work_status": _work_item_status(task, subtask),
                "is_complete": _is_complete_status(_work_item_status(task, subtask)),
            }
        )

    assigned_invite_ids = {invite.id for invite in invites if invite.assignment_id}
    for invite in invites:
        if invite.id in assigned_invite_ids:
            continue
        task, subtask, project = resolve_context(invite.task_id, invite.subtask_id)
        entries.append(
            {
                "kind": "invite",
                "assignment": None,
                "invite": invite,
                "task": task,
                "subtask": subtask,
                "project": project,
                "status": invite.status,
                "calendar_opt_in": invite.calendar_opt_in,
                "created_at": invite.created_at,
                "work_status": _work_item_status(task, subtask),
                "is_complete": _is_complete_status(_work_item_status(task, subtask)),
            }
        )

    entries.sort(key=lambda item: (item["created_at"], item["invite"].id if item["invite"] else item["assignment"].id), reverse=True)

    return render_template(
        "collaborator_portal.html",
        status="ok",
        collaborator=collaborator,
        entries=entries,
    )


@ui_bp.post("/collaborators/<token>/settings")
def update_collaborator_settings(token: str):
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return render_template("collaborator_portal.html", status="invalid", collaborator=None, entries=[])

    collaborator.display_name = (request.form.get("display_name") or "").strip() or None
    collaborator.default_calendar_opt_in = request.form.get("default_calendar_opt_in") == "on"
    collaborator.updated_at = datetime.utcnow()
    db.session.commit()
    return redirect(url_for("ui.collaborator_portal", token=token))


@ui_bp.post("/collaborators/<token>/invites/<int:invite_id>")
def update_collaborator_invite(token: str, invite_id: int):
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return render_template("collaborator_portal.html", status="invalid", collaborator=None, entries=[])

    invite = Invite.query.filter_by(id=invite_id, email=collaborator.email).first()
    if not invite:
        return redirect(url_for("ui.collaborator_portal", token=token))

    action = request.form.get("action")
    calendar_opt_in = request.form.get("calendar_opt_in") == "on"
    if action in {"accept", "decline"}:
        _apply_invite_response(invite, action, calendar_opt_in)
    elif action == "calendar":
        invite.calendar_opt_in = True
    elif action == "complete":
        task, subtask = _resolve_work_item(invite.task_id, invite.subtask_id)
        _mark_work_item_complete(task, subtask)
    db.session.commit()

    task, _subtask = _resolve_work_item(invite.task_id, invite.subtask_id)
    if action == "calendar" and task:
        _safe_ensure_task_event(task.id, invite.email)
    elif invite.status == "accepted" and invite.calendar_opt_in and task:
        _safe_ensure_task_event(task.id, invite.email)
    return redirect(url_for("ui.collaborator_portal", token=token))


@ui_bp.post("/collaborators/<token>/assignments/<int:assignment_id>")
def update_collaborator_assignment(token: str, assignment_id: int):
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return render_template("collaborator_portal.html", status="invalid", collaborator=None, entries=[])

    assignment = Assignment.query.filter_by(id=assignment_id, email=collaborator.email).first()
    if not assignment:
        return redirect(url_for("ui.collaborator_portal", token=token))

    calendar_opt_in = request.form.get("calendar_opt_in") == "on"
    invite = _ensure_assignment_invite(assignment, calendar_opt_in=calendar_opt_in)
    action = request.form.get("action")
    if action in {"accept", "decline"}:
        _apply_invite_response(invite, action, calendar_opt_in)
    elif action == "calendar":
        invite.calendar_opt_in = True
    elif action == "complete":
        task, subtask = _resolve_work_item(assignment.task_id, assignment.subtask_id)
        _mark_work_item_complete(task, subtask)
    db.session.commit()

    task, _subtask = _resolve_work_item(assignment.task_id, assignment.subtask_id)
    if action == "calendar" and task:
        _safe_ensure_task_event(task.id, invite.email)
    elif invite.status == "accepted" and invite.calendar_opt_in and task:
        _safe_ensure_task_event(task.id, invite.email)
    return redirect(url_for("ui.collaborator_portal", token=token))
