"""Shared volunteer and assignment-confirmation state for HTTP and socket payloads."""

from datetime import datetime

from app.info_utils import load_info_payload
from app.models import Assignment, Invite, Task


def task_assignee_mode(task):
    info = load_info_payload(task.info, task.link)
    mode = str((info.get("meta") or {}).get("assignee_mode") or "default")
    return mode if mode in {"default", "none", "volunteer", "confirm"} else "default"


def volunteer_payload(task):
    rows = Assignment.query.filter_by(task_id=task.id).all() if task.id and task_assignee_mode(task) in {"volunteer", "confirm"} else []
    accepted = [row.id for row in rows if row.volunteered_at]
    return {
        "volunteers_required": task.volunteers_required,
        "volunteer": {
            "required": task.volunteers_required if task_assignee_mode(task) == "volunteer" else None,
            "invitee_count": len(rows),
            "accepted_count": len(accepted),
            "accepted_assignment_ids": accepted,
        },
    }


def lock_volunteer_task(task_id):
    # SQLite ignores SELECT FOR UPDATE; a no-op write serializes the claim.
    Task.query.filter_by(id=task_id).update({"updated_at": Task.updated_at}, synchronize_session=False)
    return Task.query.filter_by(id=task_id).populate_existing().first()


def confirm_volunteer(task, assignment):
    """Call after lock_volunteer_task; repeat confirmations are idempotent."""
    from app.task_status import task_status_meta

    if task.locked:
        return False, "this task is locked"
    if task_assignee_mode(task) not in {"volunteer", "confirm"}:
        return False, "this task is not requesting assignment responses"
    if task_status_meta(task).get("aggregate_state") == "complete":
        return False, "this task is complete"
    if assignment.volunteered_at:
        return False, None
    accepted = Assignment.query.filter_by(task_id=task.id).filter(Assignment.volunteered_at.isnot(None)).count()
    if task_assignee_mode(task) == "volunteer" and task.volunteers_required is not None and accepted >= task.volunteers_required:
        return False, "all volunteer places have been filled"
    assignment.volunteered_at = datetime.utcnow()
    task.updated_at = datetime.utcnow()
    return True, None


def merge_assignment_response(primary, duplicate):
    """Preserve a person's response and working invite links when deduplicating."""
    if duplicate.volunteered_at and (
        not primary.volunteered_at or duplicate.volunteered_at < primary.volunteered_at
    ):
        primary.volunteered_at = duplicate.volunteered_at
    if duplicate.status == "accepted":
        primary.status = "accepted"
    for invite in Invite.query.filter_by(assignment_id=duplicate.id).all():
        invite.assignment_id = primary.id
