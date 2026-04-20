from datetime import date, datetime
from html import escape
import json
from pathlib import Path
import re
from urllib.parse import urlparse
from sqlalchemy import or_

from flask import Blueprint, current_app, request, send_from_directory

from markdown import markdown as render_markdown
from app.auth import login_required
from app.collaborators import get_or_create_collaborator_profile
from app.dashboard_state import build_dashboard_bootstrap, build_dashboard_changes
from app.emailer import MailDeliveryError, send_magic_link_digest_email, send_magic_link_email
from app.extensions import db
from app.discussion_activity import (
    group_discussion_user_ids,
    project_discussion_user_ids,
    task_discussion_user_ids,
    upsert_discussion_activity,
)
from app.discussion_history import (
    log_group_history,
    log_project_history,
    log_task_history,
    serialized_discussion_events_for_entity,
)
from app.github_sync import GitHubSyncError, github_identity_for_user, should_sync_github_issues, sync_github_issues_for_user
from app.group_templates import serialize_group_template
from app.group_assignments import group_assignment_candidate_users, serialize_group_assignment_members, sync_group_task_assignments
from app.identity import find_user_by_email, normalize_email, search_users_by_identity
from app.info_utils import load_info_payload, normalize_info_payload, save_uploaded_file, sanitize_info_html
from app.favicon_cache import cache_favicons_for_links
from app.notification_preferences import user_notification_channel_enabled
from app.debug_tools import debug_print
from app.web_push import (
    active_web_push_subscriptions_for_user,
    delete_web_push_subscription,
    public_web_push_config,
    send_web_push_notification,
    upsert_web_push_subscription,
)
from app.realtime import (
    _serialize_group_payload,
    _serialize_project_payload_for_user,
    emit_assignment_updated,
    emit_division_created,
    emit_division_deleted,
    emit_division_updated,
    emit_group_created,
    emit_discussion_activity_updated,
    emit_group_updated,
    emit_group_deleted,
    emit_group_members_updated,
    emit_group_comment_created,
    emit_group_comment_deleted,
    emit_group_comment_updated,
    emit_group_reordered,
    emit_notification_state,
    emit_notification_preview_dismissed,
    emit_project_created,
    emit_project_deleted,
    emit_project_updated,
    emit_project_comment_created,
    emit_project_comment_deleted,
    emit_project_comment_updated,
    emit_project_created,
    emit_project_deleted,
    emit_project_members_updated,
    emit_sidebar_reordered,
    is_user_viewing_project,
    is_user_viewing_task,
    project_access_map,
    emit_task_comment_created,
    emit_task_comment_deleted,
    emit_task_comment_updated,
    emit_task_notification_updates,
    emit_tasks_updated,
    emit_task_updated,
    notification_payload_for_user,
    emit_user_notification_preview,
    is_user_viewing_discussion,
    queue_user_notification,
    queue_task_notifications,
)
from app.sidebar_layout import (
    apply_top_level_project_order,
    ensure_sidebar_preference,
    insert_project_position,
    resequence_division_projects,
    resequence_top_level_items,
    sidebar_preference_map,
    top_level_sidebar_items,
)
from app.task_status import effective_task_status_for_user, normalize_task_status, normalize_task_status_mode, set_task_user_status, task_status_meta, task_status_meta_map
from app.themes import DEFAULT_THEME_NAME, color_slot_from_palette, division_effective_color, normalize_theme_name, theme_palette
import secrets

from app.models import (
    Division,
    Task,
    Project,
    ProjectSidebarPreference,
    Assignment,
    Invite,
    User,
    Group,
    ProjectMember,
    GroupMember,
    UserEmail,
    DiscussionEvent,
    TaskComment,
    TaskFollower,
    TaskPrerequisite,
    TaskNotification,
    CollaboratorProfile,
    GroupTemplate,
    GroupTemplateTask,
    ProjectComment,
    GroupComment,
)
from app.utils import current_user, display_name_for_user, is_admin as user_is_admin


api_bp = Blueprint("api", __name__)


def _json_response_with_payload_header(payload: dict, status_code: int = 200):
    response = current_app.response_class(
        current_app.json.dumps(payload),
        status=status_code,
        mimetype="application/json",
    )
    try:
        response.headers["X-Termin-Payload-Bytes"] = str(len(response.get_data()))
    except Exception:
        response.headers["X-Termin-Payload-Bytes"] = "0"
    return response


URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
MENTION_PATTERN = re.compile(r"@\{([^{}\s]+)\}")
DESCRIPTION_FORMAT_OPTIONS = {"plain", "markdown", "restructuredtext", "html"}
DEFAULT_PROJECT_DESCRIPTION_FORMAT = "markdown"
DEFAULT_GROUP_DESCRIPTION_FORMAT = "markdown"
DEFAULT_TASK_DESCRIPTION_FORMAT = "markdown"
TASK_DUE_MODE_OPTIONS = {"none", "date", "asap"}
TASK_TYPE_OPTIONS = {"standard", "poll"}
TASK_LOCKED_PROTECTED_FIELDS = {
    "title",
    "due_at",
    "due_mode",
    "start_date",
    "status",
    "user_status",
    "status_user_id",
    "status_mode",
    "status_percentage",
    "per_user_status_enabled",
    "assign_group_members",
    "task_type",
    "poll",
}


def _task_is_locked(task: Task | None) -> bool:
    return bool(task and getattr(task, "locked", False))


