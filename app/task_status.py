from collections import Counter

from app.extensions import db
from app.info_utils import load_info_payload
from app.models import Assignment, Task, TaskCollaboratorStatus, TaskPrerequisite, TaskUserStatus, User


COMPLETE_STATUS_VALUES = {"complete", "completed", "done", "closed", "pr closed", "pr merged"}
TASK_STATUS_MODES = {"single", "multi", "percent", "poll"}


def normalize_task_status_mode(value: str | None, *, default: str = "single") -> str:
    text = str(value or "").strip().lower()
    if text in {"per_user", "multi"}:
        return "multi"
    if text in {"percentage", "percent"}:
        return "percent"
    if text == "poll":
        return "poll"
    if text == "single":
        return "single"
    return default


def _task_type(task: Task | None) -> str:
    if not task:
        return "standard"
    info_payload = load_info_payload(getattr(task, "info", None), getattr(task, "link", None))
    meta = info_payload.get("meta") or {}
    task_type = str(meta.get("task_type") or "").strip().lower()
    return "poll" if task_type == "poll" else "standard"


def _task_poll_payload(task: Task | None) -> dict:
    if not task:
        return {"question": "", "allows_multiple": False, "results_visibility": "everyone", "options": [], "responses": []}
    info_payload = load_info_payload(getattr(task, "info", None), getattr(task, "link", None))
    meta = info_payload.get("meta") or {}
    poll = meta.get("poll") if isinstance(meta.get("poll"), dict) else {}
    question = str(poll.get("question") or "").strip()
    allows_multiple = bool(poll.get("allows_multiple"))
    results_visibility = str(poll.get("results_visibility") or "everyone").strip().lower()
    if results_visibility not in {"everyone", "creator"}:
        results_visibility = "everyone"
    options = []
    seen_option_ids: set[str] = set()
    for item in poll.get("options") or []:
        if isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            option_id = str(item.get("id") or "").strip()
        else:
            label = str(item or "").strip()
            option_id = ""
        if not label:
            continue
        if not option_id or option_id in seen_option_ids:
            continue
        seen_option_ids.add(option_id)
        options.append({"id": option_id, "label": label})
    valid_option_ids = set(seen_option_ids)
    responses = []
    seen_response_keys: set[str] = set()
    for item in poll.get("responses") or []:
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
        option_ids = []
        for option_id in item.get("option_ids") or []:
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
                "updated_at": str(item.get("updated_at") or "").strip(),
            }
        )
    return {
        "question": question,
        "allows_multiple": allows_multiple,
        "closed": bool(poll.get("closed")),
        "results_visibility": results_visibility,
        "options": options,
        "responses": responses,
    }


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


