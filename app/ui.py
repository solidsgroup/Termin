from datetime import date, datetime, timedelta
from functools import wraps
import secrets

from flask import Blueprint, current_app, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from app.auth import login_required
from app.calendar_sync import CalendarSyncError, ensure_task_event
from app.collaborators import get_or_create_collaborator_profile
from app.emailer import MailDeliveryError, send_account_verification_email
from app.extensions import db
from app.identity import (
    add_user_email,
    claim_collaborator_profile,
    consume_email_verification,
    create_email_verification,
    find_user_by_email,
    list_external_identities,
    list_user_emails,
    merge_users,
    remove_user_email,
    set_primary_user_email,
)
from app.info_utils import load_info_payload, normalize_info_payload
from app.models import CollaboratorProfile, DevMailboxMessage, Division, EmailVerification, ExternalIdentity, Project, ProjectSidebarPreference, Task, Invite, Subtask, Assignment, User, UserEmail, Group, ProjectMember, GroupMember
from app.sidebar_layout import (
    ensure_sidebar_preference,
    insert_project_position,
    insert_top_level,
    sidebar_preference_map,
    top_level_sidebar_items,
)
from app.utils import current_user
from app.utils import is_admin as user_is_admin


ui_bp = Blueprint("ui", __name__)


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user_is_admin(user):
            return redirect(url_for("ui.dashboard"))
        return fn(*args, **kwargs)
    return wrapper


def _task_ordering(query):
    return query.order_by(Task.position.asc(), Task.id.asc())


def _division_palette() -> list[str]:
    return ["#4cc9f0", "#f59e0b", "#34d399", "#f472b6", "#a78bfa", "#f87171", "#38bdf8", "#facc15"]


def _render_account_page(user: User, *, error: str | None = None, message: str | None = None):
    emails = list_user_emails(user.id)
    identities = list_external_identities(user.id)
    pending = EmailVerification.query.filter(
        EmailVerification.user_id == user.id,
        EmailVerification.consumed_at.is_(None),
    ).order_by(EmailVerification.created_at.desc()).all()
    return render_template(
        "account.html",
        user=user,
        emails=emails,
        identities=identities,
        pending_verifications=pending,
        title="Account",
        error=error,
        merge_message=message,
    )


def _render_admin_page(template: str, *, section: str, **context):
    return render_template(
        template,
        title="Admin",
        admin_section=section,
        **context,
    )


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


