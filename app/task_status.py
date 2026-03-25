from collections import Counter

from app.extensions import db
from app.models import Assignment, Task, TaskUserStatus, User


COMPLETE_STATUS_VALUES = {"complete", "completed", "done", "closed", "pr closed", "pr merged"}


def normalize_task_status(value: str | None, *, default: str = "open") -> str:
    text = (value or "").strip()
    return text or default


def task_status_state(value: str | None) -> str:
    normalized = normalize_task_status(value).lower()
    if normalized == "critical":
        return "critical"
    if normalized in COMPLETE_STATUS_VALUES:
        return "complete"
    return "open"


def is_complete_task_status(value: str | None) -> bool:
    return task_status_state(value) == "complete"


def load_task_user_status_rows(task_ids: list[int]) -> dict[int, list[TaskUserStatus]]:
    rows_by_task: dict[int, list[TaskUserStatus]] = {}
    if not task_ids:
        return rows_by_task
    rows = (
        TaskUserStatus.query.filter(TaskUserStatus.task_id.in_(task_ids))
        .order_by(TaskUserStatus.updated_at.asc(), TaskUserStatus.id.asc())
        .all()
    )
    for row in rows:
        rows_by_task.setdefault(row.task_id, []).append(row)
    return rows_by_task


def summarize_status_values(values: list[str]) -> list[dict]:
    counts = Counter(normalize_task_status(value) for value in values if normalize_task_status(value))

    def sort_key(item: tuple[str, int]) -> tuple[int, str]:
        status = item[0].lower()
        if status == "open":
            return (0, status)
        if status == "critical":
            return (1, status)
        if status in COMPLETE_STATUS_VALUES:
            return (2, status)
        return (3, status)

    return [
        {
            "status": status,
            "count": count,
            "state": task_status_state(status),
        }
        for status, count in sorted(counts.items(), key=sort_key)
    ]


def aggregate_status_state(values: list[str]) -> str:
    normalized = [normalize_task_status(value) for value in values if normalize_task_status(value)]
    if not normalized:
        return "open"
    if all(task_status_state(value) == "complete" for value in normalized):
        return "complete"
    if any(task_status_state(value) == "critical" for value in normalized):
        return "critical"
    return "open"


def task_status_meta(task: Task, *, viewer_user_id: int | None = None, rows_by_task: dict[int, list[TaskUserStatus]] | None = None) -> dict:
    if rows_by_task is None:
        rows_by_task = load_task_user_status_rows([task.id] if task and task.id else [])
    rows = list((rows_by_task or {}).get(task.id, []))
    assignments = (
        Assignment.query.filter_by(task_id=task.id)
        .order_by(Assignment.created_at.asc(), Assignment.id.asc())
        .all()
        if task and task.id
        else []
    )
    row_by_user_id = {
        int(row.user_id): row
        for row in rows
        if row.user_id is not None
    }
    statuses = []
    assignees = []
    seen_assignment_keys: set[str] = set()
    for assignment in assignments:
        assignment_key = f"user:{assignment.user_id}" if assignment.user_id else f"email:{normalize_task_status(assignment.email, default='').lower()}"
        if not assignment_key or assignment_key in seen_assignment_keys:
            continue
        seen_assignment_keys.add(assignment_key)
        assignment_row = row_by_user_id.get(int(assignment.user_id)) if assignment.user_id is not None else None
        effective_status = normalize_task_status(assignment_row.status if assignment_row else "open")
        account_user = User.query.get(assignment.user_id) if assignment.user_id is not None else None
        display_email = (account_user.email if account_user and account_user.email else assignment.email) or ""
        display_name = (account_user.display_name if account_user and account_user.display_name else display_email) or "User"
        statuses.append(
            {
                "user_id": assignment.user_id,
                "status": effective_status,
                "state": task_status_state(effective_status),
            }
        )
        assignees.append(
            {
                "user_id": assignment.user_id,
                "email": display_email,
                "display_name": display_name,
                "avatar_url": account_user.avatar_url if account_user and account_user.avatar_url else None,
                "status": effective_status,
                "state": task_status_state(effective_status),
                "editable": assignment.user_id is not None,
            }
        )
    viewer_status = None
    viewer_is_assignee = False
    if viewer_user_id is not None:
        viewer_status = next(
            (row["status"] for row in statuses if row["user_id"] is not None and int(row["user_id"]) == int(viewer_user_id)),
            None,
        )
        viewer_is_assignee = any(row["user_id"] is not None and int(row["user_id"]) == int(viewer_user_id) for row in statuses)
    enabled = bool(getattr(task, "per_user_status_enabled", False))
    base_status = normalize_task_status(task.status)
    my_status = viewer_status if (enabled and viewer_is_assignee) else (base_status if not enabled else None)
    assignment_backed_statuses = [assignee["status"] for assignee in assignees]
    summary = summarize_status_values(assignment_backed_statuses) if enabled else []
    return {
        "enabled": enabled,
        "task_status": base_status,
        "task_status_state": task_status_state(base_status),
        "aggregate_state": aggregate_status_state(assignment_backed_statuses) if enabled else task_status_state(base_status),
        "my_status": normalize_task_status(my_status) if (viewer_user_id is not None and my_status is not None) else None,
        "my_status_state": task_status_state(my_status) if (viewer_user_id is not None and my_status is not None) else None,
        "viewer_can_set": (not enabled) or viewer_is_assignee,
        "summary": summary,
        "user_statuses": statuses,
        "assignees": assignees,
    }


def task_status_meta_map(tasks: list[Task], *, viewer_user_id: int | None = None) -> dict[int, dict]:
    rows_by_task = load_task_user_status_rows([task.id for task in tasks if task and task.id])
    return {
        task.id: task_status_meta(task, viewer_user_id=viewer_user_id, rows_by_task=rows_by_task)
        for task in tasks
        if task and task.id
    }


def effective_task_status_for_user(task: Task, *, viewer_user_id: int | None = None, status_meta: dict | None = None) -> str:
    meta = status_meta or task_status_meta(task, viewer_user_id=viewer_user_id)
    if meta.get("enabled"):
        return normalize_task_status(meta.get("my_status"))
    return normalize_task_status(task.status)


def set_task_user_status(task_id: int, user_id: int, status: str) -> TaskUserStatus:
    normalized = normalize_task_status(status)
    row = TaskUserStatus.query.filter_by(task_id=task_id, user_id=user_id).first()
    if row:
        row.status = normalized
    else:
        row = TaskUserStatus(task_id=task_id, user_id=user_id, status=normalized)
        db.session.add(row)
    return row
