from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape
import re

from markdown import markdown as render_markdown

from app.discussion_activity import build_discussion_activity_items
from app.extensions import db
from app.group_assignments import serialize_group_assignment_members
from app.info_utils import load_info_payload
from app.models import (
    Assignment,
    Division,
    Group,
    GroupMember,
    Project,
    ProjectMember,
    Task,
    TaskComment,
    TaskNotification,
    User,
    UserDiscussionActivity,
)
from app.sidebar_layout import sidebar_preference_map, top_level_sidebar_items
from app.task_status import effective_task_status_for_user, task_status_meta_map
from app.themes import division_effective_color, normalize_theme_name
from app.info_utils import sanitize_info_html
from app.ui import _build_github_task_meta
from app.utils import display_name_for_user


def _direct_peer_for_user(project: Project, user_id: int) -> User | None:
    if not project or not getattr(project, "is_direct", False):
        return None
    peer_id = None
    if project.direct_user_a_id and int(project.direct_user_a_id) != int(user_id):
        peer_id = project.direct_user_a_id
    elif project.direct_user_b_id and int(project.direct_user_b_id) != int(user_id):
        peer_id = project.direct_user_b_id
    return User.query.get(peer_id) if peer_id else None


def _project_display_name_for_user(project: Project, user_id: int) -> str:
    peer = _direct_peer_for_user(project, user_id)
    if peer:
        return display_name_for_user(peer) or peer.email or project.name
    return project.name