def _task_payload_touches_locked_fields(payload: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return any(field in payload for field in TASK_LOCKED_PROTECTED_FIELDS)


def _locked_task_response():
    return {"error": "task is locked"}, 423


def _is_complete_status_value(status: str | None) -> bool:
    value = (status or "").strip().lower()
    return value in {"complete", "completed", "done", "closed", "pr closed", "pr merged"}


def _task_update_notification_kind(
    *,
    old_title: str | None,
    new_title: str | None,
    old_status: str | None,
    new_status: str | None,
    old_due_at,
    new_due_at,
    old_due_mode: str | None,
    new_due_mode: str | None,
    user_status: str | None,
) -> str:
    effective_status = (user_status if user_status is not None else new_status) or ""
    if _is_complete_status_value(effective_status) and not _is_complete_status_value(old_status):
        return "task_completed"
    if (old_due_mode or "none") != (new_due_mode or "none") or bool(old_due_at) != bool(new_due_at) or (old_due_at and new_due_at and old_due_at != new_due_at):
        if (new_due_mode or "none") == "asap":
            return "task_due_asap"
        if (new_due_mode or "none") == "none":
            return "task_due_cleared"
        return "task_due_changed"
    if (old_status or "").strip() != (new_status or "").strip() or user_status is not None:
        return "task_status_changed"
    if (old_title or "").strip() != (new_title or "").strip():
        return "task_renamed"
    return "task_update"


def _task_notification_actor_payload(user) -> dict:
    if not user:
        return {}
    return {
        "actor_user_id": user.id,
        "actor_name": display_name_for_user(user) or user.email or "Someone",
        "actor_avatar_url": user.avatar_url or "",
    }


def _task_changed_field_labels(
    *,
    old_title: str | None,
    new_title: str | None,
    old_status: str | None,
    new_status: str | None,
    old_due_at,
    new_due_at,
    old_due_mode: str | None,
    new_due_mode: str | None,
    old_start_date: str | None,
    new_start_date: str | None,
    old_info_payload: dict | None,
    new_info_payload: dict | None,
    old_per_user_status_enabled: bool,
    new_per_user_status_enabled: bool,
    old_status_mode: str | None,
    new_status_mode: str | None,
    old_assign_group_members: bool,
    new_assign_group_members: bool,
    old_follow_project_members: bool,
    new_follow_project_members: bool,
    old_description: str | None,
    new_description: str | None,
    old_description_format: str | None,
    new_description_format: str | None,
    old_task_type: str | None,
    new_task_type: str | None,
    old_poll: dict | None,
    new_poll: dict | None,
) -> list[str]:
    labels: list[str] = []
    if (old_title or "").strip() != (new_title or "").strip():
        labels.append("title")
    if (old_status or "").strip() != (new_status or "").strip():
        labels.append("status")
    if (old_due_mode or "none") != (new_due_mode or "none") or bool(old_due_at) != bool(new_due_at) or (old_due_at and new_due_at and old_due_at != new_due_at):
        labels.append("due date")
    if (old_start_date or "").strip() != (new_start_date or "").strip():
        labels.append("start date")
    old_links = list((old_info_payload or {}).get("links") or [])
    new_links = list((new_info_payload or {}).get("links") or [])
    if old_links != new_links:
        labels.append("links")
    old_html = str((old_info_payload or {}).get("html") or "")
    new_html = str((new_info_payload or {}).get("html") or "")
    if old_html != new_html:
        labels.append("notes")
    if (old_status_mode or "single") != (new_status_mode or "single"):
        labels.append("status mode")
    elif bool(old_per_user_status_enabled) != bool(new_per_user_status_enabled):
        labels.append("status mode")
    if bool(old_assign_group_members) != bool(new_assign_group_members):
        labels.append("assignment mode")
    if bool(old_follow_project_members) != bool(new_follow_project_members):
        labels.append("follower mode")
    if (old_description or "") != (new_description or ""):
        labels.append("description")
    if (old_description_format or "") != (new_description_format or ""):
        labels.append("description format")
    if _normalize_task_type(old_task_type, default="standard") != _normalize_task_type(new_task_type, default="standard"):
        labels.append("task type")
    if _normalize_task_poll_payload(old_poll) != _normalize_task_poll_payload(new_poll):
        labels.append("poll")
    return labels


def _project_changed_field_labels(
    *,
    old_name: str | None,
    new_name: str | None,
    old_links: list[str] | None,
    new_links: list[str] | None,
    old_description: str | None,
    new_description: str | None,
    old_description_format: str | None,
    new_description_format: str | None,
    old_start_date,
    new_start_date,
    old_end_date,
    new_end_date,
    old_division_id: int | None,
    new_division_id: int | None,
) -> list[str]:
    labels: list[str] = []
    if (old_name or "").strip() != (new_name or "").strip():
        labels.append("title")
    if list(old_links or []) != list(new_links or []):
        labels.append("links")
    if (old_description or "") != (new_description or ""):
        labels.append("description")
    if (old_description_format or "") != (new_description_format or ""):
        labels.append("description format")
    if old_start_date != new_start_date:
        labels.append("start date")
    if old_end_date != new_end_date:
        labels.append("end date")
    if (old_division_id or None) != (new_division_id or None):
        labels.append("division")
    return labels


def _group_changed_field_labels(
    *,
    old_name: str | None,
    new_name: str | None,
    old_links: list[str] | None,
    new_links: list[str] | None,
    old_description: str | None,
    new_description: str | None,
    old_description_format: str | None,
    new_description_format: str | None,
) -> list[str]:
    labels: list[str] = []
    if (old_name or "").strip() != (new_name or "").strip():
        labels.append("title")
    if list(old_links or []) != list(new_links or []):
        labels.append("links")
    if (old_description or "") != (new_description or ""):
        labels.append("description")
    if (old_description_format or "") != (new_description_format or ""):
        labels.append("description format")
    return labels


def _quoted_history_value(value: str | None, fallback: str) -> str:
    text = (value or "").strip() or fallback
    return '"' + text + '"'


def _task_due_history_value(*, due_mode: str | None, due_at) -> str:
    normalized_mode = str(due_mode or "none").strip().lower()
    if normalized_mode == "asap":
        return "ASAP"
    if normalized_mode != "date" or not due_at:
        return "no date"
    return '"' + due_at.strftime("%Y-%m-%d") + '"'


def _task_start_history_value(start_date: str | None) -> str:
    value = str(start_date or "").strip()
    return '"' + value + '"' if value else "no start date"


def _task_status_mode_label(value: str | None) -> str:
    normalized = normalize_task_status_mode(value, default="single")
    if normalized == "multi":
        return "multi"
    if normalized == "percent":
        return "percent"
    return "single"


def _task_poll_change_descriptions(old_poll: dict | None, new_poll: dict | None) -> list[str]:
    old_normalized = _normalize_task_poll_payload(old_poll)
    new_normalized = _normalize_task_poll_payload(new_poll)
    changes: list[str] = []

    old_question = str(old_normalized.get("question") or "").strip()
    new_question = str(new_normalized.get("question") or "").strip()
    if old_question != new_question:
        if new_question:
            changes.append("updated the poll question to " + _quoted_history_value(new_question, "poll question"))
        else:
            changes.append("cleared the poll question")

    old_allows_multiple = bool(old_normalized.get("allows_multiple"))
    new_allows_multiple = bool(new_normalized.get("allows_multiple"))
    if old_allows_multiple != new_allows_multiple:
        changes.append("enabled multiple selections for the poll" if new_allows_multiple else "disabled multiple selections for the poll")

    old_visibility = str(old_normalized.get("results_visibility") or "everyone").strip().lower() or "everyone"
    new_visibility = str(new_normalized.get("results_visibility") or "everyone").strip().lower() or "everyone"
    if old_visibility != new_visibility:
        visibility_label = "creator only" if new_visibility == "creator" else "everyone"
        changes.append("changed poll results visibility to " + _quoted_history_value(visibility_label, visibility_label))

    old_options = {
        str(item.get("id") or "").strip(): str(item.get("label") or "").strip()
        for item in list(old_normalized.get("options") or [])
        if str(item.get("id") or "").strip() and str(item.get("label") or "").strip()
    }
    new_options = {
        str(item.get("id") or "").strip(): str(item.get("label") or "").strip()
        for item in list(new_normalized.get("options") or [])
        if str(item.get("id") or "").strip() and str(item.get("label") or "").strip()
    }

    for option_id, label in new_options.items():
        if option_id not in old_options:
            changes.append("added option " + _quoted_history_value(label, "option") + " to the poll")
        elif old_options[option_id] != label:
            changes.append(
                "renamed poll option "
                + _quoted_history_value(old_options[option_id], "option")
                + " to "
                + _quoted_history_value(label, "option")
            )

    for option_id, label in old_options.items():
        if option_id not in new_options:
            changes.append("removed option " + _quoted_history_value(label, "option") + " from the poll")

    return changes


def _task_update_notification_changed_fields(changed_fields: list[str], *, old_poll: dict | None = None, new_poll: dict | None = None) -> list[str]:
    detailed: list[str] = []
    for field in changed_fields or []:
        normalized = str(field or "").strip().lower()
        if normalized == "poll":
            poll_changes = _task_poll_change_descriptions(old_poll, new_poll)
            if poll_changes:
                detailed.extend(poll_changes)
            continue
        detailed.append(str(field))
    return detailed


def _task_update_history_bodies(
    *,
    actor_name: str,
    task_title: str | None,
    changed_fields: list[str],
    new_title: str | None,
    new_due_mode: str | None,
    new_due_at,
    new_start_date: str | None,
    new_status: str | None,
    new_status_mode: str | None,
    new_status_percentage: int | None,
    old_locked: bool | None = None,
    new_locked: bool | None = None,
    old_poll: dict | None = None,
    new_poll: dict | None = None,
    assignments: list | None = None,
    followers: list | None = None,
) -> tuple[str, str]:
    clauses_task: list[str] = []
    clauses_scoped: list[str] = []
    title_label = _quoted_history_value(task_title, "Task")
    changed = {str(value).strip().lower() for value in changed_fields}
    if "title" in changed:
        next_title = _quoted_history_value(new_title, "Untitled")
        clauses_task.append("changed the title to " + next_title)
        clauses_scoped.append("changed the title of task " + title_label + " to " + next_title)
    if "due date" in changed:
        due_label = _task_due_history_value(due_mode=new_due_mode, due_at=new_due_at)
        clauses_task.append("updated the due date to " + due_label)
        clauses_scoped.append("updated the due date of task " + title_label + " to " + due_label)
    if "start date" in changed:
        start_label = _task_start_history_value(new_start_date)
        clauses_task.append("updated the start date to " + start_label)
        clauses_scoped.append("updated the start date of task " + title_label + " to " + start_label)
    if "status" in changed:
        normalized_status = str(new_status or "").strip().lower()
        if normalized_status in {"complete", "completed", "done", "closed", "pr closed", "pr merged"}:
            clauses_task.append("marked as completed")
            clauses_scoped.append("marked task " + title_label + " as completed")
        else:
            status_label = _quoted_history_value(new_status, "open")
            clauses_task.append("updated the status to " + status_label)
            clauses_scoped.append("updated the status of task " + title_label + " to " + status_label)
    if "status mode" in changed:
        if normalize_task_status_mode(new_status_mode, default="single") == "percent" and new_status_percentage is not None:
            progress_label = '"' + str(max(0, min(100, int(new_status_percentage)))) + '%"'
            clauses_task.append("updated the progress to " + progress_label)
            clauses_scoped.append("updated the progress of task " + title_label + " to " + progress_label)
        else:
            mode_label = '"' + _task_status_mode_label(new_status_mode) + '"'
            clauses_task.append("changed the status mode to " + mode_label)
            clauses_scoped.append("changed the status mode of task " + title_label + " to " + mode_label)
    if "assignees" in changed:
        assignee_names = []
        for assignment in assignments or []:
            label = (
                (assignment.get("display_name") if isinstance(assignment, dict) else None)
                or (assignment.get("display_email") if isinstance(assignment, dict) else None)
                or (assignment.get("email") if isinstance(assignment, dict) else None)
                or ""
            ).strip()
            if label:
                assignee_names.append(label)
        if assignee_names:
            assignee_text = ", ".join(_quoted_history_value(value, value) for value in assignee_names[:3])
            clauses_task.append("assigned to " + assignee_text)
            clauses_scoped.append("assigned task " + title_label + " to " + assignee_text)
        else:
            clauses_task.append("updated the assignees")
            clauses_scoped.append("updated the assignees for task " + title_label)
    if "followers" in changed:
        follower_names = []
        for follower in followers or []:
            label = (
                (follower.get("display_name") if isinstance(follower, dict) else None)
                or (follower.get("display_email") if isinstance(follower, dict) else None)
                or ""
            ).strip()
            if label:
                follower_names.append(label)
        if follower_names:
            follower_text = ", ".join(_quoted_history_value(value, value) for value in follower_names[:3])
            clauses_task.append("added followers " + follower_text)
            clauses_scoped.append("added followers " + follower_text + " to task " + title_label)
        else:
            clauses_task.append("updated the followers")
            clauses_scoped.append("updated the followers for task " + title_label)
    if "lock" in changed:
        if bool(new_locked):
            clauses_task.append("locked the task")
            clauses_scoped.append("locked task " + title_label)
        else:
            clauses_task.append("unlocked the task")
            clauses_scoped.append("unlocked task " + title_label)
    for simple_label in ["links", "notes", "description", "description format", "assignment mode", "follower mode", "task type"]:
        if simple_label not in changed:
            continue
        clause = "updated the " + simple_label
        clauses_task.append(clause)
        clauses_scoped.append(clause + " for task " + title_label)
    if "poll" in changed:
        poll_clauses = _task_poll_change_descriptions(old_poll, new_poll)
        if poll_clauses:
            clauses_task.extend(poll_clauses)
            clauses_scoped.extend([
                clause.replace(" the poll", " for task " + title_label).replace(" to the poll", " to task " + title_label + "'s poll").replace(" from the poll", " from task " + title_label + "'s poll")
                if "poll" in clause else (clause + " for task " + title_label)
                for clause in poll_clauses
            ])
        else:
            clauses_task.append("updated the poll")
            clauses_scoped.append("updated the poll for task " + title_label)
    return (
        actor_name + " " + "; ".join(clauses_task) + ".",
        actor_name + " " + "; ".join(clauses_scoped) + ".",
    )


def _assignment_history_label(assignment_row) -> str:
    if not assignment_row:
        return '"someone"'
    if isinstance(assignment_row, dict):
        label = (
            assignment_row.get("display_name")
            or assignment_row.get("display_email")
            or assignment_row.get("email")
            or ""
        )
    else:
        label = (
            getattr(assignment_row, "display_name", None)
            or getattr(assignment_row, "display_email", None)
            or getattr(assignment_row, "email", None)
            or ""
        )
    return _quoted_history_value(str(label or "").strip(), "someone")


def _queue_project_shared_notification(member_user, actor_user, project) -> bool:
    if not member_user or not actor_user or not project:
        return False
    if int(actor_user.id) == int(member_user.id):
        return False
    preview_payload = {
        "id": "live-project-shared:" + str(project.id) + ":" + str(actor_user.id),
        "task_id": "",
        "project_id": project.id,
        "project_name": _project_display_name_for_user(project, member_user.id),
        "group_id": "",
        "group_name": "",
        "task_title": _project_display_name_for_user(project, member_user.id),
        "kind": "project_shared",
        "summary": "Added to project",
        "preview": (_task_notification_actor_payload(actor_user).get("actor_name") or "Someone") + " added you to " + _project_display_name_for_user(project, member_user.id),
        "comment_preview": "",
        "created_at": datetime.utcnow().isoformat(),
        "created_at_iso": datetime.utcnow().isoformat(),
        "read": False,
        "pinned": False,
        "detail_payload": {
            **_task_notification_actor_payload(actor_user),
            "project_id": project.id,
            "project_name": _project_display_name_for_user(project, member_user.id),
        },
    }
    push_enabled = user_notification_channel_enabled(member_user.id, "project_added", "push")
    email_enabled = user_notification_channel_enabled(member_user.id, "project_added", "email")
    if push_enabled:
        emit_user_notification_preview(member_user.id, preview_payload)
    try:
        if not push_enabled and not email_enabled:
            return False
        queue_user_notification(
            user_id=member_user.id,
            kind="project_shared",
            detail_payload={
                **_task_notification_actor_payload(actor_user),
                "project_id": project.id,
                "project_name": _project_display_name_for_user(project, member_user.id),
            },
        )
        db.session.commit()
        emit_notification_state(member_user.id)
        return True
    except Exception:
        db.session.rollback()
        current_app.logger.exception("project_shared notification failed")
        return False


def _queue_project_removed_notification(member_user, actor_user, project) -> bool:
    if not member_user or not actor_user or not project:
        return False
    if int(actor_user.id) == int(member_user.id):
        return False
    project_name = _project_display_name_for_user(project, member_user.id)
    preview_payload = {
        "id": "live-project-removed:" + str(project.id) + ":" + str(actor_user.id),
        "task_id": "",
        "project_id": project.id,
        "project_name": project_name,
        "group_id": "",
        "group_name": "",
        "task_title": project_name,
        "kind": "project_removed",
        "summary": "Removed from project",
        "preview": (_task_notification_actor_payload(actor_user).get("actor_name") or "Someone") + " removed you from " + project_name,
        "comment_preview": "",
        "created_at": datetime.utcnow().isoformat(),
        "created_at_iso": datetime.utcnow().isoformat(),
        "read": False,
        "pinned": False,
        "detail_payload": {
            **_task_notification_actor_payload(actor_user),
            "project_id": project.id,
            "project_name": project_name,
        },
    }
    push_enabled = user_notification_channel_enabled(member_user.id, "project_removed", "push")
    email_enabled = user_notification_channel_enabled(member_user.id, "project_removed", "email")
    if push_enabled:
        emit_user_notification_preview(member_user.id, preview_payload)
    try:
        if not push_enabled and not email_enabled:
            return False
        queue_user_notification(
            user_id=member_user.id,
            kind="project_removed",
            detail_payload={
                **_task_notification_actor_payload(actor_user),
                "project_id": project.id,
                "project_name": project_name,
            },
        )
        db.session.commit()
        emit_notification_state(member_user.id)
        return True
    except Exception:
        db.session.rollback()
        current_app.logger.exception("project_removed notification failed")
        return False


def _queue_group_assignment_notifications(task: Task, assignments: list[Assignment], actor_user) -> None:
    if not task or not assignments:
        return
    total_assignees = len(group_assignment_candidate_users(task))
    recipient_ids: set[int] = set()
    project_label = task.project.name if getattr(task, "project", None) and task.project.name else (task.title or "Task")
    for assignment in assignments:
        if not assignment.user_id:
            continue
        if actor_user and int(assignment.user_id) == int(actor_user.id):
            continue
        recipient_ids.add(int(assignment.user_id))
        queue_user_notification(
            user_id=assignment.user_id,
            kind="assignment_added",
            task_id=task.id,
            detail_payload={
                **_task_notification_actor_payload(actor_user),
                "assignee_user_id": assignment.user_id,
                "group_assign": True,
                "group_assign_total": total_assignees,
                "group_assign_other_count": max(total_assignees - 1, 0),
                "project_name": project_label,
                "task_title": task.title or "Task",
            },
        )
    if not recipient_ids:
        return
    db.session.commit()
    for recipient_id in sorted(recipient_ids):
        emit_notification_state(recipient_id)


def _assignment_recipient_phrase(
    task: Task,
    *,
    recipient_user_id: int,
    fallback_label: str = "",
) -> str:
    assignment_rows = (
        Assignment.query.filter_by(task_id=task.id)
        .order_by(Assignment.created_at.asc(), Assignment.id.asc())
        .all()
    )
    assigned_users: list[User] = []
    seen_ids: set[int] = set()
    for row in assignment_rows:
        if not row.user_id or row.user_id in seen_ids:
            continue
        assignee = User.query.get(row.user_id)
        if not assignee:
            continue
        seen_ids.add(row.user_id)
        assigned_users.append(assignee)
    if not assigned_users:
        return fallback_label.strip() or "you"

    if any(int(assignee.id) == int(recipient_user_id) for assignee in assigned_users):
        other_labels = [
            display_name_for_user(assignee) or assignee.email or "Someone"
            for assignee in assigned_users
            if int(assignee.id) != int(recipient_user_id)
        ]
        if not other_labels:
            return "you"
        if len(other_labels) == 1:
            return "you and " + other_labels[0]
        return "you and " + str(len(other_labels)) + " others"

    labels = [display_name_for_user(assignee) or assignee.email or "Someone" for assignee in assigned_users]
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return labels[0] + " and " + labels[1]
    return labels[0] + " and " + str(len(labels) - 1) + " others"


def _queue_assignment_added_notifications(task: Task, *, actor_user, fallback_assignee_label: str = "") -> None:
    if not task:
        return
    recipient_ids = {
        int(recipient_id)
        for recipient_id in _task_notification_user_ids(task)
        if recipient_id and (not actor_user or int(recipient_id) != int(actor_user.id))
    }
    if not recipient_ids:
        return
    for recipient_id in sorted(recipient_ids):
        queue_user_notification(
            user_id=recipient_id,
            kind="assignment_added",
            task_id=task.id,
            detail_payload={
                **_task_notification_actor_payload(actor_user),
                "task_title": task.title or "Task",
                "recipient_assignment_phrase": _assignment_recipient_phrase(
                    task,
                    recipient_user_id=recipient_id,
                    fallback_label=fallback_assignee_label,
                ),
            },
        )
    db.session.commit()
    for recipient_id in sorted(recipient_ids):
        emit_notification_state(recipient_id)


def _emit_comment_notification_previews(
    *,
    recipient_ids: set[int] | list[int],
    actor_user,
    task_id: int | None = None,
    project_id: int | None = None,
    group_id: int | None = None,
    task_title: str = "",
    project_name: str = "",
    preview: str = "",
    exclude_user_id: int | None = None,
) -> None:
    actor_name = (_task_notification_actor_payload(actor_user).get("actor_name") or "New message") if actor_user else "New message"
    actor_avatar_url = (_task_notification_actor_payload(actor_user).get("actor_avatar_url") or "") if actor_user else ""
    created_at = datetime.utcnow().isoformat()
    task = Task.query.get(task_id) if task_id else None
    for recipient_id in sorted({int(user_id) for user_id in (recipient_ids or []) if user_id}):
        if exclude_user_id is not None and int(recipient_id) == int(exclude_user_id):
            continue
        event_key = "task_message"
        if group_id:
            event_key = "group_message"
        elif project_id and not task_id:
            event_key = "project_message"
        if not user_notification_channel_enabled(recipient_id, event_key, "push", task=task):
            continue
        if task_id and is_user_viewing_discussion(recipient_id, "task", int(task_id)):
            continue
        if group_id and is_user_viewing_discussion(recipient_id, "group", int(group_id)):
            continue
        if project_id and not group_id and not task_id and is_user_viewing_discussion(recipient_id, "project", int(project_id)):
            continue
        live_id_parts = ["live-comment", str(task_id or ""), str(project_id or ""), str(group_id or ""), created_at]
        emit_user_notification_preview(
            recipient_id,
            {
                "id": ":".join(live_id_parts),
                "task_id": task_id or "",
                "project_id": project_id or "",
                "project_name": project_name or "",
                "group_id": group_id or "",
                "group_name": "",
                "task_title": task_title or "Task",
                "kind": "comment",
                "summary": actor_name + " commented",
                "preview": preview or "New comment",
                "comment_preview": preview or "New comment",
                "created_at": created_at,
                "created_at_iso": created_at,
                "read": False,
                "pinned": False,
                "sender_name": actor_name,
                "unread_count": 1,
                "detail_payload": {
                    "actor_name": actor_name,
                    "actor_avatar_url": actor_avatar_url,
                },
            },
        )


def _coerce_description_format(value: str | None, default: str) -> str | None:
    if value is None:
        return default
    normalized = (str(value) or "").strip().lower()
    if not normalized:
        return default
    if normalized not in DESCRIPTION_FORMAT_OPTIONS:
        return None
    return normalized


def _render_description(description: str | None, description_format: str | None, default_format: str) -> str:
    if not description:
        return ""
    fmt = (description_format or default_format or "").strip().lower()
    if fmt == "markdown":
        rendered = render_markdown(description, extensions=["extra", "sane_lists"])
        return sanitize_info_html(rendered)
    if fmt == "html":
        return sanitize_info_html(description)
    escaped = escape(description)
    paragraphs = []
    for block in re.split(r"\\n\\s*\\n", escaped.strip()):
        if not block:
            continue
        paragraphs.append(f"<p>{block.replace('\\n', '<br />')}</p>")
    return "".join(paragraphs)


def _render_comment_body(body: str | None) -> str:
    if not body:
        return ""
    rendered = render_markdown(_render_mentions_for_html(body), extensions=["extra", "sane_lists"])
    return sanitize_info_html(rendered)


def _render_mentions_for_display(body: str | None) -> str:
    text = str(body or "")
    if not text:
        return ""

    def replace(match):
      email = normalize_email(match.group(1))
      user = find_user_by_email(email)
      if not user:
          return match.group(0)
      label = display_name_for_user(user) or user.email or email
      return "@" + label

    return MENTION_PATTERN.sub(replace, text)


def _render_mentions_for_html(body: str | None) -> str:
    text = str(body or "")
    if not text:
        return ""

    def replace(match):
        email = normalize_email(match.group(1))
        user = find_user_by_email(email)
        if not user:
            return match.group(0)
        label = display_name_for_user(user) or user.email or email
        return (
            '<span class="discussion-mention-tag"'
            + ' data-mentioned-user-id="' + str(user.id) + '"'
            + ' data-mentioned-email="' + escape(email) + '"'
            + '>@' + escape(label) + '</span>'
        )

    return MENTION_PATTERN.sub(replace, text)


def _mentioned_users(body: str | None, *, exclude_user_id: int | None = None):
    users: list[User] = []
    seen_ids: set[int] = set()
    for match in MENTION_PATTERN.finditer(str(body or "")):
        email = normalize_email(match.group(1))
        if not email:
            continue
        user = find_user_by_email(email)
        if not user:
            continue
        if exclude_user_id is not None and int(user.id) == int(exclude_user_id):
            continue
        if int(user.id) in seen_ids:
            continue
        seen_ids.add(int(user.id))
        users.append(user)
    return users


def _task_due_mode(task: Task) -> str:
    info = load_info_payload(getattr(task, "info", None), getattr(task, "link", None))
    mode = str(info.get("meta", {}).get("due_mode") or "").strip().lower()
    if mode in {"asap", "date"}:
        return mode
    if task.due_at:
        return "date"
    return "none"


def _normalize_due_mode(value: str | None, *, fallback: str = "date") -> str:
    mode = (str(value or fallback).strip().lower() or fallback)
    return mode if mode in TASK_DUE_MODE_OPTIONS else fallback


def _task_start_date(task: Task) -> str:
    info = load_info_payload(getattr(task, "info", None), getattr(task, "link", None))
    return str((info.get("meta") or {}).get("start_date") or "").strip()


def _task_status_mode(task: Task) -> str:
    info = load_info_payload(task.info, task.link)
    legacy_meta_mode = (info.get("meta") or {}).get("status_mode")
    return normalize_task_status_mode(
        getattr(task, "status_mode", None) or legacy_meta_mode or ("multi" if bool(task.per_user_status_enabled) else "single"),
        default="single",
    )


def _set_task_status_mode(task: Task, status_mode_raw) -> tuple[bool, str | None]:
    value = normalize_task_status_mode(status_mode_raw, default="single")
    if value not in {"single", "multi", "percent"}:
        return False, "Invalid status_mode"
    info_payload = load_info_payload(task.info, task.link)
    meta = dict(info_payload.get("meta") or {})
    task.status_mode = value
    task.per_user_status_enabled = value == "multi"
    meta["status_mode"] = value
    if value == "percent":
        meta["status_percentage"] = str(_task_status_percentage(task))
    elif "status_percentage" in meta:
        meta.pop("status_percentage", None)
    info_payload["meta"] = meta
    task.info = normalize_info_payload(info_payload, task.link)
    return True, None


def _task_status_percentage(task: Task) -> int:
    info = load_info_payload(task.info, task.link)
    raw = str((info.get("meta") or {}).get("status_percentage") or "").strip()
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = 100 if normalize_task_status(task.status).lower() in {"complete", "completed", "done", "closed", "pr closed", "pr merged"} else 0
    return max(0, min(100, value))


def _normalize_task_type(value: str | None, *, default: str = "standard") -> str:
    normalized = str(value or default).strip().lower() or default
    return normalized if normalized in TASK_TYPE_OPTIONS else default


def _task_type(task: Task) -> str:
    info_payload = load_info_payload(task.info, task.link)
    meta = info_payload.get("meta") if isinstance(info_payload, dict) else {}
    if _task_poll_has_configuration((meta or {}).get("poll")):
        return "poll"
    return _normalize_task_type((meta or {}).get("task_type"), default="standard")


def _task_poll(task: Task) -> dict:
    info_payload = load_info_payload(task.info, task.link)
    meta = info_payload.get("meta") if isinstance(info_payload, dict) else {}
    poll = _normalize_task_poll_payload((meta or {}).get("poll"))
    return {
        "question": str(poll.get("question") or "").strip(),
        "allows_multiple": bool(poll.get("allows_multiple")),
        "results_visibility": str(poll.get("results_visibility") or "everyone").strip().lower() or "everyone",
        "options": list(poll.get("options") or []),
    }


def _normalize_task_poll_payload(poll_raw) -> dict:
    poll = poll_raw if isinstance(poll_raw, dict) else {}
    question = str(poll.get("question") or "").strip()
    allows_multiple = bool(poll.get("allows_multiple"))
    results_visibility = str(poll.get("results_visibility") or "everyone").strip().lower()
    if results_visibility not in {"everyone", "creator"}:
        results_visibility = "everyone"
    options_raw = poll.get("options")
    options: list[dict] = []
    seen_ids: set[str] = set()
    if isinstance(options_raw, list):
        for item in options_raw:
            if isinstance(item, dict):
                label = str(item.get("label") or "").strip()
                option_id = str(item.get("id") or "").strip()
            else:
                label = str(item or "").strip()
                option_id = ""
            if not label:
                continue
            if not option_id or option_id in seen_ids:
                option_id = secrets.token_hex(4)
                while option_id in seen_ids:
                    option_id = secrets.token_hex(4)
            seen_ids.add(option_id)
            options.append({"id": option_id, "label": label})
    valid_option_ids = {str(option.get("id") or "").strip() for option in options if str(option.get("id") or "").strip()}
    responses_raw = poll.get("responses")
    responses: list[dict] = []
    seen_response_keys: set[str] = set()
    if isinstance(responses_raw, list):
        for item in responses_raw:
            if not isinstance(item, dict):
                continue
            user_id_raw = item.get("user_id")
            try:
                user_id = int(user_id_raw) if user_id_raw not in (None, "", "null") else None
            except (TypeError, ValueError):
                user_id = None
            email = str(item.get("email") or "").strip().lower()
            response_key = f"user:{user_id}" if user_id is not None else (f"email:{email}" if email else "")
            if not response_key or response_key in seen_response_keys:
                continue
            option_ids_raw = item.get("option_ids")
            option_ids: list[str] = []
            if isinstance(option_ids_raw, list):
                for option_id in option_ids_raw:
                    normalized_option_id = str(option_id or "").strip()
                    if not normalized_option_id or normalized_option_id not in valid_option_ids:
                        continue
                    if normalized_option_id not in option_ids:
                        option_ids.append(normalized_option_id)
            if not allows_multiple and len(option_ids) > 1:
                option_ids = option_ids[:1]
            seen_response_keys.add(response_key)
            responses.append(
                {
                    "user_id": user_id,
                    "email": email,
                    "option_ids": option_ids,
                    "updated_at": str(item.get("updated_at") or "").strip() or datetime.utcnow().isoformat(),
                }
            )
    return {
        "question": question,
        "allows_multiple": allows_multiple,
        "results_visibility": results_visibility,
        "options": options,
        "responses": responses,
    }


def _task_poll_has_configuration(poll_payload: dict | None) -> bool:
    normalized = _normalize_task_poll_payload(poll_payload)
    return bool(normalized.get("question") or normalized.get("allows_multiple") or normalized.get("options"))


def _set_task_type(task: Task, task_type_raw) -> None:
    info_payload = load_info_payload(task.info, task.link)
    meta = dict(info_payload.get("meta") or {})
    task_type = _normalize_task_type(task_type_raw, default="standard")
    if task_type == "standard":
        meta.pop("task_type", None)
    else:
        meta["task_type"] = task_type
    info_payload["meta"] = meta
    task.info = normalize_info_payload(info_payload, task.link)


def _set_task_poll(task: Task, poll_raw) -> None:
    info_payload = load_info_payload(task.info, task.link)
    meta = dict(info_payload.get("meta") or {})
    existing_poll = _normalize_task_poll_payload(meta.get("poll"))
    normalized_poll = _normalize_task_poll_payload(poll_raw)
    if "responses" not in (poll_raw if isinstance(poll_raw, dict) else {}):
        normalized_poll["responses"] = list(existing_poll.get("responses") or [])
    meta["poll"] = normalized_poll
    info_payload["meta"] = meta
    task.info = normalize_info_payload(info_payload, task.link)


def _set_task_poll_response(task: Task, *, user_id: int | None = None, email: str | None = None, option_ids=None) -> dict:
    info_payload = load_info_payload(task.info, task.link)
    meta = dict(info_payload.get("meta") or {})
    poll_payload = _normalize_task_poll_payload(meta.get("poll"))
    valid_option_ids = {str(option.get("id") or "").strip() for option in poll_payload.get("options") or [] if str(option.get("id") or "").strip()}
    normalized_option_ids: list[str] = []
    if isinstance(option_ids, list):
        for option_id in option_ids:
            normalized_option_id = str(option_id or "").strip()
            if not normalized_option_id or normalized_option_id not in valid_option_ids:
                continue
            if normalized_option_id not in normalized_option_ids:
                normalized_option_ids.append(normalized_option_id)
    if not poll_payload.get("allows_multiple") and len(normalized_option_ids) > 1:
        normalized_option_ids = normalized_option_ids[:1]
    normalized_email = str(email or "").strip().lower()
    next_responses = []
    matched = False
    for row in list(poll_payload.get("responses") or []):
        row_user_id = row.get("user_id")
        row_email = str(row.get("email") or "").strip().lower()
        same_identity = False
        if user_id is not None and row_user_id is not None and int(row_user_id) == int(user_id):
            same_identity = True
        elif normalized_email and row_email == normalized_email:
            same_identity = True
        if same_identity:
            matched = True
            if normalized_option_ids:
                next_responses.append(
                    {
                        "user_id": int(user_id) if user_id is not None else None,
                        "email": normalized_email,
                        "option_ids": normalized_option_ids,
                        "updated_at": datetime.utcnow().isoformat(),
                    }
                )
            continue
        next_responses.append(row)
    if not matched and normalized_option_ids:
        next_responses.append(
            {
                "user_id": int(user_id) if user_id is not None else None,
                "email": normalized_email,
                "option_ids": normalized_option_ids,
                "updated_at": datetime.utcnow().isoformat(),
            }
        )
    poll_payload["responses"] = next_responses
    meta["poll"] = poll_payload
    info_payload["meta"] = meta
    task.info = normalize_info_payload(info_payload, task.link)
    return poll_payload


def _set_task_status_percentage(task: Task, percentage_raw) -> tuple[bool, str | None]:
    try:
        value = int(float(str(percentage_raw or "0").strip() or "0"))
    except (TypeError, ValueError):
        return False, "Invalid status_percentage"
    value = max(0, min(100, value))
    info_payload = load_info_payload(task.info, task.link)
    meta = dict(info_payload.get("meta") or {})
    meta["status_mode"] = "percent"
    meta["status_percentage"] = str(value)
    info_payload["meta"] = meta
    task.info = normalize_info_payload(info_payload, task.link)
    task.status_mode = "percent"
    task.per_user_status_enabled = False
    task.status = "complete" if value >= 100 else "open"
    return True, None


def _set_task_start_date(task: Task, start_date_raw) -> tuple[bool, str | None]:
    info_payload = load_info_payload(task.info, task.link)
    meta = dict(info_payload.get("meta") or {})
    value = str(start_date_raw or "").strip()
    if not value:
        meta.pop("start_date", None)
    else:
        try:
            date.fromisoformat(value)
        except ValueError:
            return False, "Invalid start_date format"
        meta["start_date"] = value
    info_payload["meta"] = meta
    task.info = normalize_info_payload(info_payload, task.link)
    return True, None


def _apply_task_due_payload(task: Task, *, due_at_raw, due_mode_raw) -> tuple[bool, str | None]:
    info_payload = load_info_payload(task.info, task.link)
    meta = dict(info_payload.get("meta") or {})
    mode = _normalize_due_mode(due_mode_raw, fallback="date" if due_at_raw else "none")
    if mode == "none":
        task.due_at = None
        meta.pop("due_mode", None)
        meta.pop("start_date", None)
    elif mode == "asap":
        task.due_at = None
        meta["due_mode"] = "asap"
        meta.pop("start_date", None)
    else:
        if due_at_raw == "":
            task.due_at = None
        elif due_at_raw:
            try:
                task.due_at = datetime.fromisoformat(due_at_raw)
            except ValueError:
                return False, "Invalid due_at format"
        meta["due_mode"] = "date"
    info_payload["meta"] = meta
    task.info = normalize_info_payload(info_payload, task.link)
    return True, None

@api_bp.get("/me")
@login_required
def me():
    user = current_user()
    return {"user": {"id": user.id, "email": user.email, "display_name": user.display_name}}


@api_bp.get("/dashboard-bootstrap")
@login_required
def dashboard_bootstrap():
    user = current_user()
    return _json_response_with_payload_header({"dashboard": build_dashboard_bootstrap(user)})


@api_bp.get("/dashboard-changes")
@login_required
def dashboard_changes():
    user = current_user()
    raw_cursor = str(request.args.get("cursor", "") or "").strip()
    if not raw_cursor:
        return {"error": "cursor query parameter is required"}, 400
    try:
        cursor = datetime.fromisoformat(raw_cursor.replace("Z", "+00:00"))
        if cursor.tzinfo is not None:
            cursor = cursor.astimezone().replace(tzinfo=None)
    except ValueError:
        return {"error": "cursor must be an ISO-8601 datetime"}, 400
    return _json_response_with_payload_header({"dashboard": build_dashboard_changes(user, since=cursor)})


@api_bp.get("/group-templates")
@login_required
def list_group_templates():
    user = current_user()
    templates = (
        GroupTemplate.query.filter_by(user_id=user.id)
        .order_by(GroupTemplate.updated_at.desc(), GroupTemplate.id.desc())
        .all()
    )
    return {
        "templates": [serialize_group_template(template) for template in templates],
    }


@api_bp.post("/markdown-preview")
@login_required
def markdown_preview():
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text") or "")
    html = sanitize_info_html(
        render_markdown(
            text,
            extensions=["extra", "sane_lists", "nl2br"],
            output_format="html5",
        )
    ) if text.strip() else ""
    return {
        "html": html,
    }


@api_bp.post("/projects/<int:project_id>/group-templates/<int:template_id>/apply")
@login_required
def apply_group_template(project_id: int, template_id: int):
    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if not _can_manage_project(user, project.id):
        return {"error": "unauthorized"}, 403
    template = GroupTemplate.query.filter_by(id=template_id, user_id=user.id).first()
    if not template:
        return {"error": "group template not found"}, 404
    template_tasks = (
        GroupTemplateTask.query.filter_by(group_template_id=template.id)
        .order_by(GroupTemplateTask.position.asc(), GroupTemplateTask.id.asc())
        .all()
    )
    template_task_rows = [
        {
            "title": str(row.title or "").strip(),
            "description": str(row.description or "").strip() or None,
        }
        for row in template_tasks
        if str(row.title or "").strip()
    ]
    if not template_task_rows:
        return {"error": "group template has no tasks"}, 400

    max_pos = db.session.query(db.func.max(Group.position)).filter_by(project_id=project.id).scalar() or 0
    palette = list(theme_palette(getattr(user, "theme_name", None)))
    group = Group(
        project_id=project.id,
        name=template.title,
        position=max_pos + 1,
        color=palette[max_pos % len(palette)] if palette else None,
    )
    db.session.add(group)
    db.session.flush()

    created_tasks: list[Task] = []
    for index, template_task in enumerate(template_task_rows, start=1):
        task = Task(
            project_id=project.id,
            group_id=group.id,
            creator_user_id=user.id,
            position=index,
            title=template_task["title"],
            description=template_task["description"],
            description_format="markdown" if template_task["description"] else "plain",
            info=normalize_info_payload(None),
            owner_calendar_opt_in=project.default_owner_calendar_opt_in,
        )
        db.session.add(task)
        created_tasks.append(task)
    db.session.flush()

    log_group_history(group, actor=user, action="created")
    for task in created_tasks:
        log_task_history(task, actor=user, action="created")
    db.session.commit()

    emit_group_created(group, actor_user_id=user.id)
    for task in created_tasks:
        emit_task_updated(task, action="created", actor_user_id=user.id)

    return {
        "group": _serialize_group_payload(group),
        "tasks": [_serialize_task_row(task, viewer_user_id=user.id) for task in created_tasks],
    }, 201


@api_bp.get("/web-push")
@login_required
def web_push_status():
    user = current_user()
    config = public_web_push_config()
    subscriptions = active_web_push_subscriptions_for_user(user.id)
    debug_print(
        "web-push",
        "status",
        "user_id=",
        user.id,
        "configured=",
        config["enabled"],
        "subscriptions=",
        len(subscriptions),
    )
    return {
        "configured": config["enabled"],
        "public_key": config["public_key"],
        "subscription_count": len(subscriptions),
        "subscriptions": [
            {
                "id": row.id,
                "endpoint": row.endpoint,
                "device_label": row.device_label or "",
                "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in subscriptions
        ],
    }


@api_bp.post("/web-push")
@login_required
def register_web_push_subscription():
    user = current_user()
    if not public_web_push_config().get("enabled"):
        return {"error": "web push is not configured"}, 409
    payload = request.get_json(silent=True) or {}
    subscription = payload.get("subscription") if isinstance(payload.get("subscription"), dict) else {}
    keys = subscription.get("keys") if isinstance(subscription.get("keys"), dict) else {}
    endpoint = str(subscription.get("endpoint") or "").strip()
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        return {"error": "invalid subscription payload"}, 400

    upsert_web_push_subscription(
        user_id=user.id,
        endpoint=endpoint,
        p256dh=p256dh,
        auth=auth,
        user_agent=request.headers.get("User-Agent"),
        device_label=str(payload.get("device_label") or "").strip() or None,
    )
    db.session.commit()
    subscriptions = active_web_push_subscriptions_for_user(user.id)
    debug_print(
        "web-push",
        "registered",
        "user_id=",
        user.id,
        "subscriptions=",
        len(subscriptions),
        "device_label=",
        str(payload.get("device_label") or "").strip() or "",
    )
    return {"ok": True, "subscription_count": len(subscriptions)}


@api_bp.delete("/web-push")
@login_required
def unregister_web_push_subscription():
    user = current_user()
    payload = request.get_json(silent=True) or {}
    endpoint = str(payload.get("endpoint") or "").strip()
    if not endpoint:
        return {"error": "endpoint is required"}, 400
    deleted = delete_web_push_subscription(user_id=user.id, endpoint=endpoint)
    if deleted:
        db.session.commit()
    subscriptions = active_web_push_subscriptions_for_user(user.id)
    debug_print(
        "web-push",
        "unregistered",
        "user_id=",
        user.id,
        "deleted=",
        bool(deleted),
        "subscriptions=",
        len(subscriptions),
    )
    return {"ok": True, "deleted": bool(deleted), "subscription_count": len(subscriptions)}


@api_bp.post("/web-push/test")
@login_required
def send_test_web_push():
    user = current_user()
    if not public_web_push_config().get("enabled"):
        return {"error": "web push is not configured"}, 400
    result = send_web_push_notification(
        user_id=user.id,
        payload={
            "title": "Termin notifications enabled",
            "body": "This browser can receive Termin push notifications.",
            "url": "/account",
            "tag": "termin:test-push",
        },
    )
    if result["subscription_count"] <= 0:
        return {
            "ok": False,
            "error": "no active browser push subscription is registered for this account",
            **result,
        }, 409
    if result["sent_count"] <= 0:
        first_error = next(
            (
                item.get("error")
                for item in result.get("failures") or []
                if item.get("error")
            ),
            "browser push could not be delivered",
        )
        return {
            "ok": False,
            "error": first_error,
            **result,
        }, 502
    return {"ok": True, **result}


@api_bp.get("/notifications")
@login_required
def notifications_feed():
    user = current_user()
    return notification_payload_for_user(user.id)


@api_bp.post("/notifications/<int:notification_id>/dismiss")
@login_required
def dismiss_notification(notification_id: int):
    user = current_user()
    notification = TaskNotification.query.filter_by(id=notification_id, user_id=user.id).first()
    if not notification:
        return {"error": "notification not found"}, 404
    notification.read_at = datetime.utcnow()
    notification.pinned = False
    db.session.commit()
    emit_notification_state(user.id)
    return {"status": "ok"}, 200


@api_bp.post("/notifications/preview/dismiss")
@login_required
def dismiss_notification_preview():
    user = current_user()
    payload = request.get_json(silent=True) or {}
    notification_id = str(payload.get("notification_id") or "").strip()
    if not notification_id:
        return {"error": "notification_id is required"}, 400
    emit_notification_preview_dismissed(user.id, notification_id)
    return {"status": "ok"}, 200


@api_bp.post("/notifications/<int:notification_id>/pin")
@login_required
def pin_notification(notification_id: int):
    user = current_user()
    notification = TaskNotification.query.filter_by(id=notification_id, user_id=user.id).first()
    if not notification:
        return {"error": "notification not found"}, 404
    notification.pinned = True
    db.session.commit()
    emit_notification_state(user.id)
    return {"status": "ok"}, 200


@api_bp.post("/notifications/<int:notification_id>/unpin")
@login_required
def unpin_notification(notification_id: int):
    user = current_user()
    notification = TaskNotification.query.filter_by(id=notification_id, user_id=user.id).first()
    if not notification:
        return {"error": "notification not found"}, 404
    notification.pinned = False
    db.session.commit()
    emit_notification_state(user.id)
    return {"status": "ok"}, 200


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
    return _can_access_project(user, task.project_id)


def _task_notification_user_ids(task: Task) -> set[int]:
    user_ids: set[int] = set()
    if task.creator_user_id:
        user_ids.add(task.creator_user_id)
    user_ids.update(
        row.user_id
        for row in Assignment.query.filter_by(task_id=task.id).all()
        if row.user_id
    )
    user_ids.update(
        row.user_id
        for row in TaskFollower.query.filter_by(task_id=task.id).all()
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
        label = display_name_for_user(author) or "Unknown user"
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
        "rendered_body": _render_comment_body(comment.body),
        "created_at": comment.created_at.isoformat(),
        "updated_at": comment.updated_at.isoformat(),
        "author": author_payload,
    }


def _serialize_simple_comment(comment, author: User | None, *, project_id: int | None = None, group_id: int | None = None) -> dict:
    label = display_name_for_user(author) or "Unknown user"
    author_payload = {
        "id": author.id if author else None,
        "display_name": label,
        "email": author.email if author else None,
        "avatar_url": author.avatar_url if author else None,
        "kind": "user",
    }
    payload = {
        "id": comment.id,
        "user_id": comment.user_id,
        "body": comment.body,
        "rendered_body": _render_comment_body(comment.body),
        "created_at": comment.created_at.isoformat(),
        "updated_at": comment.updated_at.isoformat(),
        "author": author_payload,
    }
    if project_id is not None:
        payload["project_id"] = project_id
    if group_id is not None:
        payload["group_id"] = group_id
    return payload


def _serialize_assignment_row(assignment: Assignment) -> dict:
    account_user = User.query.get(assignment.user_id) if assignment.user_id else None
    return {
        "id": assignment.id,
        "task_id": assignment.task_id,
        "user_id": assignment.user_id,
        "email": assignment.email,
        "status": assignment.status,
        "display_name": account_user.display_name if account_user else None,
        "display_email": account_user.email if account_user else assignment.email,
        "avatar_url": account_user.avatar_url if account_user else None,
    }


def _serialize_follower_row(follower: TaskFollower) -> dict:
    account_user = User.query.get(follower.user_id) if follower.user_id else None
    return {
        "id": follower.id,
        "task_id": follower.task_id,
        "user_id": follower.user_id,
        "display_name": account_user.display_name if account_user else None,
        "display_email": account_user.email if account_user else None,
        "avatar_url": account_user.avatar_url if account_user else None,
    }


def _serialize_task_prerequisite_row(prerequisite: TaskPrerequisite) -> dict | None:
    task = Task.query.get(prerequisite.prerequisite_task_id)
    if not task:
        return None
    status_meta = task_status_meta(task)
    return {
        "id": prerequisite.id,
        "task_id": prerequisite.task_id,
        "prerequisite_task_id": prerequisite.prerequisite_task_id,
        "title": task.title,
        "project_id": task.project_id,
        "group_id": task.group_id,
        "status": task.status,
        "status_mode": _task_status_mode(task),
        "status_percentage": _task_status_percentage(task),
        "status_meta": status_meta,
        "prereq_blocked": bool(status_meta.get("prereq_blocked")) if isinstance(status_meta, dict) else False,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "due_mode": _task_due_mode(task),
        "start_date": _task_start_date(task),
        "locked": bool(task.locked),
    }


def _serialize_task_prerequisites(task_id: int) -> list[dict]:
    rows = (
        TaskPrerequisite.query.filter_by(task_id=task_id)
        .order_by(TaskPrerequisite.created_at.asc(), TaskPrerequisite.id.asc())
        .all()
    )
    serialized: list[dict] = []
    for row in rows:
        payload = _serialize_task_prerequisite_row(row)
        if payload:
            serialized.append(payload)
    return serialized


def _serialize_task_dependent_row(prerequisite: TaskPrerequisite) -> dict | None:
    task = Task.query.get(prerequisite.task_id)
    if not task:
        return None
    status_meta = task_status_meta(task)
    return {
        "id": prerequisite.id,
        "task_id": prerequisite.task_id,
        "prerequisite_task_id": prerequisite.prerequisite_task_id,
        "dependent_task_id": prerequisite.task_id,
        "title": task.title,
        "project_id": task.project_id,
        "group_id": task.group_id,
        "status": task.status,
        "status_mode": _task_status_mode(task),
        "status_percentage": _task_status_percentage(task),
        "status_meta": status_meta,
        "prereq_blocked": bool(status_meta.get("prereq_blocked")) if isinstance(status_meta, dict) else False,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "due_mode": _task_due_mode(task),
        "start_date": _task_start_date(task),
        "locked": bool(task.locked),
    }


def _serialize_task_dependents(task_id: int) -> list[dict]:
    rows = (
        TaskPrerequisite.query.filter_by(prerequisite_task_id=task_id)
        .order_by(TaskPrerequisite.created_at.asc(), TaskPrerequisite.id.asc())
        .all()
    )
    serialized: list[dict] = []
    for row in rows:
        payload = _serialize_task_dependent_row(row)
        if payload:
            serialized.append(payload)
    return serialized


def _task_prerequisite_would_create_cycle(task_id: int, prerequisite_task_id: int) -> bool:
    if int(task_id) == int(prerequisite_task_id):
        return True
    pending = [int(prerequisite_task_id)]
    seen: set[int] = set()
    while pending:
        current_id = pending.pop()
        if current_id in seen:
            continue
        seen.add(current_id)
        if current_id == int(task_id):
            return True
        next_ids = [
            int(next_id)
            for (next_id,) in db.session.query(TaskPrerequisite.prerequisite_task_id)
            .filter(TaskPrerequisite.task_id == current_id)
            .all()
            if next_id
        ]
        pending.extend(next_ids)
    return False


def _task_dependent_ids(task_id: int) -> list[int]:
    return [
        int(next_id)
        for (next_id,) in db.session.query(TaskPrerequisite.task_id)
        .filter(TaskPrerequisite.prerequisite_task_id == task_id)
        .all()
        if next_id
    ]


def _task_impacted_ids(seed_task_ids) -> list[int]:
    pending = [int(task_id) for task_id in (seed_task_ids or []) if task_id]
    seen: set[int] = set()
    ordered: list[int] = []
    while pending:
        current_id = pending.pop(0)
        if current_id in seen:
            continue
        seen.add(current_id)
        ordered.append(current_id)
        pending.extend(_task_dependent_ids(current_id))
    return ordered


def _touch_tasks(task_ids) -> list[Task]:
    normalized_ids = [int(task_id) for task_id in (task_ids or []) if task_id]
    if not normalized_ids:
        return []
    tasks = Task.query.filter(Task.id.in_(normalized_ids)).all()
    if not tasks:
        return []
    touched_at = datetime.utcnow()
    for task in tasks:
        task.updated_at = touched_at
    db.session.commit()
    return tasks


def _project_access_user_ids(project_id: int) -> list[int]:
    return sorted(
        int(user_id)
        for user_id in project_access_map(project_id).keys()
        if user_id
    )


def _task_follow_project_members(task: Task) -> bool:
    info_payload = load_info_payload(task.info, task.link)
    meta = info_payload.get("meta") if isinstance(info_payload, dict) else {}
    return str((meta or {}).get("follow_project_members") or "").strip().lower() in {"1", "true", "yes", "on"}


def _set_task_follow_project_members(task: Task, enabled: bool) -> None:
    info_payload = load_info_payload(task.info, task.link)
    meta = dict(info_payload.get("meta") or {})
    if enabled:
        meta["follow_project_members"] = "1"
    else:
        meta.pop("follow_project_members", None)
    info_payload["meta"] = meta
    task.info = normalize_info_payload(info_payload, task.link)


def _serialize_project_follower_members(task: Task) -> list[dict]:
    members = list(project_access_map(task.project_id).values())
    members.sort(key=lambda item: ((item.get("display_name") or item.get("email") or "").lower(), item.get("id") or 0))
    return members


def _account_user_has_project_access(project_id: int, account_user_id: int | None) -> bool:
    if not account_user_id:
        return False
    project = Project.query.get(project_id)
    if project and int(project.owner_id or 0) == int(account_user_id):
        return True
    if ProjectMember.query.filter_by(project_id=project_id, user_id=account_user_id).first():
        return True
    return (
        db.session.query(Group.id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(Group.project_id == project_id, GroupMember.user_id == account_user_id)
        .first()
        is not None
    )


def _assignment_requires_project_share_payload(project: Project | None, account_user: User | None) -> dict | None:
    if not project or not account_user:
        return None
    if _account_user_has_project_access(project.id, account_user.id):
        return None
    return {
        "error": "project share required",
        "requires_project_member": True,
        "project": {
            "id": project.id,
            "name": project.name,
        },
        "user": {
            "id": account_user.id,
            "email": account_user.email,
            "display_name": account_user.display_name,
            "avatar_url": account_user.avatar_url,
        },
    }


def _merge_comment_links(item, body: str) -> bool:
    candidates = []
    for match in URL_PATTERN.findall(body or ""):
        link = match.rstrip('.,;:!?)]}\'"')
        parsed = urlparse(link)
        if parsed.scheme not in {"http", "https"}:
            continue
        if link not in candidates:
            candidates.append(link)
    if not candidates:
        return False
    info_payload = load_info_payload(getattr(item, "info", None), getattr(item, "link", None))
    links = list(info_payload.get("links", []))
    changed = False
    for link in candidates:
        if link in links:
            continue
        links.append(link)
        changed = True
    if not changed:
        return False
    info_payload["links"] = links
    item.link = links[0] if links else None
    item.info = normalize_info_payload(info_payload, item.link)
    try:
        cache_favicons_for_links(current_app._get_current_object(), links)
    except Exception:
        pass
    return True


def _serialize_task_row(task: Task, *, viewer_user_id: int | None = None) -> dict:
    info = load_info_payload(task.info)
    assignments = (
        Assignment.query.filter_by(task_id=task.id)
        .order_by(Assignment.created_at.asc(), Assignment.id.asc())
        .all()
    )
    followers = (
        TaskFollower.query.filter_by(task_id=task.id)
        .order_by(TaskFollower.created_at.asc(), TaskFollower.id.asc())
        .all()
    )
    status_meta = task_status_meta(task, viewer_user_id=viewer_user_id)
    creator = User.query.get(task.creator_user_id) if task.creator_user_id else None
    return {
        "id": task.id,
        "project_id": task.project_id,
        "group_id": task.group_id,
        "locked": bool(task.locked),
        "creator": {
            "id": creator.id,
            "display_name": creator.display_name,
            "email": creator.email,
            "avatar_url": creator.avatar_url,
        } if creator else None,
        "position": task.position,
        "title": task.title,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "due_mode": _task_due_mode(task),
        "start_date": _task_start_date(task),
        "status": task.status,
        "task_type": _task_type(task),
        "poll": _task_poll(task),
        "status_mode": _task_status_mode(task),
        "status_percentage": _task_status_percentage(task),
        "per_user_status_enabled": _task_status_mode(task) == "multi",
        "assign_group_members": bool(task.assign_group_members),
        "group_assignment_members": serialize_group_assignment_members(task) if task.assign_group_members else [],
        "follow_project_members": _task_follow_project_members(task),
        "project_follower_members": _serialize_project_follower_members(task) if _task_follow_project_members(task) else [],
        "status_meta": status_meta,
        "description": task.description,
        "description_format": task.description_format or DEFAULT_TASK_DESCRIPTION_FORMAT,
        "rendered_description": _render_description(
            task.description,
            task.description_format,
            DEFAULT_TASK_DESCRIPTION_FORMAT,
        ),
        "info": info,
        "assignments": [_serialize_assignment_row(row) for row in assignments],
        "followers": [_serialize_follower_row(row) for row in followers],
        "prerequisites": _serialize_task_prerequisites(task.id),
        "dependents": _serialize_task_dependents(task.id),
    }


def _can_manage_project(user, project_id: int) -> bool:
    if Project.query.filter_by(id=project_id, owner_id=user.id).first() is not None:
        return True
    if ProjectMember.query.filter_by(project_id=project_id, user_id=user.id).first() is not None:
        return True
    return (
        db.session.query(Group.id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(Group.project_id == project_id, GroupMember.user_id == user.id)
        .first()
        is not None
    )


def _can_manage_task_bucket(user, project_id: int, group_id: int | None) -> bool:
    return _can_manage_project(user, project_id)


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
    return False


def _can_access_group(user, group_id: int) -> bool:
    group = Group.query.get(group_id)
    if not group:
        return False
    return _can_access_project(user, group.project_id)


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
    accessible_project_ids = list(
        {project.id for project in owned_projects}
        .union(member_project_ids)
        .union(member_group_project_ids)
    )
    return Project.query.filter(Project.id.in_(accessible_project_ids)).all() if accessible_project_ids else []


def _project_display_name_for_user(project: Project, user_id: int) -> str:
    if project and getattr(project, "is_direct", False):
        peer_id = None
        if project.direct_user_a_id and int(project.direct_user_a_id) != int(user_id):
            peer_id = project.direct_user_a_id
        elif project.direct_user_b_id and int(project.direct_user_b_id) != int(user_id):
            peer_id = project.direct_user_b_id
        if peer_id:
            peer = User.query.get(peer_id)
            if peer:
                return peer.display_name or peer.email or project.name
    return project.name if project else ""


def _sidebar_order_tokens(user) -> list[str]:
    accessible_projects = _accessible_projects_for_user(user)
    divisions = Division.query.filter_by(owner_id=user.id).order_by(Division.position.asc(), Division.id.asc()).all()
    pref_map = sidebar_preference_map(user.id, accessible_projects, divisions)
    items = top_level_sidebar_items(accessible_projects, divisions, pref_map)
    return [f"{item['type']}:{item['id']}" for item in items]


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
    account_user_id: int | None = None,
    email: str | None = None,
) -> Assignment | None:
    query = Assignment.query
    if task_id is None:
        query = query.filter(Assignment.task_id.is_(None))
    else:
        query = query.filter_by(task_id=task_id)
    normalized_email = (email or "").strip().lower()
    if account_user_id:
        existing = query.filter_by(user_id=account_user_id).first()
        if existing:
            return existing
    if normalized_email:
        return query.filter(db.func.lower(Assignment.email) == normalized_email).first()
    return None


def _reset_existing_assignment(existing: Assignment) -> Assignment:
    existing.status = "assigned" if existing.user_id else "draft"
    Invite.query.filter_by(assignment_id=existing.id).delete(synchronize_session=False)
    return existing


def _matching_assignments_for_target(
    *,
    task_id: int,
    account_user_id: int | None = None,
    email: str | None = None,
) -> list[Assignment]:
    query = Assignment.query.filter_by(task_id=task_id)
    normalized_email = (email or "").strip().lower()
    if account_user_id:
        return query.filter_by(user_id=account_user_id).order_by(Assignment.id.asc()).all()
    if normalized_email:
        return query.filter(db.func.lower(Assignment.email) == normalized_email).order_by(Assignment.id.asc()).all()
    return []


def _collapse_duplicate_task_assignments(
    *,
    task_id: int,
    account_user_id: int | None = None,
    email: str | None = None,
) -> Assignment | None:
    matches = _matching_assignments_for_target(task_id=task_id, account_user_id=account_user_id, email=email)
    if not matches:
        return None
    primary = matches[0]
    for duplicate in matches[1:]:
        Invite.query.filter_by(assignment_id=duplicate.id).delete(synchronize_session=False)
        db.session.delete(duplicate)
    return primary


@api_bp.post("/tasks")
@login_required
def create_task():
    payload = request.get_json(silent=True) or {}
    user = current_user()
    title = (payload.get("title") or "").strip()
    project_id = payload.get("project_id")
    group_id = payload.get("group_id")
    due_at = payload.get("due_at")
    due_mode = payload.get("due_mode")
    start_date = payload.get("start_date")
    assignee_email = payload.get("assignee_email")
    task_type = payload.get("task_type")
    poll = payload.get("poll")

    if not title or not project_id:
        return {"error": "title and project_id are required"}, 400

    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404

    group = None
    target_group_id = None if group_id in (None, "", "null") else group_id
    if target_group_id is not None:
        try:
            target_group_id = int(target_group_id)
        except (TypeError, ValueError):
            return {"error": "invalid group id"}, 400
        group = Group.query.filter_by(id=target_group_id, project_id=project.id).first()
        if not group:
            return {"error": "group not found"}, 404

    if not _can_manage_task_bucket(user, project.id, group.id if group else None):
        return {"error": "unauthorized"}, 403

    task = Task(
        project_id=project.id,
        group_id=group.id if group else None,
        creator_user_id=user.id,
        position=_next_task_position(project.id, group.id if group else None),
        title=title,
        description=payload.get("description"),
        info=normalize_info_payload(payload.get("info"), payload.get("link")),
        due_at=None,
        owner_calendar_opt_in=project.default_owner_calendar_opt_in,
    )
    ok, error = _apply_task_due_payload(task, due_at_raw=due_at, due_mode_raw=due_mode)
    if not ok:
        return {"error": error}, 400
    ok, error = _set_task_start_date(task, start_date if _task_due_mode(task) == "date" else "")
    if not ok:
        return {"error": error}, 400
    normalized_poll = _normalize_task_poll_payload(poll)
    _set_task_type(task, task_type)
    _set_task_poll(task, normalized_poll)
    db.session.add(task)
    db.session.commit()
    assignment_payload = None
    if assignee_email:
        email = assignee_email.strip().lower()
        account_user = find_user_by_email(email)
        project_share_required = _assignment_requires_project_share_payload(project, account_user)
        if project_share_required:
            return project_share_required, 200
        existing_assignment = _existing_assignment_for_target(
            task_id=task.id,
            account_user_id=account_user.id if account_user else None,
            email=email,
        )
        if existing_assignment:
            _reset_existing_assignment(existing_assignment)
            log_task_history(task, actor=user, action="created")
            db.session.commit()
            task = Task.query.get(task.id) or task
            task_payload = _serialize_task_row(task, viewer_user_id=user.id)
            emit_task_updated(task, action="created", actor_user_id=user.id)
            return {
                "task": task_payload,
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

    queue_task_notifications(
        task,
        exclude_user_id=user.id,
        kind="task_created",
        detail_payload=_task_notification_actor_payload(user),
    )
    log_task_history(task, actor=user, action="created")
    db.session.commit()
    task = Task.query.get(task.id) or task
    task_payload = _serialize_task_row(task, viewer_user_id=user.id)
    emit_task_updated(task, action="created", actor_user_id=user.id)
    emit_task_notification_updates(task, exclude_user_id=user.id)
    return {
        "task": task_payload,
        "id": task.id,
        "title": task.title,
        "assignment": assignment_payload,
        "info": _info_payload_for(task),
    }, 201


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

    old_title = task.title
    old_status = task.status
    old_due_at = task.due_at
    old_due_mode = _task_due_mode(task)
    old_start_date = _task_start_date(task)
    old_status_mode = _task_status_mode(task)
    old_info_payload = _info_payload_for(task)
    old_per_user_status_enabled = bool(task.per_user_status_enabled)
    old_assign_group_members = bool(task.assign_group_members)
    old_follow_project_members = _task_follow_project_members(task)
    old_description = task.description
    old_description_format = task.description_format or DEFAULT_TASK_DESCRIPTION_FORMAT
    old_task_type = _task_type(task)
    old_poll = _task_poll(task)
    old_locked = bool(task.locked)

    title = payload.get("title")
    link = payload.get("link")
    links = payload.get("links")
    status = payload.get("status")
    user_status = payload.get("user_status")
    status_user_id = payload.get("status_user_id")
    due_at = payload.get("due_at")
    due_mode = payload.get("due_mode")
    start_date = payload.get("start_date")
    status_mode = payload.get("status_mode")
    status_percentage = payload.get("status_percentage")
    task_type = payload.get("task_type")
    poll = payload.get("poll")
    info = payload.get("info")
    per_user_status_enabled = payload.get("per_user_status_enabled")
    assign_group_members = payload.get("assign_group_members")
    follow_project_members = payload.get("follow_project_members")
    description = payload.get("description")
    description_format = payload.get("description_format")
    locked = payload.get("locked")

    if _task_is_locked(task) and _task_payload_touches_locked_fields(payload):
        return _locked_task_response()

    if title is not None:
        task.title = title.strip()
    if locked is not None:
        task.locked = bool(locked)
    info_payload = dict(old_info_payload)
    if links is None and link is not None:
        stripped_link = link.strip()
        links = [stripped_link] if stripped_link else []
    if links is not None:
        info_payload["links"] = links
        task.link = (info_payload.get("links") or [None])[0]
    if status is not None:
        task.status = status.strip() or task.status
    if status_mode is not None:
        ok, error = _set_task_status_mode(task, status_mode)
        if not ok:
            return {"error": error}, 400
    elif per_user_status_enabled is not None:
        task.status_mode = "multi" if bool(per_user_status_enabled) else "single"
        task.per_user_status_enabled = bool(per_user_status_enabled)
    if assign_group_members is not None:
        next_group_mode = bool(assign_group_members)
        task.assign_group_members = next_group_mode
        if next_group_mode:
            sync_group_task_assignments(task)
    if user_status is not None:
        target_user_id = user.id
        if status_user_id is not None:
            try:
                target_user_id = int(status_user_id)
            except (TypeError, ValueError):
                return {"error": "invalid status_user_id"}, 400
            is_assigned_target = Assignment.query.filter_by(task_id=task.id, user_id=target_user_id).first() is not None
            if not is_assigned_target:
                return {"error": "status user must be an assigned account"}, 400
        personal_row = set_task_user_status(task.id, target_user_id, user_status)
        if _task_status_mode(task) == "single" and personal_row.status:
            task.status = personal_row.status
    if status_percentage is not None:
        ok, error = _set_task_status_percentage(task, status_percentage)
        if not ok:
            return {"error": error}, 400
    if poll is not None:
        normalized_poll = _normalize_task_poll_payload(poll)
        _set_task_poll(task, normalized_poll)
        if task_type is not None:
            _set_task_type(task, task_type)
    elif task_type is not None:
        _set_task_type(task, task_type)
    if info is not None:
        if isinstance(info, dict):
            info_payload["html"] = info.get("html")
            info_payload["attachments"] = info.get("attachments", info_payload.get("attachments", []))
        else:
            info_payload["html"] = info
        task.info = normalize_info_payload(info_payload, task.link)
    elif links is not None:
        task.info = normalize_info_payload(info_payload, task.link)
    if due_at is not None or due_mode is not None:
        ok, error = _apply_task_due_payload(task, due_at_raw=due_at, due_mode_raw=due_mode)
        if not ok:
            return {"error": error}, 400
    if start_date is not None or due_at is not None or due_mode is not None:
        ok, error = _set_task_start_date(task, start_date if _task_due_mode(task) == "date" else "")
        if not ok:
            return {"error": error}, 400
    if description is not None:
        task.description = description
    if description_format is not None:
        normalized = _coerce_description_format(description_format, DEFAULT_TASK_DESCRIPTION_FORMAT)
        if normalized is None:
            return {"error": "invalid description_format"}, 400
        task.description_format = normalized
    if follow_project_members is not None:
        _set_task_follow_project_members(task, bool(follow_project_members))
        if bool(follow_project_members):
            desired_user_ids = set(_project_access_user_ids(task.project_id))
            existing_rows = TaskFollower.query.filter_by(task_id=task.id).all()
            existing_by_user_id = {int(row.user_id): row for row in existing_rows if row.user_id}
            for row in existing_rows:
                if row.user_id and int(row.user_id) not in desired_user_ids:
                    db.session.delete(row)
            for follower_user_id in desired_user_ids:
                if follower_user_id not in existing_by_user_id:
                    db.session.add(TaskFollower(task_id=task.id, user_id=follower_user_id))
    if links is not None:
        try:
            cache_favicons_for_links(current_app._get_current_object(), info_payload.get("links", []))
        except Exception:
            pass
    db.session.commit()
    new_due_mode = _task_due_mode(task)
    new_start_date = _task_start_date(task)
    new_status_mode = _task_status_mode(task)
    changed_fields = _task_changed_field_labels(
        old_title=old_title,
        new_title=task.title,
        old_status=old_status,
        new_status=task.status,
        old_due_at=old_due_at,
        new_due_at=task.due_at,
        old_due_mode=old_due_mode,
        new_due_mode=new_due_mode,
        old_start_date=old_start_date,
        new_start_date=new_start_date,
        old_info_payload=old_info_payload,
        new_info_payload=_info_payload_for(task),
        old_per_user_status_enabled=old_per_user_status_enabled,
        new_per_user_status_enabled=bool(task.per_user_status_enabled),
        old_status_mode=old_status_mode,
        new_status_mode=new_status_mode,
        old_assign_group_members=old_assign_group_members,
        new_assign_group_members=bool(task.assign_group_members),
        old_follow_project_members=old_follow_project_members,
        new_follow_project_members=_task_follow_project_members(task),
        old_description=old_description,
        new_description=task.description,
        old_description_format=old_description_format,
        new_description_format=task.description_format or DEFAULT_TASK_DESCRIPTION_FORMAT,
        old_task_type=old_task_type,
        new_task_type=_task_type(task),
        old_poll=old_poll,
        new_poll=_task_poll(task),
    )
    if old_locked != bool(task.locked):
        changed_fields.append("lock")
    notification_kind = _task_update_notification_kind(
        old_title=old_title,
        new_title=task.title,
        old_status=old_status,
        new_status=task.status,
        old_due_at=old_due_at,
        new_due_at=task.due_at,
        old_due_mode=old_due_mode,
        new_due_mode=new_due_mode,
        user_status=user_status,
    )
    if changed_fields or notification_kind != "task_update":
        detail_payload = _task_notification_actor_payload(user)
        if notification_kind == "task_renamed":
            detail_payload.update({"old_title": old_title or "", "new_title": task.title or ""})
        elif notification_kind in {"task_due_changed", "task_due_cleared", "task_due_asap"}:
            detail_payload.update({
                "old_due_mode": old_due_mode or "none",
                "new_due_mode": new_due_mode or "none",
                "old_due_at": old_due_at.isoformat() if old_due_at else "",
                "new_due_at": task.due_at.isoformat() if task.due_at else "",
                "old_due_label": old_due_at.strftime("%Y-%m-%d") if old_due_at else ("ASAP" if old_due_mode == "asap" else ""),
                "new_due_label": task.due_at.strftime("%Y-%m-%d") if task.due_at else ("ASAP" if new_due_mode == "asap" else ""),
            })
        elif notification_kind in {"task_status_changed", "task_completed"}:
            detail_payload.update({
                "old_status": old_status or "",
                "new_status": user_status or task.status or "",
            })
        elif notification_kind == "task_update":
            detail_payload.update({
                "changed_fields": _task_update_notification_changed_fields(
                    changed_fields,
                    old_poll=old_poll,
                    new_poll=_task_poll(task),
                ),
            })
        queue_task_notifications(task, exclude_user_id=user.id, kind=notification_kind, detail_payload=detail_payload)
        history_task_body, history_scoped_body = _task_update_history_bodies(
            actor_name=display_name_for_user(user) or user.email or "Someone",
            task_title=task.title,
            changed_fields=changed_fields,
            new_title=task.title,
            new_due_mode=new_due_mode,
            new_due_at=task.due_at,
            new_start_date=new_start_date,
            new_status=task.status,
            new_status_mode=new_status_mode,
            new_status_percentage=_task_status_percentage(task),
            old_locked=old_locked,
            new_locked=bool(task.locked),
            old_poll=old_poll,
            new_poll=_task_poll(task),
            assignments=[_serialize_assignment_row(row) for row in Assignment.query.filter_by(task_id=task.id).all()],
            followers=[_serialize_follower_row(row) for row in TaskFollower.query.filter_by(task_id=task.id).all()],
        )
        log_task_history(task, actor=user, action="updated", changed_fields=changed_fields, task_body=history_task_body, scoped_body=history_scoped_body)
        db.session.commit()
    impacted_ids = _task_impacted_ids([task.id])
    impacted_tasks = _touch_tasks(impacted_ids)
    if impacted_tasks:
        emit_tasks_updated(impacted_tasks, actor_user_id=user.id)
    emit_task_notification_updates(task, exclude_user_id=user.id)
    info_payload = _info_payload_for(task)
    status_meta = task_status_meta(task, viewer_user_id=user.id)
    assignments = Assignment.query.filter_by(task_id=task.id).all()
    followers = TaskFollower.query.filter_by(task_id=task.id).all()
    return {
        "id": task.id,
        "title": task.title,
        "locked": bool(task.locked),
        "creator": _serialize_task_row(task, viewer_user_id=user.id).get("creator"),
        "status": task.status,
        "task_type": _task_type(task),
        "poll": _task_poll(task),
        "status_mode": _task_status_mode(task),
        "status_percentage": _task_status_percentage(task),
        "per_user_status_enabled": _task_status_mode(task) == "multi",
        "assign_group_members": bool(task.assign_group_members),
        "group_assignment_members": serialize_group_assignment_members(task) if task.assign_group_members else [],
        "follow_project_members": _task_follow_project_members(task),
        "project_follower_members": _serialize_project_follower_members(task) if _task_follow_project_members(task) else [],
        "status_meta": status_meta,
        "user_status": status_meta.get("my_status"),
        "info": info_payload,
        "link": task.link,
        "links": info_payload.get("links", []),
        "assignments": [_serialize_assignment_row(row) for row in assignments],
        "followers": [_serialize_follower_row(row) for row in followers],
        "prerequisites": _serialize_task_prerequisites(task.id),
        "dependents": _serialize_task_dependents(task.id),
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "due_mode": _task_due_mode(task),
        "start_date": _task_start_date(task),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "description": task.description,
        "description_format": task.description_format or DEFAULT_TASK_DESCRIPTION_FORMAT,
        "rendered_description": _render_description(task.description, task.description_format, DEFAULT_TASK_DESCRIPTION_FORMAT),
    }, 200


@api_bp.get("/tasks/<int:task_id>")
@login_required
def get_task(task_id: int):
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403
    assignments = Assignment.query.filter_by(task_id=task.id).all()
    followers = TaskFollower.query.filter_by(task_id=task.id).all()
    info_payload = _info_payload_for(task)
    return {
        "id": task.id,
        "project_id": task.project_id,
        "group_id": task.group_id,
        "group_name": (Group.query.get(task.group_id).name if task.group_id else None),
        "locked": bool(task.locked),
        "title": task.title,
        "creator": _serialize_task_row(task, viewer_user_id=user.id).get("creator"),
        "link": task.link,
        "links": _info_payload_for(task).get("links", []),
        "status": task.status,
        "task_type": _task_type(task),
        "poll": _task_poll(task),
        "status_mode": _task_status_mode(task),
        "status_percentage": _task_status_percentage(task),
        "per_user_status_enabled": _task_status_mode(task) == "multi",
        "assign_group_members": bool(task.assign_group_members),
        "group_assignment_members": serialize_group_assignment_members(task) if task.assign_group_members else [],
        "follow_project_members": _task_follow_project_members(task),
        "project_follower_members": _serialize_project_follower_members(task) if _task_follow_project_members(task) else [],
        "status_meta": task_status_meta(task, viewer_user_id=user.id),
        "info": info_payload,
        "description": task.description,
        "description_format": task.description_format or DEFAULT_TASK_DESCRIPTION_FORMAT,
        "rendered_description": _render_description(task.description, task.description_format, DEFAULT_TASK_DESCRIPTION_FORMAT),
        "assignments": [_serialize_assignment_row(row) for row in assignments],
        "followers": [_serialize_follower_row(row) for row in followers],
        "prerequisites": _serialize_task_prerequisites(task.id),
        "dependents": _serialize_task_dependents(task.id),
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "due_mode": _task_due_mode(task),
        "start_date": _task_start_date(task),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "rendered_description": _render_description(task.description, task.description_format, DEFAULT_TASK_DESCRIPTION_FORMAT),
    }, 200


@api_bp.get("/projects/<int:project_id>")
@login_required
def get_project(project_id: int):
    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if not _can_access_project(user, project.id):
        return {"error": "unauthorized"}, 403
    return {
        "id": project.id,
        "name": project.name,
        "links": _info_payload_for(project).get("links", []),
        "description": project.description,
        "description_format": project.description_format or DEFAULT_PROJECT_DESCRIPTION_FORMAT,
        "rendered_description": _render_description(project.description, project.description_format, DEFAULT_PROJECT_DESCRIPTION_FORMAT),
        "start_date": project.start_date.isoformat() if project.start_date else None,
        "end_date": project.end_date.isoformat() if project.end_date else None,
        "is_direct": bool(project.is_direct),
        "created_at": project.created_at.isoformat() if project.created_at else None,
    }, 200


@api_bp.get("/projects/<int:project_id>/tree_snapshot")
@login_required
def get_project_tree_snapshot(project_id: int):
    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if not _can_access_project(user, project.id):
        return {"error": "unauthorized"}, 403

    groups = Group.query.filter_by(project_id=project.id).order_by(Group.position.asc(), Group.id.asc()).all()
    tasks = (
        Task.query.filter_by(project_id=project.id)
        .order_by(Task.position.asc(), Task.id.asc())
        .all()
    )
    status_map = task_status_meta_map(tasks, viewer_user_id=user.id) if tasks else {}
    tasks_by_group: dict[int, list[dict]] = {group.id: [] for group in groups}
    ungrouped_tasks: list[dict] = []
    for task in tasks:
        row = _serialize_task_row(task, viewer_user_id=user.id)
        if task.group_id and task.group_id in tasks_by_group:
            tasks_by_group[task.group_id].append(row)
        else:
            ungrouped_tasks.append(row)

    return _json_response_with_payload_header({
        "project": _serialize_project_payload_for_user(project, user.id),
        "can_manage_project": bool(_can_manage_project(user, project.id)),
        "groups": [
            {
                "id": group.id,
                "project_id": group.project_id,
                "name": group.name,
                "position": group.position,
                "links": _info_payload_for(group).get("links", []),
                "description": group.description,
                "description_format": group.description_format or DEFAULT_GROUP_DESCRIPTION_FORMAT,
                "rendered_description": _render_description(group.description, group.description_format, DEFAULT_GROUP_DESCRIPTION_FORMAT),
                "tasks": tasks_by_group.get(group.id, []),
            }
            for group in groups
        ],
        "ungrouped_tasks": ungrouped_tasks,
    }, 200)


@api_bp.get("/groups/<int:group_id>")
@login_required
def get_group(group_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_access_project(user, group.project_id):
        return {"error": "unauthorized"}, 403
    return {
        "id": group.id,
        "project_id": group.project_id,
        "name": group.name,
        "links": _info_payload_for(group).get("links", []),
        "description": group.description,
        "description_format": group.description_format or DEFAULT_GROUP_DESCRIPTION_FORMAT,
        "rendered_description": _render_description(group.description, group.description_format, DEFAULT_GROUP_DESCRIPTION_FORMAT),
        "created_at": group.created_at.isoformat() if group.created_at else None,
    }, 200


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
    serialized_comments = [_serialize_task_comment(comment, users.get(comment.user_id), collaborators.get(comment.collaborator_id)) for comment in comments]
    history_entries = serialized_discussion_events_for_entity("task", task.id)
    timeline = sorted(
        serialized_comments + history_entries,
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")),
    )
    return {
        "comments": timeline,
        "comment_count": len(serialized_comments),
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
    links_changed = _merge_comment_links(task, body)
    rendered_preview = _render_mentions_for_display(body)
    mentioned_users = [mentioned_user for mentioned_user in _mentioned_users(body, exclude_user_id=user.id) if _can_access_task(mentioned_user, task)]
    mentioned_user_ids = {int(mentioned_user.id) for mentioned_user in mentioned_users}
    activity_rows = upsert_discussion_activity(
        user_ids=task_discussion_user_ids(task),
        entity_type="task",
        entity_id=task.id,
        task_id=task.id,
        project_id=task.project_id,
        group_id=task.group_id,
        comment_id=comment.id,
        actor_user_id=user.id,
        preview=rendered_preview,
        exclude_user_id=user.id,
    )

    queue_task_notifications(task, exclude_user_id=user.id, kind="comment", comment_id=comment.id)
    for mentioned_user_id in sorted(mentioned_user_ids):
        queue_user_notification(user_id=mentioned_user_id, kind="comment", task_id=task.id, comment_id=comment.id)

    db.session.commit()
    comment_count = TaskComment.query.filter_by(task_id=task.id).count()
    if links_changed:
        emit_task_updated(task, actor_user_id=user.id)
    emit_task_comment_created(task.id, _serialize_task_comment(comment, user))
    _emit_comment_notification_previews(
        recipient_ids=task_discussion_user_ids(task),
        actor_user=user,
        task_id=task.id,
        project_id=task.project_id,
        group_id=task.group_id,
        task_title=task.title or "Task",
        project_name=task.project.name if getattr(task, "project", None) and task.project.name else "",
        preview=rendered_preview,
        exclude_user_id=user.id,
    )
    extra_mentioned_user_ids = mentioned_user_ids.difference(task_discussion_user_ids(task))
    if extra_mentioned_user_ids:
        _emit_comment_notification_previews(
            recipient_ids=extra_mentioned_user_ids,
            actor_user=user,
            task_id=task.id,
            project_id=task.project_id,
            group_id=task.group_id,
            task_title=task.title or "Task",
            project_name=task.project.name if getattr(task, "project", None) and task.project.name else "",
            preview=rendered_preview,
            exclude_user_id=user.id,
        )
    for mentioned_user_id in sorted(mentioned_user_ids):
        emit_notification_state(mentioned_user_id)
    for row in activity_rows:
        emit_discussion_activity_updated(row.user_id, row.id)
    emit_task_notification_updates(task, exclude_user_id=user.id)
    return {"comment": _serialize_task_comment(comment, user), "comment_count": comment_count}, 201


@api_bp.patch("/tasks/<int:task_id>/comments/<int:comment_id>")
@login_required
def update_task_comment(task_id: int, comment_id: int):
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

    comment = TaskComment.query.filter_by(id=comment_id, task_id=task.id, user_id=user.id).first()
    if not comment:
        return {"error": "comment not found"}, 404

    comment.body = body
    db.session.commit()
    payload = _serialize_task_comment(comment, user)
    emit_task_comment_updated(task.id, payload)
    return {"comment": payload}, 200


@api_bp.delete("/tasks/<int:task_id>/comments/<int:comment_id>")
@login_required
def delete_task_comment(task_id: int, comment_id: int):
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    comment = TaskComment.query.filter_by(id=comment_id, task_id=task.id, user_id=user.id).first()
    if not comment:
        return {"error": "comment not found"}, 404

    db.session.delete(comment)
    db.session.commit()
    emit_task_comment_deleted(task.id, comment_id)
    return {"status": "ok", "comment_id": comment_id}, 200


@api_bp.delete("/tasks/<int:task_id>/history/<int:event_id>")
@login_required
def delete_task_history(task_id: int, event_id: int):
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403
    event = DiscussionEvent.query.filter_by(id=event_id, entity_type="task", entity_id=task.id).first()
    if not event:
        return {"error": "history not found"}, 404
    db.session.delete(event)
    db.session.commit()
    return {"status": "ok", "history_id": event_id}, 200


@api_bp.get("/projects/<int:project_id>/comments")
@login_required
def list_project_comments(project_id: int):
    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if not _can_access_project(user, project.id):
        return {"error": "unauthorized"}, 403

    comments = ProjectComment.query.filter_by(project_id=project.id).order_by(ProjectComment.created_at.asc(), ProjectComment.id.asc()).all()
    user_ids = {comment.user_id for comment in comments}
    users = {row.id: row for row in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
    serialized_comments = [_serialize_simple_comment(comment, users.get(comment.user_id), project_id=project.id) for comment in comments]
    history_entries = serialized_discussion_events_for_entity("project", project.id)
    timeline = sorted(
        serialized_comments + history_entries,
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")),
    )
    return {
        "comments": timeline,
        "comment_count": len(serialized_comments),
    }


@api_bp.post("/projects/<int:project_id>/comments")
@login_required
def create_project_comment(project_id: int):
    payload = request.get_json(silent=True) or {}
    body = (payload.get("body") or "").strip()
    if not body:
        return {"error": "body is required"}, 400

    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if not _can_access_project(user, project.id):
        return {"error": "unauthorized"}, 403

    comment = ProjectComment(project_id=project.id, user_id=user.id, body=body)
    db.session.add(comment)
    _merge_comment_links(project, body)
    db.session.flush()
    rendered_preview = _render_mentions_for_display(body)
    mentioned_user_ids = {
        int(mentioned_user.id)
        for mentioned_user in _mentioned_users(body, exclude_user_id=user.id)
        if _can_access_project(mentioned_user, project.id)
    }
    activity_rows = upsert_discussion_activity(
        user_ids=project_discussion_user_ids(project.id),
        entity_type="project",
        entity_id=project.id,
        project_id=project.id,
        comment_id=comment.id,
        actor_user_id=user.id,
        preview=rendered_preview,
        exclude_user_id=user.id,
    )
    db.session.commit()
    comment_count = ProjectComment.query.filter_by(project_id=project.id).count()
    payload = _serialize_simple_comment(comment, user, project_id=project.id)
    emit_project_updated(project, actor_user_id=user.id)
    emit_project_comment_created(project.id, payload)
    _emit_comment_notification_previews(
        recipient_ids=project_discussion_user_ids(project.id),
        actor_user=user,
        project_id=project.id,
        task_title="Project chat",
        project_name=project.name or "Project",
        preview=rendered_preview,
        exclude_user_id=user.id,
    )
    extra_mentioned_user_ids = mentioned_user_ids.difference(project_discussion_user_ids(project.id))
    if extra_mentioned_user_ids:
        _emit_comment_notification_previews(
            recipient_ids=extra_mentioned_user_ids,
            actor_user=user,
            project_id=project.id,
            task_title="Project chat",
            project_name=project.name or "Project",
            preview=rendered_preview,
            exclude_user_id=user.id,
        )
    for row in activity_rows:
        emit_discussion_activity_updated(row.user_id, row.id)
    return {"comment": payload, "comment_count": comment_count}, 201


@api_bp.patch("/projects/<int:project_id>/comments/<int:comment_id>")
@login_required
def update_project_comment(project_id: int, comment_id: int):
    payload = request.get_json(silent=True) or {}
    body = (payload.get("body") or "").strip()
    if not body:
        return {"error": "body is required"}, 400

    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if not _can_access_project(user, project.id):
        return {"error": "unauthorized"}, 403

    comment = ProjectComment.query.filter_by(id=comment_id, project_id=project.id, user_id=user.id).first()
    if not comment:
        return {"error": "comment not found"}, 404

    comment.body = body
    db.session.commit()
    payload = _serialize_simple_comment(comment, user, project_id=project.id)
    emit_project_comment_updated(project.id, payload)
    return {"comment": payload}, 200


@api_bp.delete("/projects/<int:project_id>/comments/<int:comment_id>")
@login_required
def delete_project_comment(project_id: int, comment_id: int):
    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if not _can_access_project(user, project.id):
        return {"error": "unauthorized"}, 403

    comment = ProjectComment.query.filter_by(id=comment_id, project_id=project.id, user_id=user.id).first()
    if not comment:
        return {"error": "comment not found"}, 404

    db.session.delete(comment)
    db.session.commit()
    emit_project_comment_deleted(project.id, comment_id)
    return {"status": "ok", "comment_id": comment_id}, 200


@api_bp.delete("/projects/<int:project_id>/history/<int:event_id>")
@login_required
def delete_project_history(project_id: int, event_id: int):
    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if not _can_access_project(user, project.id):
        return {"error": "unauthorized"}, 403
    event = DiscussionEvent.query.filter_by(id=event_id, entity_type="project", entity_id=project.id).first()
    if not event:
        return {"error": "history not found"}, 404
    db.session.delete(event)
    db.session.commit()
    return {"status": "ok", "history_id": event_id}, 200


@api_bp.get("/groups/<int:group_id>/comments")
@login_required
def list_group_comments(group_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_access_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    comments = GroupComment.query.filter_by(group_id=group.id).order_by(GroupComment.created_at.asc(), GroupComment.id.asc()).all()
    user_ids = {comment.user_id for comment in comments}
    users = {row.id: row for row in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
    serialized_comments = [_serialize_simple_comment(comment, users.get(comment.user_id), group_id=group.id) for comment in comments]
    history_entries = serialized_discussion_events_for_entity("group", group.id)
    timeline = sorted(
        serialized_comments + history_entries,
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")),
    )
    return {
        "comments": timeline,
        "comment_count": len(serialized_comments),
    }


@api_bp.post("/groups/<int:group_id>/comments")
@login_required
def create_group_comment(group_id: int):
    payload = request.get_json(silent=True) or {}
    body = (payload.get("body") or "").strip()
    if not body:
        return {"error": "body is required"}, 400

    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_access_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    comment = GroupComment(group_id=group.id, user_id=user.id, body=body)
    db.session.add(comment)
    _merge_comment_links(group, body)
    db.session.flush()
    rendered_preview = _render_mentions_for_display(body)
    mentioned_user_ids = {
        int(mentioned_user.id)
        for mentioned_user in _mentioned_users(body, exclude_user_id=user.id)
        if _can_access_project(mentioned_user, group.project_id)
    }
    activity_rows = upsert_discussion_activity(
        user_ids=group_discussion_user_ids(group.id),
        entity_type="group",
        entity_id=group.id,
        project_id=group.project_id,
        group_id=group.id,
        comment_id=comment.id,
        actor_user_id=user.id,
        preview=rendered_preview,
        exclude_user_id=user.id,
    )
    db.session.commit()
    comment_count = GroupComment.query.filter_by(group_id=group.id).count()
    payload = _serialize_simple_comment(comment, user, group_id=group.id)
    emit_group_updated(group, actor_user_id=user.id)
    emit_group_comment_created(group.id, payload)
    _emit_comment_notification_previews(
        recipient_ids=group_discussion_user_ids(group.id),
        actor_user=user,
        project_id=group.project_id,
        group_id=group.id,
        task_title=(group.name or "Group") + " chat",
        project_name=group.project.name if getattr(group, "project", None) and group.project.name else "",
        preview=rendered_preview,
        exclude_user_id=user.id,
    )
    extra_mentioned_user_ids = mentioned_user_ids.difference(group_discussion_user_ids(group.id))
    if extra_mentioned_user_ids:
        _emit_comment_notification_previews(
            recipient_ids=extra_mentioned_user_ids,
            actor_user=user,
            project_id=group.project_id,
            group_id=group.id,
            task_title=(group.name or "Group") + " chat",
            project_name=group.project.name if getattr(group, "project", None) and group.project.name else "",
            preview=rendered_preview,
            exclude_user_id=user.id,
        )
    for row in activity_rows:
        emit_discussion_activity_updated(row.user_id, row.id)
    return {"comment": payload, "comment_count": comment_count}, 201


@api_bp.patch("/groups/<int:group_id>/comments/<int:comment_id>")
@login_required
def update_group_comment(group_id: int, comment_id: int):
    payload = request.get_json(silent=True) or {}
    body = (payload.get("body") or "").strip()
    if not body:
        return {"error": "body is required"}, 400

    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_access_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    comment = GroupComment.query.filter_by(id=comment_id, group_id=group.id, user_id=user.id).first()
    if not comment:
        return {"error": "comment not found"}, 404

    comment.body = body
    db.session.commit()
    payload = _serialize_simple_comment(comment, user, group_id=group.id)
    emit_group_comment_updated(group.id, payload)
    return {"comment": payload}, 200


@api_bp.delete("/groups/<int:group_id>/comments/<int:comment_id>")
@login_required
def delete_group_comment(group_id: int, comment_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_access_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    comment = GroupComment.query.filter_by(id=comment_id, group_id=group.id, user_id=user.id).first()
    if not comment:
        return {"error": "comment not found"}, 404

    db.session.delete(comment)
    db.session.commit()
    emit_group_comment_deleted(group.id, comment_id)
    return {"status": "ok", "comment_id": comment_id}, 200


@api_bp.delete("/groups/<int:group_id>/history/<int:event_id>")
@login_required
def delete_group_history(group_id: int, event_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_access_project(user, group.project_id):
        return {"error": "unauthorized"}, 403
    event = DiscussionEvent.query.filter_by(id=event_id, entity_type="group", entity_id=group.id).first()
    if not event:
        return {"error": "history not found"}, 404
    db.session.delete(event)
    db.session.commit()
    return {"status": "ok", "history_id": event_id}, 200


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
    TaskNotification.query.filter_by(user_id=user.id, task_id=task.id, kind="comment").filter(TaskNotification.read_at.is_(None)).update(
        {"read_at": now},
        synchronize_session=False,
    )
    db.session.commit()
    payload = notification_payload_for_user(user.id)
    emit_notification_state(user.id)
    payload["status"] = "ok"
    return payload


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

    existing = TaskNotification.query.filter_by(user_id=user.id, task_id=task.id, kind="comment").filter(TaskNotification.read_at.is_(None)).first()
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

    payload = notification_payload_for_user(user.id)
    emit_notification_state(user.id)
    payload["status"] = "ok"
    return payload


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
    target_project = Project.query.get(target_project_id)
    if not target_project:
        return {"error": "project not found"}, 404

    target_group_id = None if target_group_raw in (None, "", "null") else int(target_group_raw)
    if target_group_id is not None:
        target_group = Group.query.filter_by(id=target_group_id, project_id=target_project.id).first()
        if not target_group:
            return {"error": "group not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403
    if not _can_access_project(user, target_project.id):
        return {"error": "unauthorized"}, 403
    if target_group_id is not None and not _can_access_group(user, target_group_id):
        return {"error": "unauthorized"}, 403

    before_task = None
    if before_task_id:
        before_task = Task.query.get(before_task_id)
        if not before_task:
            return {"error": "before_task not found"}, 404
        if before_task.id == task.id:
            before_task = None
        elif before_task.project_id != target_project.id or before_task.group_id != target_group_id:
            return {"error": "before_task bucket mismatch"}, 400
        elif not _can_access_task(user, before_task):
            return {"error": "unauthorized"}, 403

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
    log_task_history(
        task,
        actor=user,
        action="moved",
        old_project_id=source_project_id,
        old_group_id=source_group_id,
    )
    queue_task_notifications(
        task,
        exclude_user_id=user.id,
        kind="task_moved",
        detail_payload={
            **_task_notification_actor_payload(user),
            "old_project_id": source_project_id,
            "old_group_id": source_group_id,
            "new_project_id": task.project_id,
            "new_group_id": task.group_id,
        },
    )
    db.session.commit()
    emit_task_updated(task, action="moved", old_project_id=source_project_id, old_group_id=source_group_id, actor_user_id=user.id)
    impacted_ids = [task_id for task_id in _task_impacted_ids([task.id]) if int(task_id) != int(task.id)]
    if impacted_ids:
        impacted_tasks = Task.query.filter(Task.id.in_(impacted_ids)).all()
        if impacted_tasks:
            emit_tasks_updated(impacted_tasks, actor_user_id=user.id)
    emit_task_notification_updates(task, exclude_user_id=user.id)
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
    link = payload.get("link")
    links = payload.get("links")
    description = payload.get("description")
    description_format = payload.get("description_format")
    if name is None and links is None and link is None and description is None and description_format is None:
        return {"error": "name or links is required"}, 400

    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404

    if not _can_manage_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    old_name = group.name
    old_links = list(_info_payload_for(group).get("links", []))
    old_description = group.description
    old_description_format = group.description_format or DEFAULT_GROUP_DESCRIPTION_FORMAT
    if name is not None:
        group.name = name.strip() or group.name
    if description is not None:
        group.description = description
    if description_format is not None:
        normalized = _coerce_description_format(description_format, DEFAULT_GROUP_DESCRIPTION_FORMAT)
        if normalized is None:
            return {"error": "invalid description_format"}, 400
        group.description_format = normalized
    info_payload = _info_payload_for(group)
    if links is None and link is not None:
        stripped_link = link.strip()
        links = [stripped_link] if stripped_link else []
    if links is not None:
        info_payload["links"] = links
        group.link = (info_payload.get("links") or [None])[0]
        group.info = normalize_info_payload(info_payload, group.link)
    changed_fields = _group_changed_field_labels(
        old_name=old_name,
        new_name=group.name,
        old_links=old_links,
        new_links=list(_info_payload_for(group).get("links", [])) if links is None else list(links or []),
        old_description=old_description,
        new_description=group.description,
        old_description_format=old_description_format,
        new_description_format=group.description_format or DEFAULT_GROUP_DESCRIPTION_FORMAT,
    )
    log_group_history(group, actor=user, action="updated", changed_fields=changed_fields)
    if links is not None:
        try:
            cache_favicons_for_links(current_app._get_current_object(), _info_payload_for(group).get("links", []))
        except Exception:
            pass
    db.session.commit()
    emit_group_updated(group, actor_user_id=user.id)
    return {
        "id": group.id,
        "name": group.name,
        "links": _info_payload_for(group).get("links", []),
        "description": group.description,
        "description_format": group.description_format or DEFAULT_GROUP_DESCRIPTION_FORMAT,
        "rendered_description": _render_description(group.description, group.description_format, DEFAULT_GROUP_DESCRIPTION_FORMAT),
    }, 200


@api_bp.patch("/projects/<int:project_id>")
@login_required
def update_project(project_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()
    name = payload.get("name")
    division_id = payload.get("division_id", "__missing__")
    link = payload.get("link")
    links = payload.get("links")
    description = payload.get("description")
    description_format = payload.get("description_format")
    start_date_raw = payload.get("start_date", "__missing__")
    end_date_raw = payload.get("end_date", "__missing__")
    if (
        name is None
        and division_id == "__missing__"
        and links is None
        and link is None
        and description is None
        and description_format is None
        and start_date_raw == "__missing__"
        and end_date_raw == "__missing__"
    ):
        return {"error": "name, links, division_id, or dates are required"}, 400
    if name is not None and not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    if (links is not None or link is not None) and not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    if (description is not None or description_format is not None) and not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    if (start_date_raw != "__missing__" or end_date_raw != "__missing__") and not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    if division_id != "__missing__" and not _can_access_project(user, project_id):
        return {"error": "unauthorized"}, 403
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    old_name = project.name
    old_links = list(_info_payload_for(project).get("links", []))
    old_description = project.description
    old_description_format = project.description_format or DEFAULT_PROJECT_DESCRIPTION_FORMAT
    old_start_date = project.start_date
    old_end_date = project.end_date
    current_pref_before = ProjectSidebarPreference.query.filter_by(user_id=user.id, project_id=project.id).first()
    old_division_id = current_pref_before.division_id if current_pref_before else None
    if name is not None:
        project.name = name.strip() or project.name
    if links is None and link is not None:
        stripped_link = link.strip()
        links = [stripped_link] if stripped_link else []
    if links is not None:
        info_payload = _info_payload_for(project)
        info_payload["links"] = links
        project.link = (info_payload.get("links") or [None])[0]
        project.info = normalize_info_payload(info_payload, project.link)
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
    if description is not None:
        project.description = description
    if description_format is not None:
        normalized = _coerce_description_format(description_format, DEFAULT_PROJECT_DESCRIPTION_FORMAT)
        if normalized is None:
            return {"error": "invalid description_format"}, 400
        project.description_format = normalized
    if start_date_raw != "__missing__":
        if start_date_raw in (None, ""):
            project.start_date = None
        else:
            try:
                project.start_date = date.fromisoformat(str(start_date_raw))
            except ValueError:
                return {"error": "invalid start_date"}, 400
    if end_date_raw != "__missing__":
        if end_date_raw in (None, ""):
            project.end_date = None
        else:
            try:
                project.end_date = date.fromisoformat(str(end_date_raw))
            except ValueError:
                return {"error": "invalid end_date"}, 400
    if project.start_date and project.end_date and project.end_date < project.start_date:
        return {"error": "end_date must be on or after start_date"}, 400
    if links is not None:
        try:
            cache_favicons_for_links(current_app._get_current_object(), _info_payload_for(project).get("links", []))
        except Exception:
            pass
    current_pref = ProjectSidebarPreference.query.filter_by(user_id=user.id, project_id=project.id).first()
    changed_fields = _project_changed_field_labels(
        old_name=old_name,
        new_name=project.name,
        old_links=old_links,
        new_links=list(_info_payload_for(project).get("links", [])),
        old_description=old_description,
        new_description=project.description,
        old_description_format=old_description_format,
        new_description_format=project.description_format or DEFAULT_PROJECT_DESCRIPTION_FORMAT,
        old_start_date=old_start_date,
        new_start_date=project.start_date,
        old_end_date=old_end_date,
        new_end_date=project.end_date,
        old_division_id=old_division_id,
        new_division_id=current_pref.division_id if current_pref else None,
    )
    log_project_history(project, actor=user, action="updated", changed_fields=changed_fields)
    db.session.commit()
    emit_project_updated(project, actor_user_id=user.id)
    if division_id != "__missing__":
        emit_sidebar_reordered(user.id, _sidebar_order_tokens(user), actor_user_id=user.id)
    return {
        "id": project.id,
        "name": project.name,
        "division_id": current_pref.division_id if current_pref else None,
        "links": _info_payload_for(project).get("links", []),
        "start_date": project.start_date.isoformat() if project.start_date else None,
        "end_date": project.end_date.isoformat() if project.end_date else None,
        "description": project.description,
        "description_format": project.description_format or DEFAULT_PROJECT_DESCRIPTION_FORMAT,
        "rendered_description": _render_description(project.description, project.description_format, DEFAULT_PROJECT_DESCRIPTION_FORMAT),
    }, 200


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
    emit_sidebar_reordered(user.id, _sidebar_order_tokens(user), actor_user_id=user.id)
    return {"status": "ok", "id": project.id, "division_id": moving_pref.division_id, "position": moving_pref.position}, 200


@api_bp.post("/divisions/reorder")
@login_required
def reorder_divisions():
    user = current_user()
    payload = request.get_json(silent=True) or {}
    order = payload.get("order") or []
    if not isinstance(order, list) or not order:
        return {"error": "order is required"}, 400

    divisions = Division.query.filter_by(owner_id=user.id).order_by(Division.position.asc(), Division.id.asc()).all()
    expected_ids = [division.id for division in divisions]
    try:
        order_ids = [int(item) for item in order]
    except (TypeError, ValueError):
        return {"error": "invalid order"}, 400
    if set(order_ids) != set(expected_ids):
        return {"error": "order mismatch"}, 400

    for index, division_id in enumerate(order_ids, start=1):
        Division.query.filter_by(id=division_id, owner_id=user.id).update({"position": index})

    db.session.commit()
    emit_sidebar_reordered(user.id, _sidebar_order_tokens(user), actor_user_id=user.id)
    return {"status": "ok"}, 200


@api_bp.post("/divisions/<int:division_id>/shift")
@login_required
def shift_division(division_id: int):
    user = current_user()
    payload = request.get_json(silent=True) or {}
    direction = (payload.get("direction") or "").strip().lower()
    if direction not in {"up", "down"}:
        return {"error": "invalid direction"}, 400

    divisions = Division.query.filter_by(owner_id=user.id).order_by(Division.position.asc(), Division.id.asc()).all()
    division_ids = [row.id for row in divisions]
    if division_id not in division_ids:
        return {"error": "division not found"}, 404

    index = division_ids.index(division_id)
    if direction == "up":
        if index == 0:
            return {"status": "ok", "moved": False}, 200
        swap_index = index - 1
    else:
        if index >= len(divisions) - 1:
            return {"status": "ok", "moved": False}, 200
        swap_index = index + 1

    divisions[index], divisions[swap_index] = divisions[swap_index], divisions[index]
    for position, division in enumerate(divisions, start=1):
        division.position = position

    db.session.commit()
    emit_sidebar_reordered(user.id, _sidebar_order_tokens(user), actor_user_id=user.id)
    return {
        "status": "ok",
        "moved": True,
        "order": [row.id for row in divisions],
    }, 200


@api_bp.patch("/divisions/<int:division_id>")
@login_required
def update_division(division_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()
    name = payload.get("name")
    color = payload.get("color")
    color_slot = payload.get("color_slot", "__missing__")
    if name is None and color is None and color_slot == "__missing__":
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
        if color_value:
            division.color_slot = None
    if color_slot != "__missing__":
        if color_slot in (None, "", 0, "0"):
            division.color_slot = None
        else:
            try:
                slot_value = int(color_slot)
            except (TypeError, ValueError):
                return {"error": "invalid color_slot"}, 400
            if slot_value < 1 or slot_value > 10:
                return {"error": "invalid color_slot"}, 400
            division.color_slot = slot_value
            division.color = None
    if division.color is None and division.color_slot is None:
        theme_name = normalize_theme_name(getattr(user, "theme_name", None) or DEFAULT_THEME_NAME)
        division.color_slot = color_slot_from_palette(theme_name, division.color) or 1
    db.session.commit()
    emit_division_updated(division, actor_user_id=user.id, recipient_user_id=user.id)
    return {
        "id": division.id,
        "name": division.name,
        "color": division_effective_color(division, normalize_theme_name(getattr(user, "theme_name", None))),
        "color_slot": division.color_slot,
        "custom_color": division.color,
    }, 200


@api_bp.delete("/projects/<int:project_id>")
@login_required
def delete_project(project_id: int):
    user = current_user()
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if project.owner_id != user.id:
        return {"error": "unauthorized"}, 403

    recipient_user_ids = [project.owner_id] + [row.user_id for row in ProjectMember.query.filter_by(project_id=project.id).all()]
    recipient_user_ids = sorted({user_id for user_id in recipient_user_ids if user_id})
    group_ids = [g.id for g in Group.query.filter_by(project_id=project.id).all()]
    task_ids = [t.id for t in Task.query.filter_by(project_id=project.id).all()]
    if task_ids:
        TaskPrerequisite.query.filter(
            or_(
                TaskPrerequisite.task_id.in_(task_ids),
                TaskPrerequisite.prerequisite_task_id.in_(task_ids),
            )
        ).delete(synchronize_session=False)
    Assignment.query.filter(Assignment.task_id.in_(task_ids)).delete(synchronize_session=False)
    Invite.query.filter(Invite.task_id.in_(task_ids)).delete(synchronize_session=False)
    Task.query.filter(Task.project_id == project.id).delete(synchronize_session=False)
    GroupMember.query.filter(GroupMember.group_id.in_(group_ids)).delete(synchronize_session=False)
    ProjectMember.query.filter(ProjectMember.project_id == project.id).delete(synchronize_session=False)
    ProjectSidebarPreference.query.filter_by(project_id=project.id).delete(synchronize_session=False)
    Group.query.filter(Group.project_id == project.id).delete(synchronize_session=False)
    db.session.delete(project)
    db.session.commit()
    emit_project_deleted(project.id, actor_user_id=user.id, task_ids=task_ids, recipient_user_ids=recipient_user_ids)
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
    emit_division_deleted(division.id, actor_user_id=user.id, recipient_user_id=user.id)
    emit_sidebar_reordered(user.id, _sidebar_order_tokens(user), actor_user_id=user.id)
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
            creator_user_id=user.id,
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

    if task_map:
        source_prerequisites = (
            TaskPrerequisite.query.filter(TaskPrerequisite.task_id.in_(list(task_map.keys())))
            .order_by(TaskPrerequisite.created_at.asc(), TaskPrerequisite.id.asc())
            .all()
        )
        for prerequisite in source_prerequisites:
            new_task_id = task_map.get(prerequisite.task_id)
            new_prerequisite_task_id = task_map.get(prerequisite.prerequisite_task_id)
            if not new_task_id or not new_prerequisite_task_id:
                continue
            db.session.add(
                TaskPrerequisite(
                    task_id=new_task_id,
                    prerequisite_task_id=new_prerequisite_task_id,
                )
            )

    db.session.commit()
    copy_pref = ProjectSidebarPreference.query.filter_by(user_id=user.id, project_id=copy.id).first()
    log_project_history(copy, actor=user, action="created")
    db.session.commit()
    emit_project_created(copy, actor_user_id=user.id, recipient_user_id=user.id)
    emit_sidebar_reordered(user.id, _sidebar_order_tokens(user), actor_user_id=user.id)
    return {
        "id": copy.id,
        "name": copy.name,
        "division_id": copy_pref.division_id if copy_pref else None,
    }, 201


@api_bp.delete("/groups/<int:group_id>")
@login_required
def delete_group(group_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if group.project_id is None or not _can_manage_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    project_id = group.project_id
    task_ids = [t.id for t in Task.query.filter_by(group_id=group.id).all()]
    if task_ids:
        TaskPrerequisite.query.filter(
            or_(
                TaskPrerequisite.task_id.in_(task_ids),
                TaskPrerequisite.prerequisite_task_id.in_(task_ids),
            )
        ).delete(synchronize_session=False)
    Assignment.query.filter(Assignment.task_id.in_(task_ids)).delete(synchronize_session=False)
    Invite.query.filter(Invite.task_id.in_(task_ids)).delete(synchronize_session=False)
    Task.query.filter(Task.group_id == group.id).delete(synchronize_session=False)
    GroupMember.query.filter(GroupMember.group_id == group.id).delete(synchronize_session=False)
    log_group_history(group, actor=user, action="deleted", title=group.name)
    db.session.delete(group)
    db.session.commit()
    emit_group_deleted(group_id, project_id, actor_user_id=user.id, task_ids=task_ids)
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
            creator_user_id=user.id,
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

    if task_map:
        source_prerequisites = (
            TaskPrerequisite.query.filter(TaskPrerequisite.task_id.in_(list(task_map.keys())))
            .order_by(TaskPrerequisite.created_at.asc(), TaskPrerequisite.id.asc())
            .all()
        )
        for prerequisite in source_prerequisites:
            new_task_id = task_map.get(prerequisite.task_id)
            new_prerequisite_task_id = task_map.get(prerequisite.prerequisite_task_id)
            if not new_task_id or not new_prerequisite_task_id:
                continue
            db.session.add(
                TaskPrerequisite(
                    task_id=new_task_id,
                    prerequisite_task_id=new_prerequisite_task_id,
                )
            )

    db.session.commit()
    tasks = Task.query.filter_by(group_id=new_group.id).order_by(Task.position.asc(), Task.id.asc()).all()
    log_group_history(new_group, actor=user, action="created")
    db.session.commit()
    emit_group_created(new_group, actor_user_id=user.id)
    return {
        "id": new_group.id,
        "name": new_group.name,
        "project_id": new_group.project_id,
        "color": new_group.color,
        "tasks": [_serialize_task_row(task) for task in tasks],
    }, 201


@api_bp.post("/groups/<int:group_id>/promote")
@login_required
def promote_group_to_project(group_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    source_project = Project.query.get(group.project_id)
    if not source_project:
        return {"error": "project not found"}, 404
    if not _can_manage_project(user, source_project.id):
        return {"error": "unauthorized"}, 403

    new_project = Project(
        name=group.name,
        owner_id=source_project.owner_id,
        division_id=None,
        position=0,
        default_owner_calendar_opt_in=source_project.default_owner_calendar_opt_in,
        default_invitee_calendar_opt_in=source_project.default_invitee_calendar_opt_in,
    )
    db.session.add(new_project)
    db.session.flush()

    member_rows = ProjectMember.query.filter_by(project_id=source_project.id).all()
    member_user_ids = {row.user_id for row in member_rows if row.user_id and int(row.user_id) != int(new_project.owner_id)}
    for member_user_id in sorted(member_user_ids):
        db.session.add(ProjectMember(project_id=new_project.id, user_id=member_user_id))

    pref_user_ids = {source_project.owner_id}
    pref_user_ids.update(member_user_ids)
    for pref_user_id in list(pref_user_ids):
        source_pref = ProjectSidebarPreference.query.filter_by(user_id=pref_user_id, project_id=source_project.id).first()
        if not source_pref:
            continue
        pref_position = insert_project_position(pref_user_id, source_pref.division_id, (source_pref.position or 0) + 1)
        db.session.add(
            ProjectSidebarPreference(
                user_id=pref_user_id,
                project_id=new_project.id,
                division_id=source_pref.division_id,
                position=pref_position,
            )
        )

    moved_tasks = Task.query.filter_by(group_id=group.id).order_by(Task.position.asc(), Task.id.asc()).all()
    old_group_id = group.id
    source_project_id = source_project.id
    for index, task in enumerate(moved_tasks):
        task.project_id = new_project.id
        task.group_id = None
        task.position = index

    db.session.delete(group)
    db.session.commit()

    log_project_history(new_project, actor=user, action="created")
    for task in moved_tasks:
        log_task_history(task, actor=user, action="moved", old_project_id=source_project_id, old_group_id=old_group_id)
    db.session.commit()
    emit_project_created(new_project, actor_user_id=user.id)
    emit_group_deleted(old_group_id, source_project_id, actor_user_id=user.id, task_ids=[])
    for task in moved_tasks:
        emit_task_updated(task, action="moved", old_project_id=source_project_id, old_group_id=old_group_id, actor_user_id=user.id)
    for pref_user_id in pref_user_ids:
        emit_sidebar_reordered(pref_user_id, _sidebar_order_tokens(User.query.get(pref_user_id)), actor_user_id=user.id)

    new_pref = ProjectSidebarPreference.query.filter_by(user_id=user.id, project_id=new_project.id).first()
    return {
        "project": {
            "id": new_project.id,
            "name": new_project.name,
            "division_id": new_pref.division_id if new_pref else None,
        },
        "moved_task_ids": [task.id for task in moved_tasks],
        "old_group_id": old_group_id,
    }, 201


@api_bp.get("/users")
@login_required
def list_users():
    user = current_user()
    q = (request.args.get("q") or "").strip().lower()
    entity_type = (request.args.get("entity_type") or "").strip().lower()
    entity_id_raw = (request.args.get("entity_id") or "").strip()
    entity_id = None
    if entity_id_raw:
        try:
            entity_id = int(entity_id_raw)
        except ValueError:
            entity_id = None
    limit = 100
    if q:
        users = search_users_by_identity(q, exclude_user_id=user.id, limit=limit)
    else:
        users = (
            User.query.filter(User.id != user.id)
            .order_by(User.display_name.asc(), User.email.asc())
            .limit(limit)
            .all()
        )
    if entity_type in {"task", "project", "group"} and entity_id:
        allowed_user_ids: set[int] = set()
        if entity_type == "task":
            task = Task.query.get(entity_id)
            if not task or not _can_access_task(user, task):
                return {"error": "unauthorized"}, 403
            allowed_user_ids = task_discussion_user_ids(task)
        elif entity_type == "project":
            project = Project.query.get(entity_id)
            if not project or not _can_access_project(user, project.id):
                return {"error": "unauthorized"}, 403
            allowed_user_ids = project_discussion_user_ids(project.id)
        else:
            group = Group.query.get(entity_id)
            if not group or not _can_access_project(user, group.project_id):
                return {"error": "unauthorized"}, 403
            allowed_user_ids = group_discussion_user_ids(group.id)
        users = [u for u in users if int(u.id) in allowed_user_ids]
    active_theme_name = normalize_theme_name(getattr(user, "theme_name", None) or DEFAULT_THEME_NAME)
    user_ids = [u.id for u in users]
    email_rows = (
        UserEmail.query.filter(UserEmail.user_id.in_(user_ids)).order_by(UserEmail.is_primary.desc(), UserEmail.email.asc()).all()
        if user_ids else []
    )
    emails_by_user: dict[int, list[str]] = {}
    for row in email_rows:
        emails_by_user.setdefault(int(row.user_id), [])
        if row.email and row.email not in emails_by_user[int(row.user_id)]:
            emails_by_user[int(row.user_id)].append(row.email)
    results = []
    for u in users:
        results.append(
            {
                "id": u.id,
                "email": u.email,
                "emails": emails_by_user.get(int(u.id), [u.email] if u.email else []),
                "display_name": display_name_for_user(u),
                "avatar_url": u.avatar_url,
            }
        )
    return {"results": results}


@api_bp.get("/users/<int:user_id>/card")
@login_required
def user_card(user_id: int):
    viewer = current_user()
    target = User.query.get_or_404(user_id)
    email_rows = (
        UserEmail.query.filter(UserEmail.user_id == target.id)
        .order_by(UserEmail.is_primary.desc(), UserEmail.email.asc())
        .all()
    )
    emails: list[str] = []
    for row in email_rows:
        if row.email and row.email not in emails:
            emails.append(row.email)
    if target.email and target.email not in emails:
        emails.append(target.email)
    return {
        "id": target.id,
        "display_name": display_name_for_user(target) or target.email or "User",
        "email": target.email,
        "emails": emails,
        "avatar_url": target.avatar_url,
        "viewer_id": viewer.id,
    }


@api_bp.get("/tasks/search")
@login_required
def search_tasks():
    user = current_user()
    q = (request.args.get("q") or "").strip()
    entity_type_filter = (request.args.get("entity_type") or request.args.get("type") or "").strip().lower()
    exclude_task_id_raw = (request.args.get("exclude_task_id") or "").strip()
    exclude_task_id = None
    if exclude_task_id_raw:
        try:
            exclude_task_id = int(exclude_task_id_raw)
        except ValueError:
            exclude_task_id = None
    if len(q) < 2:
        return {"results": []}
    active_theme_name = normalize_theme_name(getattr(user, "theme_name", None) or DEFAULT_THEME_NAME)
    accessible_projects = _accessible_projects_for_user(user)
    accessible_project_ids = [project.id for project in accessible_projects]
    if not accessible_project_ids:
        return {"results": []}
    project_map = {project.id: project for project in accessible_projects}
    sidebar_divisions = Division.query.filter_by(owner_id=user.id).order_by(Division.position.asc(), Division.id.asc()).all()
    pref_map = sidebar_preference_map(user.id, [project for project in accessible_projects if not project.is_direct], sidebar_divisions)
    division_map = {division.id: division for division in sidebar_divisions}
    group_map = {
        group.id: group
        for group in Group.query.filter(Group.project_id.in_(accessible_project_ids)).all()
    }
    needle = f"%{q}%"
    tasks = (
        Task.query
        .join(Project, Project.id == Task.project_id)
        .outerjoin(Group, Group.id == Task.group_id)
        .filter(Task.project_id.in_(accessible_project_ids))
        .filter(
            or_(
                Task.title.ilike(needle),
                Project.name.ilike(needle),
                Group.name.ilike(needle),
            )
        )
        .order_by(Task.created_at.desc(), Task.id.desc())
        .limit(12)
        .all()
    )
    if exclude_task_id is not None:
        tasks = [task for task in tasks if int(task.id) != int(exclude_task_id)]
    groups = (
        Group.query
        .join(Project, Project.id == Group.project_id)
        .filter(Group.project_id.in_(accessible_project_ids))
        .filter(
            or_(
                Group.name.ilike(needle),
                Project.name.ilike(needle),
            )
        )
        .order_by(Group.id.desc())
        .limit(8)
        .all()
    )
    projects = (
        Project.query
        .filter(Project.id.in_(accessible_project_ids))
        .filter(Project.name.ilike(needle))
        .order_by(Project.id.desc())
        .limit(8)
        .all()
    )
    results = []
    for task in tasks:
        project = project_map.get(task.project_id)
        group = group_map.get(task.group_id) if task.group_id else None
        project_name = _project_display_name_for_user(project, user.id)
        pref = pref_map.get(task.project_id)
        division = division_map.get(pref.division_id) if pref and pref.division_id else None
        show_project_label = bool(project and not project.is_direct and division)
        try:
            status_meta = task_status_meta(task, viewer_user_id=user.id)
        except Exception:
            current_app.logger.exception("task search status_meta failed for task %s", task.id)
            status_meta = {}
        results.append(
            {
                "entity_type": "task",
                "id": task.id,
                "title": task.title,
                "project_id": task.project_id,
                "project_name": project_name,
                "group_id": task.group_id,
                "group_name": group.name if group else None,
                "division_name": division.name if division else None,
                "show_project_label": show_project_label,
                "project_color": division_effective_color(division, active_theme_name) if show_project_label and division else None,
                "status": task.status,
                "status_mode": _task_status_mode(task),
                "status_percentage": _task_status_percentage(task),
                "status_meta": status_meta,
                "prereq_blocked": bool(status_meta.get("prereq_blocked")) if isinstance(status_meta, dict) else False,
            }
        )
    for group in groups:
        project = project_map.get(group.project_id)
        project_name = _project_display_name_for_user(project, user.id)
        pref = pref_map.get(group.project_id)
        division = division_map.get(pref.division_id) if pref and pref.division_id else None
        show_project_label = bool(project and not project.is_direct and division)
        results.append(
            {
                "entity_type": "group",
                "id": group.id,
                "title": group.name,
                "project_id": group.project_id,
                "project_name": project_name,
                "group_id": group.id,
                "group_name": group.name,
                "division_name": division.name if division else None,
                "show_project_label": show_project_label,
                "project_color": division_effective_color(division, active_theme_name) if show_project_label and division else None,
                "status": None,
            }
        )
    for project in projects:
        project_name = _project_display_name_for_user(project, user.id)
        pref = pref_map.get(project.id)
        division = division_map.get(pref.division_id) if pref and pref.division_id else None
        show_project_label = bool(project and not project.is_direct and division)
        results.append(
            {
                "entity_type": "project",
                "id": project.id,
                "title": project_name,
                "project_id": project.id,
                "project_name": project_name,
                "group_id": None,
                "group_name": None,
                "division_name": division.name if division else None,
                "show_project_label": show_project_label,
                "project_color": division_effective_color(division, active_theme_name) if show_project_label and division else None,
                "status": None,
            }
        )
    type_rank = {"project": 0, "group": 1, "task": 2}
    if entity_type_filter in {"task", "group", "project"}:
        results = [item for item in results if item.get("entity_type") == entity_type_filter]
    deduped = []
    seen: set[tuple[str, int]] = set()
    for item in sorted(results, key=lambda item: (type_rank.get(item["entity_type"], 9), str(item["title"] or "").lower(), -int(item["id"]))):
        key = (str(item["entity_type"]), int(item["id"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= 20:
            break
    return {"results": deduped}


@api_bp.post("/direct-projects")
@login_required
def create_direct_project():
    user = current_user()
    payload = request.get_json(silent=True) or {}
    target_user_id = payload.get("user_id")
    try:
        target_user_id = int(target_user_id)
    except (TypeError, ValueError):
        return {"error": "user_id is required"}, 400
    target_user = User.query.get(target_user_id)
    if not target_user:
        return {"error": "user not found"}, 404

    user_a_id, user_b_id = sorted([int(user.id), int(target_user.id)])
    project = Project.query.filter_by(
        is_direct=True,
        direct_user_a_id=user_a_id,
        direct_user_b_id=user_b_id,
    ).first()
    created = False
    if not project:
        label_a = user.display_name or user.email or f"User {user.id}"
        label_b = target_user.display_name or target_user.email or f"User {target_user.id}"
        project_name = label_a if int(user.id) == int(target_user.id) else f"{label_a} · {label_b}"
        project = Project(
            name=project_name,
            owner_id=user.id,
            is_direct=True,
            direct_user_a_id=user_a_id,
            direct_user_b_id=user_b_id,
            division_id=None,
            position=0,
        )
        db.session.add(project)
        db.session.flush()
        if not ProjectMember.query.filter_by(project_id=project.id, user_id=target_user.id).first():
            db.session.add(ProjectMember(project_id=project.id, user_id=target_user.id))
        db.session.flush()
        log_project_history(project, actor=user, action="created")
        db.session.commit()
        emit_project_created(project, actor_user_id=user.id, recipient_user_id=user.id)
        emit_project_created(project, actor_user_id=user.id, recipient_user_id=target_user.id)
        created = True

    return {
        "project": _serialize_project_payload_for_user(project, user.id),
        "created": created,
    }, 201 if created else 200


@api_bp.get("/direct-projects")
@login_required
def list_direct_projects():
    user = current_user()
    member_project_ids = [row.project_id for row in ProjectMember.query.filter_by(user_id=user.id).all()]
    projects = (
        Project.query.filter(
            Project.is_direct.is_(True),
            or_(Project.owner_id == user.id, Project.id.in_(member_project_ids if member_project_ids else [-1])),
        )
        .all()
    )
    results = [_serialize_project_payload_for_user(project, user.id) for project in projects]
    results.sort(key=lambda item: ((item.get("display_name") or item.get("name") or "").lower(), item.get("id") or 0))
    return {"results": results}


@api_bp.post("/admin/resequence_tasks")
@login_required
def admin_resequence_tasks():
    user = current_user()
    if not user_is_admin(user):
        return {"error": "unauthorized"}, 403
    buckets = db.session.query(Task.project_id, Task.group_id).distinct().all()
    for project_id, group_id in buckets:
        _resequence_tasks(project_id, group_id)
    db.session.commit()
    return {"status": "ok", "buckets": len(buckets)}, 200


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
    group_ids = [group_id for (group_id,) in db.session.query(Group.id).filter(Group.project_id == project_id).all()]
    if group_ids:
        GroupMember.query.filter(GroupMember.group_id.in_(group_ids), GroupMember.user_id == user_id).delete(synchronize_session=False)
    db.session.commit()
    _sync_project_group_mode_tasks(project_id, actor_user_id=user.id)
    _sync_project_follow_mode_tasks(project_id, actor_user_id=user.id)
    project = Project.query.get(project_id)
    emit_project_created(project, actor_user_id=user.id, recipient_user_id=user_id)
    _queue_project_shared_notification(member_user, user, project)
    emit_project_members_updated(project_id, actor_user_id=user.id)
    for group_id in group_ids:
        emit_group_members_updated(group_id, actor_user_id=user.id)
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
    existing = ProjectMember.query.filter_by(project_id=group.project_id, user_id=user_id).first()
    if existing:
        return {"status": "ok"}, 200
    db.session.add(ProjectMember(project_id=group.project_id, user_id=user_id))
    group_ids = [item_id for (item_id,) in db.session.query(Group.id).filter(Group.project_id == group.project_id).all()]
    if group_ids:
        GroupMember.query.filter(GroupMember.group_id.in_(group_ids), GroupMember.user_id == user_id).delete(synchronize_session=False)
    db.session.commit()
    _sync_project_group_mode_tasks(group.project_id, actor_user_id=user.id)
    _sync_project_follow_mode_tasks(group.project_id, actor_user_id=user.id)
    project = Project.query.get(group.project_id)
    emit_project_created(project, actor_user_id=user.id, recipient_user_id=user_id)
    _queue_project_shared_notification(member_user, user, project)
    emit_project_members_updated(group.project_id, actor_user_id=user.id)
    for item_id in group_ids:
        emit_group_members_updated(item_id, actor_user_id=user.id)
    return {"status": "ok"}, 201


@api_bp.get("/projects/<int:project_id>/members")
@login_required
def list_project_members(project_id: int):
    user = current_user()
    if not _can_access_project(user, project_id):
        return {"error": "unauthorized"}, 403
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    members = list(project_access_map(project_id).values())
    for member in members:
        if member.get("owner"):
            member["access_label"] = "Owner"
        else:
            member["access_label"] = "Shared"
        member["can_promote"] = False
        member["can_demote"] = False
    members.sort(key=lambda item: (0 if item.get("owner") else 1, (item.get("display_name") or item.get("email") or "").lower()))
    return {"members": members}, 200


@api_bp.get("/groups/<int:group_id>/members")
@login_required
def list_group_members(group_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_access_project(user, group.project_id):
        return {"error": "unauthorized"}, 403

    members = list(project_access_map(group.project_id).values())
    members.sort(key=lambda item: (0 if item.get("owner") else 1, (item.get("display_name") or item.get("email") or "").lower()))
    return {"members": members}, 200


@api_bp.delete("/projects/<int:project_id>/members/<int:user_id>")
@login_required
def remove_project_member(project_id: int, user_id: int):
    user = current_user()
    if not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    member_user = User.query.get(user_id)
    project = Project.query.get(project_id)
    ProjectMember.query.filter_by(project_id=project_id, user_id=user_id).delete()
    group_ids = [group_id for (group_id,) in db.session.query(Group.id).filter(Group.project_id == project_id).all()]
    if group_ids:
        GroupMember.query.filter(GroupMember.group_id.in_(group_ids), GroupMember.user_id == user_id).delete(synchronize_session=False)
    db.session.commit()
    _sync_project_group_mode_tasks(project_id, actor_user_id=user.id)
    _sync_project_follow_mode_tasks(project_id, actor_user_id=user.id)
    emit_project_deleted(project_id, actor_user_id=user.id, recipient_user_ids=[user_id], include_project_room=False)
    if member_user and project:
        _queue_project_removed_notification(member_user, user, project)
    emit_project_members_updated(project_id, actor_user_id=user.id)
    for group_id in group_ids:
        emit_group_members_updated(group_id, actor_user_id=user.id)
    return {"status": "deleted"}, 200


@api_bp.post("/projects/<int:project_id>/members/<int:user_id>/scope")
@login_required
def update_project_member_scope(project_id: int, user_id: int):
    user = current_user()
    if not _can_manage_project(user, project_id):
        return {"error": "unauthorized"}, 403
    project = Project.query.get(project_id)
    if not project:
        return {"error": "project not found"}, 404
    if user_id == project.owner_id:
        return {"error": "cannot change owner scope"}, 400

    payload = request.get_json(silent=True) or {}
    scope = (payload.get("scope") or "").strip().lower()
    if scope not in {"full", "partial"}:
        return {"error": "invalid scope"}, 400

    existing = ProjectMember.query.filter_by(project_id=project_id, user_id=user_id).first()
    member_user = User.query.get(user_id)
    if scope == "full":
        if not existing:
            db.session.add(ProjectMember(project_id=project_id, user_id=user_id))
    else:
        if existing:
            db.session.delete(existing)
        group_ids = [group_id for (group_id,) in db.session.query(Group.id).filter(Group.project_id == project_id).all()]
        if group_ids:
            GroupMember.query.filter(GroupMember.group_id.in_(group_ids), GroupMember.user_id == user_id).delete(synchronize_session=False)
    db.session.commit()
    _sync_project_group_mode_tasks(project_id, actor_user_id=user.id)
    _sync_project_follow_mode_tasks(project_id, actor_user_id=user.id)
    if scope == "partial" and member_user:
        _queue_project_removed_notification(member_user, user, project)
    emit_project_members_updated(project_id, actor_user_id=user.id)
    for group_id in [group_id for (group_id,) in db.session.query(Group.id).filter(Group.project_id == project_id).all()]:
        emit_group_members_updated(group_id, actor_user_id=user.id)
    return {"status": "ok"}, 200


@api_bp.delete("/groups/<int:group_id>/members/<int:user_id>")
@login_required
def remove_group_member(group_id: int, user_id: int):
    user = current_user()
    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not _can_manage_project(user, group.project_id):
        return {"error": "unauthorized"}, 403
    member_user = User.query.get(user_id)
    ProjectMember.query.filter_by(project_id=group.project_id, user_id=user_id).delete()
    group_ids = [item_id for (item_id,) in db.session.query(Group.id).filter(Group.project_id == group.project_id).all()]
    if group_ids:
        GroupMember.query.filter(GroupMember.group_id.in_(group_ids), GroupMember.user_id == user_id).delete(synchronize_session=False)
    db.session.commit()
    _sync_project_group_mode_tasks(group.project_id, actor_user_id=user.id)
    _sync_project_follow_mode_tasks(group.project_id, actor_user_id=user.id)
    emit_project_deleted(group.project_id, actor_user_id=user.id, recipient_user_ids=[user_id], include_project_room=False)
    if member_user:
        _queue_project_removed_notification(member_user, user, Project.query.get(group.project_id))
    emit_project_members_updated(group.project_id, actor_user_id=user.id)
    for item_id in group_ids:
        emit_group_members_updated(item_id, actor_user_id=user.id)
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
    emit_group_reordered(project.id, order_ids, actor_user_id=user.id)
    return {"status": "ok"}, 200


@api_bp.post("/groups/<int:group_id>/move")
@login_required
def move_group(group_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()
    target_project_id = payload.get("project_id")
    before_group_id = payload.get("before_group_id")

    group = Group.query.get(group_id)
    if not group:
        return {"error": "group not found"}, 404
    if not target_project_id:
        return {"error": "project_id is required"}, 400

    try:
        target_project_id = int(target_project_id)
    except (TypeError, ValueError):
        return {"error": "invalid project_id"}, 400

    target_project = Project.query.get(target_project_id)
    if not target_project:
        return {"error": "project not found"}, 404

    source_project_id = group.project_id
    if not _can_access_project(user, source_project_id):
        return {"error": "unauthorized"}, 403
    if not _can_access_project(user, target_project_id):
        return {"error": "unauthorized"}, 403

    before_group = None
    if before_group_id:
        try:
            before_group_id = int(before_group_id)
        except (TypeError, ValueError):
            return {"error": "invalid before_group_id"}, 400
        before_group = Group.query.get(before_group_id)
        if not before_group:
            return {"error": "before_group not found"}, 404
        if before_group.id == group.id:
            before_group = None
        elif before_group.project_id != target_project_id:
            return {"error": "before_group project mismatch"}, 400

    if source_project_id == target_project_id:
        siblings = [row for row in Group.query.filter_by(project_id=target_project_id).order_by(Group.position.asc(), Group.id.asc()).all() if row.id != group.id]
    else:
        siblings = Group.query.filter_by(project_id=target_project_id).order_by(Group.position.asc(), Group.id.asc()).all()

    insert_at = len(siblings)
    if before_group:
        for idx, sibling in enumerate(siblings):
            if sibling.id == before_group.id:
                insert_at = idx
                break

    old_task_rows = Task.query.filter_by(group_id=group.id).all()
    old_task_ids = [task.id for task in old_task_rows]

    if source_project_id != target_project_id:
        source_groups = [row for row in Group.query.filter_by(project_id=source_project_id).order_by(Group.position.asc(), Group.id.asc()).all() if row.id != group.id]
        for idx, sibling in enumerate(source_groups, start=1):
            sibling.position = idx
        group.project_id = target_project_id
        for task in old_task_rows:
            task.project_id = target_project_id

    siblings.insert(insert_at, group)
    for idx, sibling in enumerate(siblings, start=1):
        sibling.position = idx

    db.session.commit()

    if source_project_id != target_project_id:
        emit_group_deleted(group.id, source_project_id, actor_user_id=user.id, task_ids=old_task_ids)
        emit_group_created(group, actor_user_id=user.id)
        for task in old_task_rows:
            emit_task_updated(task, action="moved", old_project_id=source_project_id, old_group_id=group.id, actor_user_id=user.id)
    else:
        emit_group_reordered(target_project_id, [row.id for row in siblings], actor_user_id=user.id)
        emit_group_updated(group, actor_user_id=user.id)

    return {"status": "ok", "group": {"id": group.id, "project_id": group.project_id}}, 200


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
    emit_sidebar_reordered(user.id, order, actor_user_id=user.id)
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
    if _task_is_locked(task):
        return _locked_task_response()

    invite_query = Invite.query.filter(Invite.task_id == task.id)
    invite_count = invite_query.filter(Invite.status != "draft").count()

    requires_confirm = invite_count > 0
    if request.args.get("preview") == "1":
        return {
            "preview": True,
            "requires_confirm": requires_confirm,
            "invite_count": invite_count,
        }, 200
    if request.args.get("confirm") != "1":
        if requires_confirm:
            return {
                "requires_confirm": True,
                "invite_count": invite_count,
            }, 200
        # No confirm needed; proceed with delete

    impacted_ids = [next_task_id for next_task_id in _task_impacted_ids([task.id]) if int(next_task_id) != int(task.id)]
    Assignment.query.filter_by(task_id=task.id).delete()
    TaskPrerequisite.query.filter(
        or_(
            TaskPrerequisite.task_id == task.id,
            TaskPrerequisite.prerequisite_task_id == task.id,
        )
    ).delete(synchronize_session=False)
    Invite.query.filter_by(task_id=task.id).delete()
    log_task_history(task, actor=user, action="deleted")
    db.session.delete(task)
    db.session.commit()
    emit_task_updated(task, action="deleted", old_project_id=task.project_id, old_group_id=task.group_id, actor_user_id=user.id)
    if impacted_ids:
        impacted_tasks = Task.query.filter(Task.id.in_(impacted_ids)).all()
        if impacted_tasks:
            emit_tasks_updated(impacted_tasks, actor_user_id=user.id)
            for impacted_task in impacted_tasks:
                emit_task_updated(impacted_task, actor_user_id=user.id)
    return {"status": "deleted"}, 200


@api_bp.post("/assignments")
@login_required
def create_assignment():
    payload = request.get_json(silent=True) or {}
    user = current_user()
    target_type = payload.get("target_type")
    target_id = payload.get("target_id")
    email = (payload.get("email") or "").strip().lower()

    if target_type != "task" or not target_id or not email:
        return {"error": "target_type, target_id, and email are required"}, 400
    if "@" not in email:
        return {"error": "invalid email"}, 400

    task = Task.query.get(target_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403
    if _task_is_locked(task):
        return _locked_task_response()

    account_user = find_user_by_email(email)
    project = Project.query.get(task.project_id) if task else None
    project_share_required = _assignment_requires_project_share_payload(project, account_user)
    if project_share_required:
        return project_share_required, 200
    if not account_user:
        get_or_create_collaborator_profile(
            email=email,
            default_calendar_opt_in=project.default_invitee_calendar_opt_in if project else False,
        )
    existing_assignment = _existing_assignment_for_target(
        task_id=task.id,
        account_user_id=account_user.id if account_user else None,
        email=email,
    )
    if existing_assignment:
        existing_assignment = _collapse_duplicate_task_assignments(
            task_id=task.id,
            account_user_id=account_user.id if account_user else None,
            email=email,
        ) or existing_assignment
        _reset_existing_assignment(existing_assignment)
        db.session.commit()
        actor_name = display_name_for_user(user) or user.email or "Someone"
        assignee_label = _assignment_history_label(
            {
                "display_name": account_user.display_name if account_user else None,
                "display_email": account_user.email if account_user else existing_assignment.email,
                "email": existing_assignment.email,
            }
        )
        history_task_body = actor_name + " assigned to " + assignee_label + "."
        history_scoped_body = actor_name + " assigned task " + _quoted_history_value(task.title, "Task") + " to " + assignee_label + "."
        log_task_history(task, actor=user, action="updated", changed_fields=["assignees"], task_body=history_task_body, scoped_body=history_scoped_body)
        db.session.commit()
        _queue_assignment_added_notifications(
            task,
            actor_user=user,
            fallback_assignee_label=display_name_for_user(account_user) if account_user else (existing_assignment.email or email),
        )
        emit_assignment_updated(task, existing_assignment, actor_user_id=user.id)
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
        task_id=task.id,
        user_id=account_user.id if account_user else None,
        email=email if not account_user else None,
        status="assigned" if account_user else "draft",
    )
    db.session.add(assignment)
    db.session.commit()
    assignment = _collapse_duplicate_task_assignments(
        task_id=task.id,
        account_user_id=account_user.id if account_user else None,
        email=email,
    ) or assignment
    db.session.commit()
    actor_name = display_name_for_user(user) or user.email or "Someone"
    assignee_label = _assignment_history_label(
        {
            "display_name": account_user.display_name if account_user else None,
            "display_email": account_user.email if account_user else assignment.email,
            "email": assignment.email,
        }
    )
    history_task_body = actor_name + " assigned to " + assignee_label + "."
    history_scoped_body = actor_name + " assigned task " + _quoted_history_value(task.title, "Task") + " to " + assignee_label + "."
    log_task_history(task, actor=user, action="updated", changed_fields=["assignees"], task_body=history_task_body, scoped_body=history_scoped_body)
    db.session.commit()
    _queue_assignment_added_notifications(
        task,
        actor_user=user,
        fallback_assignee_label=display_name_for_user(account_user) if account_user else (assignment.email or email),
    )
    emit_assignment_updated(task, assignment, action="created", actor_user_id=user.id)

    return {
        "id": assignment.id,
        "user_id": assignment.user_id,
        "email": assignment.email,
        "status": assignment.status,
        "display_name": account_user.display_name if account_user else None,
        "display_email": account_user.email if account_user else assignment.email,
        "avatar_url": account_user.avatar_url if account_user else None,
    }, 201


@api_bp.post("/tasks/<int:task_id>/assign_all")
@login_required
def assign_all_task_members(task_id: int):
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403
    if _task_is_locked(task):
        return _locked_task_response()
    task.assign_group_members = True
    changes = sync_group_task_assignments(task)
    db.session.commit()
    actor_name = display_name_for_user(user) or user.email or "Someone"
    created_labels = [_assignment_history_label(_serialize_assignment_row(row)) for row in changes["created"][:3]]
    if created_labels:
        history_task_body = actor_name + " assigned to group members " + ", ".join(created_labels) + "."
        history_scoped_body = actor_name + " assigned task " + _quoted_history_value(task.title, "Task") + " to group members " + ", ".join(created_labels) + "."
    else:
        history_task_body = actor_name + " enabled group assignment mode."
        history_scoped_body = actor_name + " enabled group assignment mode for task " + _quoted_history_value(task.title, "Task") + "."
    log_task_history(task, actor=user, action="updated", changed_fields=["assignment mode", "assignees"], task_body=history_task_body, scoped_body=history_scoped_body)
    db.session.commit()
    for assignment in changes["deleted"]:
        emit_assignment_updated(task, assignment, action="deleted", actor_user_id=user.id)
    for assignment in changes["created"]:
        emit_assignment_updated(task, assignment, action="created", actor_user_id=user.id)
    _queue_group_assignment_notifications(task, changes["created"], user)
    emit_task_updated(task, actor_user_id=user.id)
    return {
        "assign_group_members": True,
        "group_assignment_members": serialize_group_assignment_members(task),
        "assignments": [_serialize_assignment_row(row) for row in Assignment.query.filter_by(task_id=task.id).all()],
    }, 200


@api_bp.post("/tasks/<int:task_id>/followers")
@login_required
def create_task_follower(task_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    follower_user = None
    follower_user_id = payload.get("user_id")
    email = (payload.get("email") or "").strip().lower()
    if follower_user_id not in (None, "", 0, "0"):
        try:
            follower_user_id = int(follower_user_id)
        except (TypeError, ValueError):
            return {"error": "invalid user_id"}, 400
        follower_user = User.query.get(follower_user_id)
    elif email:
        follower_user = find_user_by_email(email)
    if not follower_user:
        return {"error": "account user not found"}, 400
    if not _account_user_has_project_access(task.project_id, follower_user.id):
        return {"error": "user must have project access to follow this task"}, 400

    existing = TaskFollower.query.filter_by(task_id=task.id, user_id=follower_user.id).first()
    if existing:
        return _serialize_follower_row(existing), 200

    follower = TaskFollower(task_id=task.id, user_id=follower_user.id)
    db.session.add(follower)
    db.session.commit()
    history_task_body, history_scoped_body = _task_update_history_bodies(
        actor_name=display_name_for_user(user) or user.email or "Someone",
        task_title=task.title,
        changed_fields=["followers"],
        new_title=task.title,
        new_due_mode=_task_due_mode(task),
        new_due_at=task.due_at,
        new_start_date=_task_start_date(task),
        new_status=task.status,
        new_status_mode=_task_status_mode(task),
        new_status_percentage=_task_status_percentage(task),
        followers=[_serialize_follower_row(row) for row in TaskFollower.query.filter_by(task_id=task.id).all()],
    )
    log_task_history(task, actor=user, action="updated", changed_fields=["followers"], task_body=history_task_body, scoped_body=history_scoped_body)
    db.session.commit()
    impacted_ids = _task_impacted_ids([task.id, prerequisite_task.id])
    impacted_tasks = Task.query.filter(Task.id.in_(impacted_ids)).all()
    if impacted_tasks:
        emit_tasks_updated(impacted_tasks, actor_user_id=user.id)
    emit_task_notification_updates(task, exclude_user_id=user.id)
    return _serialize_follower_row(follower), 201


@api_bp.post("/tasks/<int:task_id>/follow_all")
@login_required
def create_task_followers_for_project(task_id: int):
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    _set_task_follow_project_members(task, True)
    desired_user_ids = set(_project_access_user_ids(task.project_id))
    existing_rows = TaskFollower.query.filter_by(task_id=task.id).all()
    existing_by_user_id = {int(row.user_id): row for row in existing_rows if row.user_id}
    changed = False
    for row in existing_rows:
        if row.user_id and int(row.user_id) not in desired_user_ids:
            db.session.delete(row)
            changed = True
    for follower_user_id in desired_user_ids:
        if follower_user_id in existing_by_user_id:
            continue
        db.session.add(TaskFollower(task_id=task.id, user_id=follower_user_id))
        changed = True

    db.session.commit()
    if changed or _task_follow_project_members(task):
        history_task_body, history_scoped_body = _task_update_history_bodies(
            actor_name=display_name_for_user(user) or user.email or "Someone",
            task_title=task.title,
            changed_fields=["follower mode", "followers"],
            new_title=task.title,
            new_due_mode=_task_due_mode(task),
            new_due_at=task.due_at,
            new_start_date=_task_start_date(task),
            new_status=task.status,
            new_status_mode=_task_status_mode(task),
            new_status_percentage=_task_status_percentage(task),
            followers=[_serialize_follower_row(row) for row in TaskFollower.query.filter_by(task_id=task.id).all()],
        )
        log_task_history(task, actor=user, action="updated", changed_fields=["follower mode", "followers"], task_body=history_task_body, scoped_body=history_scoped_body)
        db.session.commit()
        emit_task_updated(task, actor_user_id=user.id)
        emit_task_notification_updates(task, exclude_user_id=user.id)

    followers = TaskFollower.query.filter_by(task_id=task.id).all()
    return {
        "follow_project_members": True,
        "project_follower_members": _serialize_project_follower_members(task),
        "followers": [_serialize_follower_row(row) for row in followers],
    }, 200


@api_bp.delete("/task-followers/<int:follower_id>")
@login_required
def delete_task_follower(follower_id: int):
    user = current_user()
    follower = TaskFollower.query.get(follower_id)
    if not follower:
        return {"error": "task follower not found"}, 404

    task = Task.query.get(follower.task_id) if follower.task_id else None
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    db.session.delete(follower)
    db.session.commit()
    history_task_body, history_scoped_body = _task_update_history_bodies(
        actor_name=display_name_for_user(user) or user.email or "Someone",
        task_title=task.title,
        changed_fields=["followers"],
        new_title=task.title,
        new_due_mode=_task_due_mode(task),
        new_due_at=task.due_at,
        new_start_date=_task_start_date(task),
        new_status=task.status,
        new_status_mode=_task_status_mode(task),
        new_status_percentage=_task_status_percentage(task),
        followers=[_serialize_follower_row(row) for row in TaskFollower.query.filter_by(task_id=task.id).all()],
    )
    log_task_history(task, actor=user, action="updated", changed_fields=["followers"], task_body=history_task_body, scoped_body=history_scoped_body)
    db.session.commit()
    impacted_ids = _task_impacted_ids([task.id, prerequisite_task.id if prerequisite_task else None])
    impacted_tasks = Task.query.filter(Task.id.in_(impacted_ids)).all()
    if impacted_tasks:
        emit_tasks_updated(impacted_tasks, actor_user_id=user.id)
    emit_task_notification_updates(task, exclude_user_id=user.id)
    return {"status": "deleted"}, 200


@api_bp.post("/tasks/<int:task_id>/prerequisites")
@login_required
def create_task_prerequisite(task_id: int):
    payload = request.get_json(silent=True) or {}
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403
    if _task_is_locked(task):
        return _locked_task_response()

    prerequisite_task_id = payload.get("prerequisite_task_id")
    try:
        prerequisite_task_id = int(prerequisite_task_id)
    except (TypeError, ValueError):
        return {"error": "prerequisite_task_id is required"}, 400
    prerequisite_task = Task.query.get(prerequisite_task_id)
    if not prerequisite_task:
        return {"error": "prerequisite task not found"}, 404
    if not _can_access_task(user, prerequisite_task):
        return {"error": "unauthorized"}, 403
    if int(prerequisite_task.id) == int(task.id):
        return {"error": "task cannot depend on itself"}, 400
    if _task_prerequisite_would_create_cycle(task.id, prerequisite_task.id):
        return {"error": "prerequisite would create a cycle"}, 400

    existing = TaskPrerequisite.query.filter_by(
        task_id=task.id,
        prerequisite_task_id=prerequisite_task.id,
    ).first()
    if existing:
        serialized_existing = _serialize_task_prerequisite_row(existing)
        return serialized_existing or {"status": "ok"}, 200

    prerequisite = TaskPrerequisite(
        task_id=task.id,
        prerequisite_task_id=prerequisite_task.id,
    )
    db.session.add(prerequisite)
    actor_name = display_name_for_user(user) or user.email or "Someone"
    history_task_body = actor_name + " added prerequisite " + _quoted_history_value(prerequisite_task.title, "Task") + "."
    history_scoped_body = actor_name + " added prerequisite " + _quoted_history_value(prerequisite_task.title, "Task") + " to task " + _quoted_history_value(task.title, "Task") + "."
    log_task_history(
        task,
        actor=user,
        action="updated",
        changed_fields=["prerequisites"],
        task_body=history_task_body,
        scoped_body=history_scoped_body,
    )
    db.session.commit()
    impacted_ids = _task_impacted_ids([task.id, prerequisite_task.id])
    impacted_tasks = _touch_tasks(impacted_ids)
    if impacted_tasks:
        emit_tasks_updated(impacted_tasks, actor_user_id=user.id)
    emit_task_notification_updates(task, exclude_user_id=user.id)
    return _serialize_task_prerequisite_row(prerequisite) or {"status": "ok"}, 201


@api_bp.delete("/task-prerequisites/<int:prerequisite_id>")
@login_required
def delete_task_prerequisite(prerequisite_id: int):
    user = current_user()
    prerequisite = TaskPrerequisite.query.get(prerequisite_id)
    if not prerequisite:
        return {"error": "task prerequisite not found"}, 404
    task = Task.query.get(prerequisite.task_id)
    prerequisite_task = Task.query.get(prerequisite.prerequisite_task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403
    if _task_is_locked(task):
        return _locked_task_response()

    prerequisite_title = prerequisite_task.title if prerequisite_task else "Task"
    db.session.delete(prerequisite)
    actor_name = display_name_for_user(user) or user.email or "Someone"
    history_task_body = actor_name + " removed prerequisite " + _quoted_history_value(prerequisite_title, "Task") + "."
    history_scoped_body = actor_name + " removed prerequisite " + _quoted_history_value(prerequisite_title, "Task") + " from task " + _quoted_history_value(task.title, "Task") + "."
    log_task_history(
        task,
        actor=user,
        action="updated",
        changed_fields=["prerequisites"],
        task_body=history_task_body,
        scoped_body=history_scoped_body,
    )
    db.session.commit()
    impacted_ids = _task_impacted_ids([task.id, prerequisite_task.id if prerequisite_task else None])
    impacted_tasks = _touch_tasks(impacted_ids)
    if impacted_tasks:
        emit_tasks_updated(impacted_tasks, actor_user_id=user.id)
    emit_task_notification_updates(task, exclude_user_id=user.id)
    return {"status": "deleted"}, 200


@api_bp.post("/tasks/<int:task_id>/poll_response")
@login_required
def save_task_poll_response(task_id: int):
    user = current_user()
    task = Task.query.get(task_id)
    if not task:
        return {"error": "task not found"}, 404
    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403
    if _task_type(task) != "poll":
        return {"error": "task is not a poll"}, 400
    payload = request.get_json(silent=True) or {}
    option_ids = payload.get("option_ids")
    if option_ids is None:
        option_ids = []
    if not isinstance(option_ids, list):
        return {"error": "option_ids must be a list"}, 400
    is_assigned = (
        Assignment.query.filter_by(task_id=task.id, user_id=user.id).first() is not None
        or Assignment.query.filter_by(task_id=task.id, email=(user.email or "").strip().lower()).first() is not None
    )
    if not is_assigned:
        return {"error": "only assignees can respond to this poll"}, 403
    poll_payload = _set_task_poll_response(
        task,
        user_id=user.id,
        email=user.email,
        option_ids=option_ids,
    )
    db.session.commit()
    response_count = len(
        [
            row
            for row in list(poll_payload.get("responses") or [])
            if isinstance(row, dict) and list(row.get("option_ids") or [])
        ]
    )
    task_body = f'{display_name_for_user(user) or user.email or "Someone"} answered the poll on task "{task.title}" ({response_count} responses).'
    scoped_body = f'{display_name_for_user(user) or user.email or "Someone"} answered the poll on task "{task.title}" ({response_count} responses).'
    log_task_history(task, actor=user, action="updated", changed_fields=["poll response"], task_body=task_body, scoped_body=scoped_body)
    db.session.commit()
    impacted_ids = _task_impacted_ids([task.id])
    impacted_tasks = _touch_tasks(impacted_ids)
    if impacted_tasks:
        emit_tasks_updated(impacted_tasks, actor_user_id=user.id)
    return _serialize_task_row(task, viewer_user_id=user.id), 200


def _sync_group_mode_tasks(group_id: int, *, actor_user_id: int | None = None) -> None:
    tasks = Task.query.filter_by(group_id=group_id, assign_group_members=True).all()
    if not tasks:
        return
    _sync_assign_group_member_tasks(tasks, actor_user_id=actor_user_id)


def _sync_assign_group_member_tasks(tasks: list[Task], *, actor_user_id: int | None = None) -> None:
    tasks = [task for task in (tasks or []) if task and task.id and task.assign_group_members]
    if not tasks:
        return
    emitted = []
    for task in tasks:
        changes = sync_group_task_assignments(task)
        emitted.append((task, changes))
    db.session.commit()
    for task, changes in emitted:
        for assignment in changes["deleted"]:
            emit_assignment_updated(task, assignment, action="deleted", actor_user_id=actor_user_id)
        for assignment in changes["created"]:
            emit_assignment_updated(task, assignment, action="created", actor_user_id=actor_user_id)
        actor_user = User.query.get(actor_user_id) if actor_user_id else None
        _queue_group_assignment_notifications(task, changes["created"], actor_user)
        actor_name = display_name_for_user(actor_user) or (actor_user.email if actor_user else "") or "Someone"
        created_labels = [_assignment_history_label(_serialize_assignment_row(row)) for row in changes["created"][:3]]
        removed_labels = [_assignment_history_label(_serialize_assignment_row(row)) for row in changes["deleted"][:3]]
        if created_labels and removed_labels:
            history_task_body = actor_name + " synced assignees; added " + ", ".join(created_labels) + " and removed " + ", ".join(removed_labels) + "."
            history_scoped_body = actor_name + " synced assignees for task " + _quoted_history_value(task.title, "Task") + "; added " + ", ".join(created_labels) + " and removed " + ", ".join(removed_labels) + "."
        elif created_labels:
            history_task_body = actor_name + " assigned to group members " + ", ".join(created_labels) + "."
            history_scoped_body = actor_name + " assigned task " + _quoted_history_value(task.title, "Task") + " to group members " + ", ".join(created_labels) + "."
        elif removed_labels:
            history_task_body = actor_name + " removed assignees " + ", ".join(removed_labels) + "."
            history_scoped_body = actor_name + " removed assignees " + ", ".join(removed_labels) + " from task " + _quoted_history_value(task.title, "Task") + "."
        else:
            history_task_body = actor_name + " synced the assignees."
            history_scoped_body = actor_name + " synced the assignees for task " + _quoted_history_value(task.title, "Task") + "."
        log_task_history(task, actor=actor_user, action="updated", changed_fields=["assignment mode", "assignees"], task_body=history_task_body, scoped_body=history_scoped_body)
        db.session.commit()
        emit_task_updated(task, actor_user_id=actor_user_id)


def _sync_project_group_mode_tasks(project_id: int, *, actor_user_id: int | None = None) -> None:
    tasks = Task.query.filter_by(project_id=project_id, assign_group_members=True).all()
    if not tasks:
        return
    _sync_assign_group_member_tasks(tasks, actor_user_id=actor_user_id)


def _sync_project_follow_mode_tasks(project_id: int, *, actor_user_id: int | None = None) -> None:
    desired_user_ids = set(_project_access_user_ids(project_id))
    tasks = [task for task in Task.query.filter_by(project_id=project_id).all() if _task_follow_project_members(task)]
    if not tasks:
        return
    for task in tasks:
        existing_rows = TaskFollower.query.filter_by(task_id=task.id).all()
        existing_by_user_id = {int(row.user_id): row for row in existing_rows if row.user_id}
        changed = False
        for row in existing_rows:
            if row.user_id and int(row.user_id) not in desired_user_ids:
                db.session.delete(row)
                changed = True
        for follower_user_id in desired_user_ids:
            if follower_user_id in existing_by_user_id:
                continue
            db.session.add(TaskFollower(task_id=task.id, user_id=follower_user_id))
            changed = True
        if changed:
            db.session.flush()
    db.session.commit()
    for task in tasks:
        actor_user = User.query.get(actor_user_id) if actor_user_id else None
        history_task_body, history_scoped_body = _task_update_history_bodies(
            actor_name=display_name_for_user(actor_user) or (actor_user.email if actor_user else "") or "Someone",
            task_title=task.title,
            changed_fields=["follower mode", "followers"],
            new_title=task.title,
            new_due_mode=_task_due_mode(task),
            new_due_at=task.due_at,
            new_start_date=_task_start_date(task),
            new_status=task.status,
            new_status_mode=_task_status_mode(task),
            new_status_percentage=_task_status_percentage(task),
            followers=[_serialize_follower_row(row) for row in TaskFollower.query.filter_by(task_id=task.id).all()],
        )
        log_task_history(task, actor=actor_user, action="updated", changed_fields=["follower mode", "followers"], task_body=history_task_body, scoped_body=history_scoped_body)
        db.session.commit()
        emit_task_updated(task, actor_user_id=actor_user_id)


@api_bp.post("/assignments/<int:assignment_id>/send_link")
@login_required
def send_assignment_link(assignment_id: int):
    user = current_user()
    assignment = Assignment.query.get(assignment_id)
    if not assignment:
        return {"error": "assignment not found"}, 404

    task = Task.query.get(assignment.task_id) if assignment.task_id else None
    if not task:
        return {"error": "task not found"}, 404

    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403

    email = assignment.email or (User.query.get(assignment.user_id).email if assignment.user_id else None)
    if not email:
        return {"error": "assignment has no email"}, 400

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
            project_name=project.name if project else None,
            task_title=task.title if task else None,
            inviter_email=user.email,
            inviter_name=user.display_name or user.email,
        )
    except MailDeliveryError as exc:
        db.session.rollback()
        return {"error": f"email delivery failed: {exc}"}, 503

    invite.status = "sent"
    assignment.status = "link_sent"
    db.session.commit()
    emit_assignment_updated(task, assignment, actor_user_id=user.id)

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
                "invite_url": manage_url,
                "portal_url": manage_url,
                "accept_url": f"{base_url}/invites/{invite.token}/quick/accept",
                "decline_url": f"{base_url}/invites/{invite.token}/quick/decline",
                "manage_url": manage_url,
            }
        )
        prepared_assignments.append((assignment, invite))

    try:
        send_magic_link_digest_email(
            to_email=first_email,
            manage_url=prepared_items[0].get("manage_url") if prepared_items else None,
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
    for assignment, _invite in prepared_assignments:
        task = Task.query.get(assignment.task_id) if assignment.task_id else None
        if task:
            emit_assignment_updated(task, assignment, actor_user_id=user.id)
    return {"status": "link_sent", "assignment_ids": [assignment.id for assignment, _ in prepared_assignments]}, 200


@api_bp.delete("/assignments/<int:assignment_id>")
@login_required
def delete_assignment(assignment_id: int):
    user = current_user()
    assignment = Assignment.query.get(assignment_id)
    if not assignment:
        return {"error": "assignment not found"}, 404

    task = Task.query.get(assignment.task_id) if assignment.task_id else None
    if not task:
        return {"error": "task not found"}, 404

    if not _can_access_task(user, task):
        return {"error": "unauthorized"}, 403
    if _task_is_locked(task):
        return _locked_task_response()

    Invite.query.filter_by(assignment_id=assignment.id).delete()
    removed_assignment = _serialize_assignment_row(assignment)
    db.session.delete(assignment)
    db.session.commit()
    actor_name = display_name_for_user(user) or user.email or "Someone"
    assignee_label = _assignment_history_label(removed_assignment)
    history_task_body = actor_name + " removed assignee " + assignee_label + "."
    history_scoped_body = actor_name + " removed assignee " + assignee_label + " from task " + _quoted_history_value(task.title, "Task") + "."
    log_task_history(task, actor=user, action="updated", changed_fields=["assignees"], task_body=history_task_body, scoped_body=history_scoped_body)
    db.session.commit()
    emit_assignment_updated(task, assignment, action="deleted", actor_user_id=user.id)
    return {"status": "deleted"}, 200


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


@api_bp.get("/uploads/<item_type>/<int:item_id>/<filename>")
@login_required
def serve_upload(item_type: str, item_id: int, filename: str):
    item = None
    user = current_user()
    if item_type == "task":
        item = Task.query.get(item_id)
        if not item or not _can_access_task(user, item):
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
    limit = 100

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
    task_ids = list(set(owned_task_ids + assigned_task_ids))

    # Users by email/display_name
    if q:
        users = search_users_by_identity(q, limit=limit)
    else:
        users = (
            User.query.filter(User.id != user.id)
            .order_by(User.display_name.asc(), User.email.asc())
            .limit(limit)
            .all()
        )

    # Prior emails from assignments/invites within scope
    emails = set()
    if task_ids:
        for row in Assignment.query.filter(Assignment.task_id.in_(task_ids)).all():
            if row.email:
                emails.add(row.email)
    if task_ids:
        invite_rows = Invite.query.filter(Invite.task_id.in_(task_ids)).all()
        for row in invite_rows:
            if row.email:
                emails.add(row.email)

    filtered_emails = [e for e in emails if not q or q in e.lower()]
    filtered_emails.sort()

    # Count accepts per email
    accept_counts = {}
    if task_ids:
        invite_rows = Invite.query.filter(
            Invite.task_id.in_(task_ids),
            Invite.status == "accepted",
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
                "avatar_url": u.avatar_url,
                "accepted_count": accept_counts.get(u.email, 0),
            }
        )
    for e in filtered_emails[:limit]:
        results.append(
            {
                "type": "email",
                "email": e,
                "accepted_count": accept_counts.get(e, 0),
            }
        )

    return {"results": results}