def load_task_collaborator_status_rows(task_ids: list[int]) -> dict[int, list[TaskCollaboratorStatus]]:
    rows_by_task: dict[int, list[TaskCollaboratorStatus]] = {}
    if not task_ids:
        return rows_by_task
    rows = (
        TaskCollaboratorStatus.query.filter(TaskCollaboratorStatus.task_id.in_(task_ids))
        .order_by(TaskCollaboratorStatus.updated_at.asc(), TaskCollaboratorStatus.id.asc())
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


def load_task_prerequisite_ids(task_ids: list[int]) -> dict[int, list[int]]:
    rows_by_task: dict[int, list[int]] = {}
    if not task_ids:
        return rows_by_task
    rows = (
        db.session.query(TaskPrerequisite)
        .join(Task, Task.id == TaskPrerequisite.prerequisite_task_id)
        .filter(TaskPrerequisite.task_id.in_(task_ids))
        .order_by(TaskPrerequisite.created_at.asc(), TaskPrerequisite.id.asc())
        .all()
    )
    for row in rows:
        rows_by_task.setdefault(int(row.task_id), []).append(int(row.prerequisite_task_id))
    return rows_by_task


def task_status_mode(task: Task | None) -> str:
    if not task:
        return "single"
    raw_model_mode = normalize_task_status_mode(getattr(task, "status_mode", None), default="")
    if raw_model_mode in TASK_STATUS_MODES:
        return raw_model_mode
    info_payload = load_info_payload(getattr(task, "info", None), getattr(task, "link", None))
    meta = info_payload.get("meta") or {}
    raw_mode = normalize_task_status_mode(meta.get("status_mode"), default="")
    if raw_mode in TASK_STATUS_MODES:
        return raw_mode
    if bool(getattr(task, "per_user_status_enabled", False)):
        return "multi"
    return "single"


def task_status_percentage(task: Task | None) -> int:
    if not task:
        return 0
    info_payload = load_info_payload(getattr(task, "info", None), getattr(task, "link", None))
    raw = str((info_payload.get("meta") or {}).get("status_percentage") or "").strip()
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = 100 if task_status_state(getattr(task, "status", None)) == "complete" else 0
    return max(0, min(100, value))


def _base_task_status_meta(
    task: Task,
    *,
    viewer_user_id: int | None = None,
    viewer_email: str | None = None,
    rows_by_task: dict[int, list[TaskUserStatus]] | None = None,
    collaborator_rows_by_task: dict[int, list[TaskCollaboratorStatus]] | None = None,
) -> dict:
    if rows_by_task is None:
        rows_by_task = load_task_user_status_rows([task.id] if task and task.id else [])
    if collaborator_rows_by_task is None:
        collaborator_rows_by_task = load_task_collaborator_status_rows([task.id] if task and task.id else [])
    rows = list((rows_by_task or {}).get(task.id, []))
    collaborator_rows = list((collaborator_rows_by_task or {}).get(task.id, []))
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
    row_by_email = {
        (row.email or "").strip().lower(): row
        for row in collaborator_rows
        if (row.email or "").strip()
    }
    statuses = []
    assignees = []
    seen_assignment_keys: set[str] = set()
    for assignment in assignments:
        assignment_key = f"user:{assignment.user_id}" if assignment.user_id else f"email:{normalize_task_status(assignment.email, default='').lower()}"
        if not assignment_key or assignment_key in seen_assignment_keys:
            continue
        seen_assignment_keys.add(assignment_key)
        normalized_email = (assignment.email or "").strip().lower()
        assignment_row = row_by_user_id.get(int(assignment.user_id)) if assignment.user_id is not None else row_by_email.get(normalized_email)
        effective_status = normalize_task_status(assignment_row.status if assignment_row else "open")
        account_user = User.query.get(assignment.user_id) if assignment.user_id is not None else None
        display_email = (account_user.email if account_user and account_user.email else assignment.email) or ""
        display_name = (account_user.display_name if account_user and account_user.display_name else display_email) or "User"
        statuses.append(
            {
                "user_id": assignment.user_id,
                "email": normalized_email or display_email.lower(),
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
                "editable": True,
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
    elif viewer_email:
        normalized_viewer_email = viewer_email.strip().lower()
        viewer_status = next((row["status"] for row in statuses if (row.get("email") or "").strip().lower() == normalized_viewer_email), None)
        viewer_is_assignee = any((row.get("email") or "").strip().lower() == normalized_viewer_email for row in statuses)
    if _task_type(task) == "poll":
        mode = "poll"
    else:
        mode = task_status_mode(task)
    enabled = mode != "single"
    base_status = normalize_task_status(task.status)
    my_status = viewer_status if (enabled and viewer_is_assignee) else (base_status if not enabled else None)
    assignment_backed_statuses = [assignee["status"] for assignee in assignees]
    summary = summarize_status_values(assignment_backed_statuses) if enabled else []
    complete_count = sum(1 for value in assignment_backed_statuses if task_status_state(value) == "complete")
    percentage_complete = int(round((complete_count / len(assignment_backed_statuses)) * 100)) if assignment_backed_statuses else 0
    if mode == "percent":
        percentage_complete = task_status_percentage(task)
    poll_payload = _task_poll_payload(task) if mode == "poll" else {"question": "", "allows_multiple": False, "options": [], "responses": []}
    poll_response_by_identity: dict[str, dict] = {}
    if mode == "poll":
        for response in list(poll_payload.get("responses") or []):
            response_user_id = response.get("user_id")
            response_email = (response.get("email") or "").strip().lower()
            if response_user_id is not None:
                poll_response_by_identity[f"user:{int(response_user_id)}"] = response
            if response_email:
                poll_response_by_identity[f"email:{response_email}"] = response
        poll_total_count = len(assignees)
        poll_complete_count = 0
        poll_viewer_option_ids: list[str] = []
        poll_viewer_has_response = False
        can_view_results = str(poll_payload.get("results_visibility") or "everyone") == "everyone" or (
            viewer_user_id is not None and task.creator_user_id is not None and int(viewer_user_id) == int(task.creator_user_id)
        )
        assignee_display_by_key: dict[str, dict] = {}
        for assignee in assignees:
            if assignee.get("user_id") is not None:
                assignee_display_by_key[f"user:{int(assignee['user_id'])}"] = assignee
            if (assignee.get("email") or "").strip():
                assignee_display_by_key[f"email:{str(assignee.get('email') or '').strip().lower()}"] = assignee
        poll_option_results: list[dict] = []
        for option in list(poll_payload.get("options") or []):
            option_id = str(option.get("id") or "").strip()
            responders = []
            if can_view_results and option_id:
                for response in list(poll_payload.get("responses") or []):
                    selected_option_ids = [str(item or "").strip() for item in list(response.get("option_ids") or [])]
                    if option_id not in selected_option_ids:
                        continue
                    response_key = (
                        f"user:{int(response['user_id'])}"
                        if response.get("user_id") is not None
                        else (f"email:{str(response.get('email') or '').strip().lower()}" if str(response.get("email") or "").strip() else "")
                    )
                    display = assignee_display_by_key.get(response_key, {})
                    responders.append(
                        {
                            "user_id": response.get("user_id"),
                            "email": str(response.get("email") or "").strip().lower(),
                            "display_name": str(display.get("display_name") or display.get("email") or "User"),
                            "avatar_url": display.get("avatar_url"),
                        }
                    )
            poll_option_results.append(
                {
                    "id": option_id,
                    "label": str(option.get("label") or "").strip(),
                    "responders": responders,
                }
            )
        for assignee in assignees:
            assignee_key = ""
            if assignee.get("user_id") is not None:
                assignee_key = f"user:{int(assignee['user_id'])}"
            elif (assignee.get("email") or "").strip():
                assignee_key = f"email:{str(assignee.get('email') or '').strip().lower()}"
            response = poll_response_by_identity.get(assignee_key) if assignee_key else None
            option_ids = list(response.get("option_ids") or []) if response else []
            responded = bool(option_ids)
            assignee["status"] = "complete" if responded else "open"
            assignee["state"] = "complete" if responded else "open"
            if responded:
                poll_complete_count += 1
            if viewer_user_id is not None and assignee.get("user_id") is not None and int(assignee["user_id"]) == int(viewer_user_id):
                poll_viewer_option_ids = option_ids
                poll_viewer_has_response = responded
            elif viewer_user_id is None and viewer_email and (assignee.get("email") or "").strip().lower() == viewer_email.strip().lower():
                poll_viewer_option_ids = option_ids
                poll_viewer_has_response = responded
        percentage_complete = int(round((poll_complete_count / poll_total_count) * 100)) if poll_total_count else 0
        has_viewer_identity = (viewer_user_id is not None) or bool((viewer_email or "").strip())
        viewer_is_assignee = any(
            (assignee.get("user_id") is not None and viewer_user_id is not None and int(assignee["user_id"]) == int(viewer_user_id))
            or ((assignee.get("email") or "").strip().lower() == (viewer_email or "").strip().lower() and (viewer_email or "").strip())
            for assignee in assignees
        )
        return {
            "mode": "poll",
            "enabled": True,
            "prereq_blocked": False,
            "prereq_total_count": 0,
            "prereq_met_count": 0,
            "task_status": "complete" if (poll_total_count > 0 and poll_complete_count == poll_total_count) else "open",
            "task_status_state": "complete" if (poll_total_count > 0 and poll_complete_count == poll_total_count) else "open",
            "aggregate_state": "complete" if (poll_total_count > 0 and poll_complete_count == poll_total_count) else "open",
            "my_status": ("complete" if poll_viewer_has_response else "open") if has_viewer_identity and viewer_is_assignee else None,
            "my_status_state": ("complete" if poll_viewer_has_response else "open") if has_viewer_identity and viewer_is_assignee else None,
            "viewer_can_set": viewer_is_assignee,
            "percentage_complete": percentage_complete,
            "summary": [],
            "user_statuses": [],
            "assignees": assignees,
            "poll_question": str(poll_payload.get("question") or "").strip(),
            "poll_allows_multiple": bool(poll_payload.get("allows_multiple")),
            "poll_closed": bool(poll_payload.get("closed")),
            "poll_results_visibility": str(poll_payload.get("results_visibility") or "everyone"),
            "poll_results_visible": can_view_results,
            "poll_option_results": poll_option_results,
            "poll_total_count": poll_total_count,
            "poll_complete_count": poll_complete_count,
            "poll_viewer_option_ids": poll_viewer_option_ids,
        }
    has_viewer_identity = (viewer_user_id is not None) or bool((viewer_email or "").strip())
    return {
        "mode": mode,
        "enabled": enabled,
        "prereq_blocked": False,
        "prereq_total_count": 0,
        "prereq_met_count": 0,
        "task_status": base_status,
        "task_status_state": task_status_state(base_status),
        "aggregate_state": ("complete" if percentage_complete >= 100 else "open") if mode == "percent" else (aggregate_status_state(assignment_backed_statuses) if enabled else task_status_state(base_status)),
        "my_status": normalize_task_status(my_status) if (has_viewer_identity and my_status is not None) else None,
        "my_status_state": task_status_state(my_status) if (has_viewer_identity and my_status is not None) else None,
        "viewer_can_set": (not enabled) or viewer_is_assignee,
        "percentage_complete": percentage_complete,
        "summary": summary,
        "user_statuses": statuses,
        "assignees": assignees,
    }


def task_status_meta_map(tasks: list[Task], *, viewer_user_id: int | None = None, viewer_email: str | None = None) -> dict[int, dict]:
    task_map: dict[int, Task] = {
        int(task.id): task
        for task in tasks
        if task and task.id
    }
    pending_ids = list(task_map.keys())
    seen_ids = set(pending_ids)
    prerequisite_ids_by_task: dict[int, list[int]] = {}
    while pending_ids:
        prerequisite_rows = load_task_prerequisite_ids(pending_ids)
        prerequisite_ids_by_task.update(prerequisite_rows)
        next_ids = sorted(
            {
                int(prerequisite_id)
                for prerequisite_ids in prerequisite_rows.values()
                for prerequisite_id in prerequisite_ids
                if prerequisite_id and int(prerequisite_id) not in seen_ids
            }
        )
        if not next_ids:
            break
        for prerequisite_task in Task.query.filter(Task.id.in_(next_ids)).all():
            task_map[int(prerequisite_task.id)] = prerequisite_task
        seen_ids.update(next_ids)
        pending_ids = next_ids

    all_task_ids = list(task_map.keys())
    rows_by_task = load_task_user_status_rows(all_task_ids)
    collaborator_rows_by_task = load_task_collaborator_status_rows(all_task_ids)
    base_meta_map = {
        task_id: _base_task_status_meta(
            task,
            viewer_user_id=viewer_user_id,
            viewer_email=viewer_email,
            rows_by_task=rows_by_task,
            collaborator_rows_by_task=collaborator_rows_by_task,
        )
        for task_id, task in task_map.items()
    }
    decorated_meta_map: dict[int, dict] = {}

    def final_state_for_task(task_id: int) -> str:
        task = task_map.get(int(task_id))
        meta = base_meta_map.get(int(task_id)) or {}
        if not task:
            return "open"
        if int(task_id) in decorated_meta_map:
            return str(decorated_meta_map[int(task_id)].get("aggregate_state") or "open")
        mode = normalize_task_status_mode(meta.get("mode"), default="single")
        base_state = (
            str(meta.get("aggregate_state") or "open")
            if mode != "single"
            else str(meta.get("task_status_state") or task_status_state(task.status))
        )
        prerequisite_ids = prerequisite_ids_by_task.get(int(task_id), [])
        prereq_total_count = len(prerequisite_ids)
        prereq_met_count = sum(1 for prerequisite_id in prerequisite_ids if final_state_for_task(prerequisite_id) == "complete")
        blocked = prereq_met_count < prereq_total_count
        final_state = "prereq" if blocked else base_state
        decorated = dict(meta)
        decorated["prereq_blocked"] = blocked
        decorated["prereq_total_count"] = prereq_total_count
        decorated["prereq_met_count"] = prereq_met_count
        decorated["task_status_state"] = "prereq" if blocked else str(meta.get("task_status_state") or task_status_state(task.status))
        decorated["aggregate_state"] = final_state
        my_status_state = meta.get("my_status_state")
        decorated["my_status_state"] = "prereq" if blocked else (str(my_status_state) if my_status_state else None)
        decorated_meta_map[int(task_id)] = decorated
        return final_state

    for task_id in list(task_map.keys()):
        if task_id not in decorated_meta_map:
            final_state_for_task(task_id)

    return {
        int(task.id): decorated_meta_map.get(int(task.id), base_meta_map.get(int(task.id), {}))
        for task in tasks
        if task and task.id
    }


def task_status_meta(
    task: Task,
    *,
    viewer_user_id: int | None = None,
    viewer_email: str | None = None,
    rows_by_task: dict[int, list[TaskUserStatus]] | None = None,
    collaborator_rows_by_task: dict[int, list[TaskCollaboratorStatus]] | None = None,
) -> dict:
    if not task or not task.id:
        return {}
    if rows_by_task is not None or collaborator_rows_by_task is not None:
        base_meta = _base_task_status_meta(
            task,
            viewer_user_id=viewer_user_id,
            viewer_email=viewer_email,
            rows_by_task=rows_by_task,
            collaborator_rows_by_task=collaborator_rows_by_task,
        )
        prerequisite_ids = load_task_prerequisite_ids([int(task.id)]).get(int(task.id), [])
        if not prerequisite_ids:
            return base_meta
        prerequisite_tasks = Task.query.filter(Task.id.in_(prerequisite_ids)).all() if prerequisite_ids else []
        prerequisite_meta_map = task_status_meta_map(
            prerequisite_tasks,
            viewer_user_id=viewer_user_id,
            viewer_email=viewer_email,
        )
        blocked = any(
            str((prerequisite_meta_map.get(int(prerequisite_id)) or {}).get("aggregate_state") or "open") != "complete"
            for prerequisite_id in prerequisite_ids
        )
        prereq_total_count = len(prerequisite_ids)
        prereq_met_count = sum(
            1
            for prerequisite_id in prerequisite_ids
            if str((prerequisite_meta_map.get(int(prerequisite_id)) or {}).get("aggregate_state") or "open") == "complete"
        )
        base_meta["prereq_total_count"] = prereq_total_count
        base_meta["prereq_met_count"] = prereq_met_count
        if not blocked:
            return base_meta
        decorated = dict(base_meta)
        decorated["prereq_blocked"] = True
        decorated["task_status_state"] = "prereq"
        decorated["aggregate_state"] = "prereq"
        decorated["my_status_state"] = "prereq" if decorated.get("my_status_state") else None
        return decorated
    return task_status_meta_map([task], viewer_user_id=viewer_user_id, viewer_email=viewer_email).get(int(task.id), {})


def effective_task_status_for_user(task: Task, *, viewer_user_id: int | None = None, viewer_email: str | None = None, status_meta: dict | None = None) -> str:
    meta = status_meta or task_status_meta(task, viewer_user_id=viewer_user_id, viewer_email=viewer_email)
    if normalize_task_status_mode(meta.get("mode"), default="single") == "poll":
        return "complete" if str(meta.get("my_status_state") or "open") == "complete" else "open"
    if normalize_task_status_mode(meta.get("mode"), default="single") == "percent":
        return "complete" if int(meta.get("percentage_complete") or 0) >= 100 else "open"
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


def set_task_collaborator_status(task_id: int, email: str, status: str) -> TaskCollaboratorStatus:
    normalized = normalize_task_status(status)
    normalized_email = (email or "").strip().lower()
    row = TaskCollaboratorStatus.query.filter_by(task_id=task_id, email=normalized_email).first()
    if row:
        row.status = normalized
    else:
        row = TaskCollaboratorStatus(task_id=task_id, email=normalized_email, status=normalized)
        db.session.add(row)
    return row