def _accessible_projects_for_user(user) -> tuple[list[Project], list[Project], set[int]]:
    owned_projects = Project.query.filter_by(owner_id=user.id).all()
    direct_member_project_ids = [
        row.project_id for row in ProjectMember.query.filter_by(user_id=user.id).all()
    ]
    member_group_project_ids = [
        row.project_id
        for row in db.session.query(Group.project_id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(GroupMember.user_id == user.id)
        .all()
    ]
    member_project_ids = {
        int(project_id)
        for project_id in direct_member_project_ids + member_group_project_ids
        if project_id
    }
    accessible_project_ids = {
        int(project.id)
        for project in owned_projects
        if project and project.id
    }.union(member_project_ids)
    projects = (
        Project.query.filter(Project.id.in_(sorted(accessible_project_ids))).all()
        if accessible_project_ids
        else []
    )
    return projects, owned_projects, member_project_ids


def _is_complete_status(status: str | None) -> bool:
    return (status or "").strip().lower() in {
        "complete",
        "completed",
        "done",
        "closed",
        "pr closed",
        "pr merged",
    }


def _render_description(description: str | None, description_format: str | None, default_format: str) -> str:
    if not description:
        return ""
    fmt = (description_format or default_format or "").strip().lower()
    if fmt == "markdown":
        rendered = render_markdown(description, extensions=["extra", "sane_lists"])
        return sanitize_info_html(rendered)
    if fmt == "html":
        return sanitize_info_html(description)
    paragraphs = []
    for block in re.split(r"\n\s*\n", escape(description).strip()):
        if not block:
            continue
        paragraphs.append(f"<p>{block.replace('\\n', '<br />')}</p>")
    return "".join(paragraphs)


def _todo_bucket_rank(task: Task) -> tuple[int, str]:
    today = datetime.utcnow().date()
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
    info_payload = load_info_payload(task.info, task.link)
    due_mode = str((info_payload.get("meta") or {}).get("due_mode") or "").strip().lower()
    due_date = task.due_at.date() if task.due_at else None
    if due_mode == "asap" and not task.due_at:
        return (1, "asap")
    if due_date is None:
        return (10, "none")
    if due_date < today:
        return (0, "overdue")
    if due_date == today:
        return (1, "today")
    if due_date == today + timedelta(days=1):
        return (2, "tomorrow")
    if due_date < next_week_start:
        return (3, "this_week")
    if due_date < two_weeks_start:
        return (4, "next_week")
    if due_date < three_weeks_start:
        return (5, "two_weeks")
    if due_date < four_weeks_start:
        return (6, "three_weeks")
    if due_date < next_month_start:
        return (7, "this_month")
    if due_date < month_after_next_start:
        return (8, "next_month")
    return (9, "later")


def _serialize_division(division: Division, *, theme_name: str) -> dict:
    updated_at = getattr(division, "updated_at", None)
    return {
        "id": division.id,
        "name": division.name,
        "position": division.position,
        "color": division_effective_color(division, theme_name),
        "color_slot": division.color_slot,
        "custom_color": division.color,
        "created_at": division.created_at.isoformat() if division.created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


def _serialize_project(
    project: Project,
    *,
    user_id: int,
    theme_name: str,
    sidebar_pref,
) -> dict:
    info = load_info_payload(project.info, project.link)
    division_id = sidebar_pref.division_id if sidebar_pref else None
    updated_at = getattr(project, "updated_at", None)
    return {
        "id": project.id,
        "name": project.name,
        "display_name": _project_display_name_for_user(project, user_id),
        "owner_id": project.owner_id,
        "division_id": division_id,
        "position": project.position,
        "sidebar_position": sidebar_pref.position if sidebar_pref else None,
        "is_direct": bool(project.is_direct),
        "direct_user_a_id": project.direct_user_a_id,
        "direct_user_b_id": project.direct_user_b_id,
        "default_owner_calendar_opt_in": bool(project.default_owner_calendar_opt_in),
        "default_invitee_calendar_opt_in": bool(project.default_invitee_calendar_opt_in),
        "description": project.description,
        "description_format": project.description_format,
        "rendered_description": _render_description(project.description, project.description_format, "markdown"),
        "start_date": project.start_date.isoformat() if project.start_date else None,
        "end_date": project.end_date.isoformat() if project.end_date else None,
        "link": project.link,
        "info": info,
        "links": list(info.get("links") or []),
        "attachments": list(info.get("attachments") or []),
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "division_color": division_effective_color(None, theme_name) if division_id is None else None,
    }


def _serialize_group(group: Group) -> dict:
    info = load_info_payload(group.info, group.link)
    updated_at = getattr(group, "updated_at", None)
    return {
        "id": group.id,
        "project_id": group.project_id,
        "name": group.name,
        "position": group.position,
        "color": group.color,
        "description": group.description,
        "description_format": group.description_format,
        "rendered_description": _render_description(group.description, group.description_format, "markdown"),
        "link": group.link,
        "info": info,
        "links": list(info.get("links") or []),
        "attachments": list(info.get("attachments") or []),
        "created_at": group.created_at.isoformat() if group.created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


def _serialize_assignment(assignment: Assignment, user_map: dict[int, User]) -> dict:
    account_user = user_map.get(int(assignment.user_id)) if assignment.user_id else None
    return {
        "id": assignment.id,
        "task_id": assignment.task_id,
        "user_id": assignment.user_id,
        "email": assignment.email,
        "status": assignment.status,
        "display_name": display_name_for_user(account_user) if account_user else None,
        "display_email": (account_user.email if account_user else assignment.email),
        "avatar_url": account_user.avatar_url if account_user else None,
        "created_at": assignment.created_at.isoformat() if assignment.created_at else None,
    }


def _serialize_task(
    task: Task,
    *,
    status_meta: dict,
    assignments: list[dict],
    comment_count: int,
    has_unread_comments: bool,
    github_meta: dict | None,
) -> dict:
    info = load_info_payload(task.info, task.link)
    meta = info.get("meta") or {}
    start_date = str(meta.get("start_date") or "").strip() or None
    due_mode = str(meta.get("due_mode") or "").strip().lower()
    updated_at = getattr(task, "updated_at", None)
    if due_mode not in {"asap", "date"}:
        due_mode = "date" if task.due_at else "none"
    return {
        "id": task.id,
        "project_id": task.project_id,
        "group_id": task.group_id,
        "creator_user_id": task.creator_user_id,
        "position": task.position,
        "title": task.title,
        "description": task.description,
        "description_format": task.description_format,
        "link": task.link,
        "info": info,
        "links": list(info.get("links") or []),
        "attachments": list(info.get("attachments") or []),
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "due_mode": due_mode,
        "start_date": start_date,
        "locked": bool(task.locked),
        "status": task.status,
        "status_mode": status_meta.get("mode") or "single",
        "status_percentage": status_meta.get("percentage_complete") or 0,
        "per_user_status_enabled": (status_meta.get("mode") or "single") == "multi",
        "assign_group_members": bool(task.assign_group_members),
        "group_assignment_members": serialize_group_assignment_members(task) if task.assign_group_members else [],
        "status_meta": status_meta,
        "assignments": assignments,
        "comment_count": int(comment_count or 0),
        "has_unread_comments": bool(has_unread_comments),
        "github_meta": github_meta or None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


def _serialize_notification(notification: TaskNotification) -> dict:
    return {
        "id": notification.id,
        "task_id": notification.task_id,
        "comment_id": notification.comment_id,
        "kind": notification.kind,
        "pinned": bool(notification.pinned),
        "read_at": notification.read_at.isoformat() if notification.read_at else None,
        "created_at": notification.created_at.isoformat() if notification.created_at else None,
        "emailed_at": notification.emailed_at.isoformat() if notification.emailed_at else None,
        "notification_data": notification.notification_data,
    }


def build_dashboard_bootstrap(user) -> dict:
    now_utc = datetime.utcnow()
    theme_name = normalize_theme_name(getattr(user, "theme_name", None))
    projects, owned_projects, member_project_ids = _accessible_projects_for_user(user)
    projects_by_id = {project.id: project for project in projects}
    standard_projects = [project for project in projects if not project.is_direct]
    direct_projects = [project for project in projects if project.is_direct]
    direct_projects.sort(
        key=lambda project: ((_project_display_name_for_user(project, user.id) or "").lower(), project.id)
    )

    divisions = (
        Division.query.filter_by(owner_id=user.id)
        .order_by(Division.position.asc(), Division.name.asc(), Division.id.asc())
        .all()
    )
    sidebar_pref_map = sidebar_preference_map(user.id, standard_projects, divisions)
    top_level_items = top_level_sidebar_items(standard_projects, divisions, sidebar_pref_map)

    groups = (
        Group.query.filter(Group.project_id.in_(list(projects_by_id.keys())))
        .order_by(Group.position.asc(), Group.id.asc())
        .all()
        if projects_by_id
        else []
    )
    groups_by_project: dict[int, list[Group]] = {project_id: [] for project_id in projects_by_id}
    for group in groups:
        groups_by_project.setdefault(group.project_id, []).append(group)
    tasks = (
        Task.query.filter(Task.project_id.in_(list(projects_by_id.keys())))
        .order_by(Task.position.asc(), Task.id.asc())
        .all()
        if projects_by_id
        else []
    )
    status_map = task_status_meta_map(tasks, viewer_user_id=user.id)

    task_ids = [task.id for task in tasks]
    assignment_rows = (
        Assignment.query.filter(Assignment.task_id.in_(task_ids))
        .order_by(Assignment.created_at.asc(), Assignment.id.asc())
        .all()
        if task_ids
        else []
    )
    assignment_user_ids = sorted({row.user_id for row in assignment_rows if row.user_id})
    assignment_users = {
        account_user.id: account_user
        for account_user in User.query.filter(User.id.in_(assignment_user_ids)).all()
    } if assignment_user_ids else {}
    github_user_map = dict(assignment_users)
    github_user_map[user.id] = user
    assignments_by_task: dict[int, list[dict]] = {}
    for row in assignment_rows:
        assignments_by_task.setdefault(row.task_id, []).append(_serialize_assignment(row, assignment_users))
    github_task_meta, _github_project_id = _build_github_task_meta(user.id, task_ids, github_user_map)

    comment_counts = {}
    if task_ids:
        comment_counts = {
            int(task_id): int(count or 0)
            for task_id, count in (
                db.session.query(TaskComment.task_id, db.func.count(TaskComment.id))
                .filter(TaskComment.task_id.in_(task_ids))
                .group_by(TaskComment.task_id)
                .all()
            )
            if task_id
        }
    unread_task_ids = {
        int(task_id)
        for (task_id,) in (
            db.session.query(TaskNotification.task_id)
            .filter(
                TaskNotification.user_id == user.id,
                TaskNotification.kind == "comment",
                TaskNotification.read_at.is_(None),
                TaskNotification.task_id.in_(task_ids if task_ids else [-1]),
            )
            .distinct()
            .all()
        )
        if task_id
    } if task_ids else set()

    todo_tasks = list(tasks)
    todo_tasks.sort(
        key=lambda task: (
            _todo_bucket_rank(task)[0],
            task.due_at.date().isoformat() if task.due_at else "9999-12-31",
            (_project_display_name_for_user(projects_by_id.get(task.project_id), user.id) or "").lower(),
            task.position or 0,
            task.id,
        )
    )

    notification_rows = (
        TaskNotification.query.filter(TaskNotification.user_id == user.id, TaskNotification.kind != "comment")
        .order_by(TaskNotification.created_at.desc(), TaskNotification.id.desc())
        .limit(100)
        .all()
    )
    discussion_rows = (
        UserDiscussionActivity.query.filter_by(user_id=user.id)
        .order_by(UserDiscussionActivity.updated_at.desc(), UserDiscussionActivity.id.desc())
        .limit(100)
        .all()
    )
    discussion_items = build_discussion_activity_items(discussion_rows, user_id=user.id)

    division_project_ids: dict[str, list[int]] = {str(division.id): [] for division in divisions}
    top_level_project_ids: list[int] = []
    for item in top_level_items:
        if item.get("type") == "project":
            top_level_project_ids.append(int(item["id"]))
        elif item.get("type") == "division":
            division = item.get("division")
            projects_in_division = item.get("projects") or []
            if division:
                division_project_ids[str(division.id)] = [int(project.id) for project in projects_in_division]

    payload = {
        "meta": {
            "user_id": user.id,
            "generated_at": now_utc.isoformat(),
            "cursor": now_utc.isoformat(),
            "schema_version": 3,
        },
        "entities": {
            "divisions": {
                str(division.id): _serialize_division(division, theme_name=theme_name)
                for division in divisions
            },
            "projects": {
                str(project.id): _serialize_project(
                    project,
                    user_id=user.id,
                    theme_name=theme_name,
                    sidebar_pref=sidebar_pref_map.get(project.id),
                )
                for project in projects
            },
            "groups": {
                str(group.id): _serialize_group(group)
                for group in groups
            },
            "tasks": {
                str(task.id): _serialize_task(
                    task,
                    status_meta=status_map.get(task.id) or {},
                    assignments=assignments_by_task.get(task.id, []),
                    comment_count=comment_counts.get(task.id, 0),
                    has_unread_comments=task.id in unread_task_ids,
                    github_meta=github_task_meta.get(task.id),
                )
                for task in tasks
            },
            "notifications": {
                str(row.id): _serialize_notification(row)
                for row in notification_rows
            },
        },
        "views": {
            "tree": {
                "division_order": [division.id for division in divisions],
                "top_level_project_ids": top_level_project_ids,
                "division_project_ids": division_project_ids,
                "project_group_ids": {
                    str(project_id): [group.id for group in groups_by_project.get(project_id, [])]
                    for project_id in projects_by_id
                },
            },
            "todo": {
                "task_ids": [task.id for task in todo_tasks],
            },
            "inbox": {
                "notification_ids": [row.id for row in notification_rows],
                "discussion_items": discussion_items,
            },
        },
        "access": {
            "manageable_project_ids": sorted(
                {project.id for project in owned_projects}.union(member_project_ids)
            ),
        },
    }
    return payload


def build_dashboard_changes(user, *, since: datetime) -> dict:
    payload = build_dashboard_bootstrap(user)
    divisions = payload.get("entities", {}).get("divisions", {})
    projects = payload.get("entities", {}).get("projects", {})
    groups = payload.get("entities", {}).get("groups", {})
    tasks = payload.get("entities", {}).get("tasks", {})
    notifications = payload.get("entities", {}).get("notifications", {})

    def changed_since(timestamp_value: str | None) -> bool:
        if not timestamp_value:
            return False
        try:
            return datetime.fromisoformat(timestamp_value) > since
        except ValueError:
            return False

    payload["meta"]["previous_cursor"] = since.isoformat()
    payload["changes"] = {
        "upserts": {
            "divisions": [
                row for row in divisions.values()
                if changed_since(row.get("updated_at") or row.get("created_at"))
            ],
            "projects": [
                row for row in projects.values()
                if changed_since(row.get("updated_at") or row.get("created_at"))
            ],
            "groups": [
                row for row in groups.values()
                if changed_since(row.get("updated_at") or row.get("created_at"))
            ],
            "tasks": [
                row for row in tasks.values()
                if changed_since(row.get("updated_at") or row.get("created_at"))
            ],
            "notifications": [
                row for row in notifications.values()
                if changed_since(row.get("created_at"))
            ],
        },
        "deletes": {
            "divisions": [],
            "projects": [],
            "groups": [],
            "tasks": [],
            "notifications": [],
        },
    }
    return payload
