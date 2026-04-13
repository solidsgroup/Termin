from __future__ import annotations

from datetime import datetime

from app.extensions import db
from app.models import GroupTemplate, GroupTemplateTask


def normalize_group_template_title(value: str | None) -> str:
    return str(value or "").strip()


def normalize_group_template_task_titles(raw_value: str | None) -> list[str]:
    lines = str(raw_value or "").splitlines()
    titles: list[str] = []
    for line in lines:
        title = str(line or "").strip()
        if title:
            titles.append(title[:255])
    return titles


def normalize_group_template_task_entries(form_data) -> list[dict]:
    titles = form_data.getlist("task_title") if form_data is not None else []
    descriptions = form_data.getlist("task_description") if form_data is not None else []
    entries: list[dict] = []
    if titles:
        for index, raw_title in enumerate(titles):
            title = str(raw_title or "").strip()[:255]
            description = str(descriptions[index] or "").strip() if index < len(descriptions) else ""
            if not title and not description:
                continue
            if not title:
                continue
            entries.append(
                {
                    "title": title,
                    "description": description or None,
                }
            )
        return entries
    legacy_titles = normalize_group_template_task_titles(form_data.get("tasks_text") if form_data is not None else None)
    return [{"title": title, "description": None} for title in legacy_titles]


def group_template_tasks(template_id: int) -> list[GroupTemplateTask]:
    return (
        GroupTemplateTask.query.filter_by(group_template_id=template_id)
        .order_by(GroupTemplateTask.position.asc(), GroupTemplateTask.id.asc())
        .all()
    )


def serialize_group_template(template: GroupTemplate) -> dict:
    tasks = group_template_tasks(template.id)
    task_titles = [task.title for task in tasks]
    return {
        "id": template.id,
        "user_id": template.user_id,
        "title": template.title,
        "tasks": [
            {
                "id": task.id,
                "title": task.title,
                "description": task.description or "",
                "position": task.position,
            }
            for task in tasks
        ],
        "task_titles": task_titles,
        "tasks_text": "\n".join(task_titles),
        "task_count": len(task_titles),
        "created_at": template.created_at.isoformat() if template.created_at else None,
        "updated_at": template.updated_at.isoformat() if template.updated_at else None,
    }


def replace_group_template_tasks(template: GroupTemplate, task_entries: list[dict]) -> None:
    GroupTemplateTask.query.filter_by(group_template_id=template.id).delete(synchronize_session=False)
    for index, entry in enumerate(task_entries, start=1):
        db.session.add(
            GroupTemplateTask(
                group_template_id=template.id,
                title=str(entry.get("title") or "").strip()[:255],
                description=str(entry.get("description") or "").strip() or None,
                position=index,
            )
        )
    template.updated_at = datetime.utcnow()