def _build_collaborator_entries(collaborator: CollaboratorProfile) -> list[dict]:
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
    entries: list[dict] = []

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
    return entries


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
    now_utc = datetime.utcnow()
    current_view = (request.args.get("view") or "project").strip().lower()
    if current_view not in {"project", "todo"}:
        current_view = "project"
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
    sidebar_divisions = Division.query.filter_by(owner_id=user.id).order_by(Division.position.asc(), Division.name.asc(), Division.id.asc()).all()
    division_options = [{"id": division.id, "name": division.name, "color": division.color} for division in sidebar_divisions]
    sidebar_pref_map = sidebar_preference_map(user.id, projects, sidebar_divisions)
    projects_by_division = {division.id: [] for division in sidebar_divisions}
    top_level_items = top_level_sidebar_items(projects, sidebar_divisions, sidebar_pref_map)
    for project in projects:
        pref = sidebar_pref_map.get(project.id)
        if pref and pref.division_id and pref.division_id in projects_by_division:
            projects_by_division[pref.division_id].append(project)
    for division_id, grouped_projects in projects_by_division.items():
        grouped_projects.sort(key=lambda project: ((sidebar_pref_map.get(project.id).position if sidebar_pref_map.get(project.id) else 0), project.id))

    selected_project = None
    division_color_map = {division.id: (division.color or "#4cc9f0") for division in sidebar_divisions}
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
    selected_project_color = "#4cc9f0"
    manageable_project_ids = {project.id for project in owned_projects}.union(member_project_ids)
    if selected_project:
        selected_pref = sidebar_pref_map.get(selected_project.id)
        selected_project_color = division_color_map.get(selected_pref.division_id if selected_pref else None, "#4cc9f0")
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
    project_map = {project.id: project for project in projects}
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

    visible_group_ids = {
        row.group_id
        for row in GroupMember.query.join(Group, Group.id == GroupMember.group_id)
        .filter(GroupMember.user_id == user.id, Group.project_id.in_(project_ids if project_ids else [-1]))
        .all()
    } if project_ids else set()
    visible_task_ids = set()
    if manageable_project_ids:
        visible_task_ids.update(
            row.id for row in Task.query.filter(Task.project_id.in_(manageable_project_ids)).all()
        )
    if visible_group_ids:
        visible_task_ids.update(
            row.id for row in Task.query.filter(Task.group_id.in_(visible_group_ids)).all()
        )
    visible_task_ids.update(task_id for task_id in assigned_task_ids if task_id)
    visible_task_ids.update(parent_task_ids)
    todo_tasks = Task.query.filter(Task.id.in_(visible_task_ids)).all() if visible_task_ids else []
    todo_groups = {group.id: group for group in Group.query.filter(Group.id.in_([task.group_id for task in todo_tasks if task.group_id])).all()} if todo_tasks else {}
    todo_items = []
    today = now_utc.date()
    week_start = today - timedelta(days=today.weekday())
    next_week_start = week_start + timedelta(days=7)
    two_weeks_start = week_start + timedelta(days=14)
    three_weeks_start = week_start + timedelta(days=21)
    four_weeks_start = week_start + timedelta(days=28)
    next_month_year = today.year + (1 if today.month == 12 else 0)
    next_month_month = 1 if today.month == 12 else today.month + 1
    month_after_next_year = next_month_year + (1 if next_month_month == 12 else 0)
    month_after_next_month = 1 if next_month_month == 12 else next_month_month + 1
    next_month_start = date(next_month_year, next_month_month, 1)
    month_after_next_start = date(month_after_next_year, month_after_next_month, 1)

    def todo_bucket(due_date):
        if due_date is None:
            return ("none", "No Due Date", 9)
        if due_date < today:
            return ("overdue", "Overdue", 0)
        if due_date == today:
            return ("today", "Today", 1)
        if due_date == today + timedelta(days=1):
            return ("tomorrow", "Tomorrow", 2)
        if due_date < next_week_start:
            return ("this_week", "This Week", 3)
        if due_date < two_weeks_start:
            return ("next_week", "Next Week", 4)
        if due_date < three_weeks_start:
            return ("two_weeks", "Two Weeks", 5)
        if due_date < four_weeks_start:
            return ("three_weeks", "Three Weeks", 6)
        if next_month_start <= due_date < month_after_next_start:
            return ("next_month", "Next Month", 7)
        return ("later", "Later", 8)

    for task in todo_tasks:
        project = project_map.get(task.project_id)
        if not project:
            continue
        pref = sidebar_pref_map.get(project.id)
        division = next((item for item in sidebar_divisions if pref and item.id == pref.division_id), None)
        bucket_key, bucket_label, bucket_rank = todo_bucket(task.due_at.date() if task.due_at else None)
        todo_items.append(
            {
                "task": task,
                "project": project,
                "group": todo_groups.get(task.group_id),
                "division_color": (division.color if division and division.color else "#4cc9f0"),
                "date_key": bucket_key,
                "date_label": bucket_label,
                "date_rank": bucket_rank,
            }
        )
    todo_items.sort(
        key=lambda item: (
            item["date_rank"],
            item["task"].due_at.date().isoformat() if item["task"].due_at else "9999-12-31",
            item["project"].name.lower(),
            item["task"].position or 0,
            item["task"].id,
        )
    )
    todo_groups_by_date = []
    for item in todo_items:
        if not todo_groups_by_date or todo_groups_by_date[-1]["key"] != item["date_key"]:
            todo_groups_by_date.append({"key": item["date_key"], "label": item["date_label"], "items": []})
        todo_groups_by_date[-1]["items"].append(item)
    return render_template(
        "dashboard.html",
        user=user,
        now_utc=now_utc,
        current_view=current_view,
        projects=projects,
        divisions=sidebar_divisions,
        division_options=division_options,
        selected_project_color=selected_project_color,
        projects_by_division=projects_by_division,
        top_level_items=top_level_items,
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
        todo_groups_by_date=todo_groups_by_date,
    )


@ui_bp.get("/dev/mail")
@login_required
def dev_mailbox():
    if not current_app.config.get("DEV_MAILBOX_ENABLED"):
        return redirect(url_for("ui.dashboard"))
    messages = DevMailboxMessage.query.order_by(DevMailboxMessage.created_at.desc(), DevMailboxMessage.id.desc()).all()
    return render_template("dev_mailbox.html", user=current_user(), messages=messages, title="Dev Mailbox")


@ui_bp.get("/account")
@login_required
def account():
    user = current_user()
    return _render_account_page(user)


@ui_bp.get("/admin")
@admin_required
def admin_index():
    return redirect(url_for("ui.admin_users"))


