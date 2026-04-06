from __future__ import annotations
from datetime import UTC
import re

from app.extensions import db
from app.models import DiscussionEvent, Group, Project, Task, User
from app.utils import display_name_for_user


def _utc_iso(value) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.isoformat().replace("+00:00", "Z")


def log_discussion_event(
    entity_type: str,
    entity_id: int,
    *,
    task_id: int | None = None,
    project_id: int | None = None,
    group_id: int | None = None,
    actor_user_id: int | None = None,
    kind: str = "updated",
    body: str,
) -> DiscussionEvent:
    event = DiscussionEvent(
        entity_type=entity_type,
        entity_id=entity_id,
        task_id=task_id,
        project_id=project_id,
        group_id=group_id,
        actor_user_id=actor_user_id,
        kind=kind,
        body=body,
    )
    db.session.add(event)
    return event


def serialize_discussion_event(event: DiscussionEvent, actor: User | None = None) -> dict:
    actor_name = ""
    actor_avatar = ""
    actor_email = ""
    if actor:
        actor_name = display_name_for_user(actor) or actor.email or ""
        actor_avatar = actor.avatar_url or ""
        actor_email = actor.email or ""
    return {
        "id": f"history-{event.id}",
        "history_id": event.id,
        "entry_type": "history",
        "system": True,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "kind": event.kind,
        "body": event.body or "",
        "created_at": _utc_iso(event.created_at),
        "updated_at": _utc_iso(event.created_at),
        "user_id": None,
        "actor_user_id": event.actor_user_id,
        "author": {
            "display_name": actor_name or "History",
            "email": actor_email or "",
            "avatar_url": actor_avatar or "",
        },
        "_created_at_dt": event.created_at,
    }


def serialized_discussion_events_for_entity(entity_type: str, entity_id: int) -> list[dict]:
    events = (
        DiscussionEvent.query.filter_by(entity_type=entity_type, entity_id=entity_id)
        .order_by(DiscussionEvent.created_at.asc(), DiscussionEvent.id.asc())
        .all()
    )
    actor_ids = sorted({int(event.actor_user_id) for event in events if event.actor_user_id})
    actors = {row.id: row for row in User.query.filter(User.id.in_(actor_ids)).all()} if actor_ids else {}
    entries = [serialize_discussion_event(event, actors.get(event.actor_user_id)) for event in events]
    for entry in entries:
        entry.pop("_created_at_dt", None)
    return entries


def _actor_name(actor: User | None) -> str:
    if not actor:
        return "Someone"
    return display_name_for_user(actor) or actor.email or "Someone"


def _quoted(value: str | None, fallback: str) -> str:
    label = (value or "").strip() or fallback
    return f'"{label}"'


def _field_list_text(changed_fields: list[str] | None) -> str:
    labels = [str(value).strip() for value in (changed_fields or []) if str(value).strip()]
    if not labels:
        return ""
    return " (" + ", ".join(labels) + ")"


def _task_destination_label(task: Task) -> str:
    project_name = getattr(getattr(task, "project", None), "name", None) or "Project"
    if getattr(task, "group_id", None) and getattr(task, "group", None) and getattr(task.group, "name", None):
        return f'to group {_quoted(task.group.name, "Group")} in project {_quoted(project_name, "Project")}'
    return f'to project {_quoted(project_name, "Project")}'