@ui_bp.get("/admin/users")
@admin_required
def admin_users():
    users = User.query.order_by(User.created_at.desc(), User.id.desc()).all()
    emails = UserEmail.query.order_by(UserEmail.user_id.asc(), UserEmail.is_primary.desc(), UserEmail.email.asc()).all()
    identities = ExternalIdentity.query.order_by(
        ExternalIdentity.user_id.asc(),
        ExternalIdentity.provider.asc(),
        ExternalIdentity.created_at.asc(),
    ).all()
    collaborators = CollaboratorProfile.query.order_by(CollaboratorProfile.updated_at.desc(), CollaboratorProfile.id.desc()).all()

    emails_by_user: dict[int, list[UserEmail]] = {}
    for email in emails:
        emails_by_user.setdefault(email.user_id, []).append(email)

    identities_by_user: dict[int, list[ExternalIdentity]] = {}
    for identity in identities:
        identities_by_user.setdefault(identity.user_id, []).append(identity)

    return _render_admin_page(
        "admin_users.html",
        section="users",
        users=users,
        emails_by_user=emails_by_user,
        identities_by_user=identities_by_user,
        collaborators=collaborators,
    )


@ui_bp.get("/admin/collaborators/<int:collaborator_id>")
@admin_required
def admin_collaborator(collaborator_id: int):
    collaborator = CollaboratorProfile.query.get_or_404(collaborator_id)
    entries = _build_collaborator_entries(collaborator)
    sent_messages = DevMailboxMessage.query.filter_by(to_email=collaborator.email).order_by(
        DevMailboxMessage.created_at.desc(),
        DevMailboxMessage.id.desc(),
    ).all()

    open_entries = [entry for entry in entries if entry["status"] in {"sent", "draft", "assigned", "link_sent"}]
    history_entries = [entry for entry in entries if entry["status"] in {"accepted", "declined", "denied"}]
    todo_entries = [entry for entry in entries if not entry["is_complete"]]

    return _render_admin_page(
        "admin_collaborator.html",
        section="users",
        collaborator=collaborator,
        entries=entries,
        open_entries=open_entries,
        history_entries=history_entries,
        todo_entries=todo_entries,
        sent_messages=sent_messages,
        collaborator_portal_url=url_for("ui.collaborator_portal", token=collaborator.access_token),
    )


@ui_bp.post("/account/emails")
@login_required
def add_account_email():
    user = current_user()
    try:
        verification = create_email_verification(user=user, email=request.form.get("email"), purpose="add_email")
        verify_url = url_for("ui.verify_account_email", token=verification.token, _external=True)
        send_account_verification_email(
            to_email=verification.email,
            verify_url=verify_url,
            purpose="add_email",
            inviter_email=user.email,
            inviter_name=user.display_name or user.email,
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _render_account_page(user, error=str(exc))
    except MailDeliveryError as exc:
        db.session.rollback()
        return _render_account_page(user, error=f"Could not send verification email: {exc}")
    return _render_account_page(user, message=f"Verification email sent to {verification.email}.")


@ui_bp.post("/account/emails/<int:email_id>/primary")
@login_required
def set_account_primary_email(email_id: int):
    user = current_user()
    try:
        set_primary_user_email(user, email_id)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _render_account_page(user, error=str(exc))
    return redirect(url_for("ui.account"))


@ui_bp.post("/account/emails/<int:email_id>/unlink")
@login_required
def unlink_account_email(email_id: int):
    user = current_user()
    try:
        remove_user_email(user, email_id)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _render_account_page(user, error=str(exc))
    return _render_account_page(user, message="Email unlinked.")


@ui_bp.post("/account/merge")
@login_required
def merge_account():
    user = current_user()
    email = request.form.get("email")
    source_user = find_user_by_email(email)

    if not source_user:
        return _render_account_page(user, error="No account was found for that email.")

    try:
        verification = create_email_verification(
            user=user,
            email=email,
            purpose="merge_account",
            source_user=source_user,
        )
        verify_url = url_for("ui.verify_account_email", token=verification.token, _external=True)
        send_account_verification_email(
            to_email=verification.email,
            verify_url=verify_url,
            purpose="merge_account",
            inviter_email=user.email,
            inviter_name=user.display_name or user.email,
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return _render_account_page(user, error=str(exc))
    except MailDeliveryError as exc:
        db.session.rollback()
        return _render_account_page(user, error=f"Could not send merge verification email: {exc}")

    return _render_account_page(user, message=f"Merge verification email sent to {verification.email}.")


@ui_bp.get("/account/verify/<token>")
def verify_account_email(token: str):
    verification = consume_email_verification(token)
    if not verification:
        return render_template(
            "account_verify.html",
            status="invalid",
            message="This verification link is invalid or already used.",
            continue_url=url_for("auth.login"),
        )

    user = User.query.get(verification.user_id)
    if not user:
        db.session.rollback()
        return render_template(
            "account_verify.html",
            status="invalid",
            message="The target account no longer exists.",
            continue_url=url_for("auth.login"),
        )

    try:
        if verification.purpose == "add_email":
            add_user_email(user, verification.email)
        elif verification.purpose == "merge_account":
            source_user = User.query.get(verification.source_user_id) if verification.source_user_id else find_user_by_email(verification.email)
            if not source_user:
                raise ValueError("The source account no longer exists.")
            merge_users(target_user=user, source_user=source_user)
        else:
            raise ValueError("Unsupported verification type.")
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return render_template(
            "account_verify.html",
            status="error",
            message=str(exc),
            continue_url=url_for("auth.login"),
        )

    return render_template(
        "account_verify.html",
        status="ok",
        message="Email verification complete." if verification.purpose == "add_email" else "Account merge complete.",
        continue_url=url_for("ui.account"),
    )


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
    division_id = (request.form.get("division_id") or "").strip()
    insert_position_raw = (request.form.get("insert_position") or "").strip()
    default_owner_calendar_opt_in = request.form.get("default_owner_calendar_opt_in") == "on"
    default_invitee_calendar_opt_in = request.form.get("default_invitee_calendar_opt_in") == "on"
    if not name:
        return redirect(url_for("ui.dashboard"))

    division = None
    if division_id:
        try:
            division = Division.query.filter_by(id=int(division_id), owner_id=user.id).first()
        except ValueError:
            division = None
    try:
        insert_position = int(insert_position_raw) if insert_position_raw else None
    except ValueError:
        insert_position = None
    project_position = insert_project_position(user.id, division.id if division else None, insert_position)

    project = Project(
        name=name,
        owner_id=user.id,
        division_id=None,
        position=0,
        default_owner_calendar_opt_in=default_owner_calendar_opt_in,
        default_invitee_calendar_opt_in=default_invitee_calendar_opt_in,
    )
    db.session.add(project)
    db.session.flush()
    db.session.add(
        ProjectSidebarPreference(
            user_id=user.id,
            project_id=project.id,
            division_id=division.id if division else None,
            position=project_position,
        )
    )
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
    return redirect(url_for("ui.dashboard", project_id=project.id))


@ui_bp.post("/divisions")
@login_required
def create_division():
    user = current_user()
    name = (request.form.get("name") or "").strip()
    insert_position_raw = (request.form.get("insert_position") or "").strip()
    if not name:
        return redirect(url_for("ui.dashboard"))
    try:
        insert_position = int(insert_position_raw) if insert_position_raw else None
    except ValueError:
        insert_position = None

    division_position = insert_top_level(user.id, insert_position)
    palette = _division_palette()
    division = Division(
        owner_id=user.id,
        name=name,
        color=palette[(division_position - 1) % len(palette)],
        position=division_position,
    )
    db.session.add(division)
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
    entries = _build_collaborator_entries(collaborator)

    return render_template(
        "collaborator_portal.html",
        status="ok",
        collaborator=collaborator,
        entries=entries,
    )


@ui_bp.post("/collaborators/<token>/claim/password")
def claim_collaborator_password(token: str):
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return render_template("collaborator_portal.html", status="invalid", collaborator=None, entries=[])

    password = request.form.get("password") or ""
    password_confirm = request.form.get("password_confirm") or ""
    if len(password) < 8:
        return render_template(
            "collaborator_portal.html",
            status="ok",
            collaborator=collaborator,
            entries=_build_collaborator_entries(collaborator),
            claim_error="Password must be at least 8 characters.",
        )
    if password != password_confirm:
        return render_template(
            "collaborator_portal.html",
            status="ok",
            collaborator=collaborator,
            entries=_build_collaborator_entries(collaborator),
            claim_error="Passwords do not match.",
        )

    user = claim_collaborator_profile(collaborator)
    user.password_hash = generate_password_hash(password)
    db.session.commit()
    from flask import session
    session["user_id"] = user.id
    return redirect(url_for("ui.dashboard"))


@ui_bp.get("/collaborators/<token>/claim/<provider>")
def claim_collaborator_provider(token: str, provider: str):
    if provider not in {"google", "github"}:
        return redirect(url_for("ui.collaborator_portal", token=token))
    collaborator = CollaboratorProfile.query.filter_by(access_token=token).first()
    if not collaborator:
        return render_template("collaborator_portal.html", status="invalid", collaborator=None, entries=[])
    from flask import session
    session["collaborator_claim_token"] = token
    return redirect(url_for(f"auth.login_{provider}"))


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