def log_task_history(
    task: Task,
    *,
    actor: User | None = None,
    action: str = "updated",
    changed_fields: list[str] | None = None,
    old_project_id: int | None = None,
    old_group_id: int | None = None,
    title: str | None = None,
    task_body: str | None = None,
    scoped_body: str | None = None,
) -> None:
    normalized_changed_fields = [str(value).strip() for value in (changed_fields or []) if str(value).strip()]
    if action == "updated" and not normalized_changed_fields:
        return
    actor_name = _actor_name(actor)
    task_label = _quoted(title if title is not None else getattr(task, "title", None), "Task")
    task_ref_id = None if action == "deleted" else task.id
    project_ref_id = None if action == "deleted" else task.project_id
    group_ref_id = None if action == "deleted" else task.group_id
    if action == "created":
        body = task_body or f"{actor_name} created task."
        related_body = scoped_body or f"{actor_name} created task {task_label}."
    elif action == "moved":
        body = task_body or f"{actor_name} moved the task {_task_destination_label(task)}."
        related_body = scoped_body or f"{actor_name} moved task {task_label} {_task_destination_label(task)}."
    elif action == "deleted":
        body = task_body or f"{actor_name} deleted the task."
        related_body = scoped_body or f"{actor_name} deleted task {task_label}."
    else:
        body = task_body or f"{actor_name} updated the task{_field_list_text(normalized_changed_fields)}."
        related_body = scoped_body or f"{actor_name} updated task {task_label}{_field_list_text(normalized_changed_fields)}."

    log_discussion_event(
        "task",
        task.id,
        task_id=task_ref_id,
        project_id=project_ref_id,
        group_id=group_ref_id,
        actor_user_id=actor.id if actor else None,
        kind=action,
        body=body,
    )
    if task.group_id:
        log_discussion_event(
            "group",
            task.group_id,
            task_id=task_ref_id,
            project_id=project_ref_id,
            group_id=group_ref_id,
            actor_user_id=actor.id if actor else None,
            kind=action,
            body=related_body,
        )
    if old_group_id and old_group_id != task.group_id:
        log_discussion_event(
            "group",
            old_group_id,
            task_id=task.id,
            project_id=old_project_id or task.project_id,
            group_id=old_group_id,
            actor_user_id=actor.id if actor else None,
            kind=action,
            body=related_body,
        )
    log_discussion_event(
        "project",
        task.project_id,
        task_id=task_ref_id,
        project_id=project_ref_id,
        group_id=group_ref_id,
        actor_user_id=actor.id if actor else None,
        kind=action,
        body=related_body,
    )
    if old_project_id and old_project_id != task.project_id:
        log_discussion_event(
            "project",
            old_project_id,
            task_id=task.id,
            project_id=old_project_id,
            group_id=old_group_id,
            actor_user_id=actor.id if actor else None,
            kind=action,
            body=related_body,
        )


def log_project_history(
    project: Project,
    *,
    actor: User | None = None,
    action: str = "updated",
    changed_fields: list[str] | None = None,
    title: str | None = None,
) -> None:
    normalized_changed_fields = [str(value).strip() for value in (changed_fields or []) if str(value).strip()]
    if action == "updated" and not normalized_changed_fields:
        return
    actor_name = _actor_name(actor)
    project_label = _quoted(title if title is not None else getattr(project, "name", None), "Project")
    project_ref_id = None if action == "deleted" else project.id
    if action == "created":
        body = f"{actor_name} created project {project_label}."
    elif action == "deleted":
        body = f"{actor_name} deleted project {project_label}."
    else:
        body = f"{actor_name} updated project {project_label}{_field_list_text(normalized_changed_fields)}."
    log_discussion_event(
        "project",
        project.id,
        project_id=project_ref_id,
        actor_user_id=actor.id if actor else None,
        kind=action,
        body=body,
    )


def log_group_history(
    group: Group,
    *,
    actor: User | None = None,
    action: str = "updated",
    changed_fields: list[str] | None = None,
    title: str | None = None,
) -> None:
    normalized_changed_fields = [str(value).strip() for value in (changed_fields or []) if str(value).strip()]
    if action == "updated" and not normalized_changed_fields:
        return
    actor_name = _actor_name(actor)
    group_label = _quoted(title if title is not None else getattr(group, "name", None), "Group")
    group_ref_id = None if action == "deleted" else group.id
    if action == "created":
        body = f"{actor_name} created group {group_label}."
    elif action == "deleted":
        body = f"{actor_name} deleted group {group_label}."
    else:
        body = f"{actor_name} updated group {group_label}{_field_list_text(normalized_changed_fields)}."
    log_discussion_event(
        "group",
        group.id,
        project_id=group.project_id,
        group_id=group_ref_id,
        actor_user_id=actor.id if actor else None,
        kind=action,
        body=body,
    )
    log_discussion_event(
        "project",
        group.project_id,
        project_id=group.project_id,
        group_id=group_ref_id,
        actor_user_id=actor.id if actor else None,
        kind=action,
        body=body,
    )
