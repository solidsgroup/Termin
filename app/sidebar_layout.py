from types import SimpleNamespace

from app.extensions import db
from app.models import Division, ProjectSidebarPreference


def ensure_sidebar_preference(user_id: int, project_id: int, *, default_division_id: int | None = None, default_position: int | None = None):
    pref = ProjectSidebarPreference.query.filter_by(user_id=user_id, project_id=project_id).first()
    if pref:
        return pref

    if default_position is None:
        top_positions = [row.position or 0 for row in ProjectSidebarPreference.query.filter_by(user_id=user_id, division_id=None).all()]
        division_positions = [division.position or 0 for division in Division.query.filter_by(owner_id=user_id).all()]
        default_position = max(top_positions + division_positions + [0]) + 1

    pref = ProjectSidebarPreference(
        user_id=user_id,
        project_id=project_id,
        division_id=default_division_id,
        position=default_position,
    )
    db.session.add(pref)
    db.session.flush()
    return pref


def sidebar_preference_map(user_id: int, projects: list, divisions: list[Division]):
    project_ids = [project.id for project in projects]
    pref_rows = (
        ProjectSidebarPreference.query.filter(
            ProjectSidebarPreference.user_id == user_id,
            ProjectSidebarPreference.project_id.in_(project_ids),
        ).all()
        if project_ids
        else []
    )
    pref_map = {row.project_id: row for row in pref_rows}
    valid_division_ids = {division.id for division in divisions}
    max_position = max(
        [row.position or 0 for row in pref_rows]
        + [division.position or 0 for division in divisions]
        + [0]
    )
    next_position = max_position + 1
    for project in sorted(projects, key=lambda item: (item.position or 0, item.id)):
        if project.id not in pref_map:
            pref_map[project.id] = SimpleNamespace(
                user_id=user_id,
                project_id=project.id,
                division_id=None,
                position=next_position,
            )
            next_position += 1
        elif pref_map[project.id].division_id not in valid_division_ids:
            pref_map[project.id].division_id = None
    return pref_map


def top_level_sidebar_items(projects: list, divisions: list[Division], pref_map: dict):
    items = []
    for project in projects:
        pref = pref_map.get(project.id)
        if pref and pref.division_id:
            continue
        items.append({"type": "project", "id": project.id, "position": (pref.position if pref else 0), "project": project})
    for division in divisions:
        items.append({"type": "division", "id": division.id, "position": division.position or 0, "division": division})
    items.sort(key=lambda item: (item["position"], 0 if item["type"] == "division" else 1, item["id"]))
    return items


def resequence_top_level_items(user_id: int) -> None:
    top_prefs = ProjectSidebarPreference.query.filter_by(user_id=user_id, division_id=None).order_by(ProjectSidebarPreference.position.asc(), ProjectSidebarPreference.id.asc()).all()
    divisions = Division.query.filter_by(owner_id=user_id).order_by(Division.position.asc(), Division.id.asc()).all()
    combined = [{"type": "project", "obj": pref, "position": pref.position or 0, "id": pref.project_id} for pref in top_prefs]
    combined += [{"type": "division", "obj": division, "position": division.position or 0, "id": division.id} for division in divisions]
    combined.sort(key=lambda item: (item["position"], 0 if item["type"] == "division" else 1, item["id"]))
    for index, item in enumerate(combined, start=1):
        item["obj"].position = index


def insert_top_level(user_id: int, position: int | None) -> int:
    resequence_top_level_items(user_id)
    max_pos = max(
        [division.position or 0 for division in Division.query.filter_by(owner_id=user_id).all()]
        + [pref.position or 0 for pref in ProjectSidebarPreference.query.filter_by(user_id=user_id, division_id=None).all()]
        + [0]
    )
    insert_pos = max(1, min(position or (max_pos + 1), max_pos + 1))
    for division in Division.query.filter(Division.owner_id == user_id, Division.position >= insert_pos).all():
        division.position += 1
    for pref in ProjectSidebarPreference.query.filter(
        ProjectSidebarPreference.user_id == user_id,
        ProjectSidebarPreference.division_id.is_(None),
        ProjectSidebarPreference.position >= insert_pos,
    ).all():
        pref.position += 1
    return insert_pos


def insert_project_position(user_id: int, division_id: int | None, position: int | None) -> int:
    if division_id is None:
        return insert_top_level(user_id, position)
    siblings = ProjectSidebarPreference.query.filter_by(user_id=user_id, division_id=division_id).order_by(ProjectSidebarPreference.position.asc(), ProjectSidebarPreference.id.asc()).all()
    for index, sibling in enumerate(siblings, start=1):
        sibling.position = index
    max_pos = len(siblings)
    insert_pos = max(1, min(position or (max_pos + 1), max_pos + 1))
    for sibling in siblings:
        if (sibling.position or 0) >= insert_pos:
            sibling.position += 1
    return insert_pos


def resequence_division_projects(user_id: int, division_id: int, exclude_project_id: int | None = None) -> None:
    prefs = ProjectSidebarPreference.query.filter_by(user_id=user_id, division_id=division_id).order_by(ProjectSidebarPreference.position.asc(), ProjectSidebarPreference.id.asc()).all()
    pos = 1
    for pref in prefs:
        if exclude_project_id and pref.project_id == exclude_project_id:
            continue
        pref.position = pos
        pos += 1


def apply_top_level_project_order(user_id: int, moving_pref, insert_index: int) -> None:
    top_prefs = (
        ProjectSidebarPreference.query.filter(
            ProjectSidebarPreference.user_id == user_id,
            ProjectSidebarPreference.division_id.is_(None),
            ProjectSidebarPreference.project_id != moving_pref.project_id,
        )
        .order_by(ProjectSidebarPreference.position.asc(), ProjectSidebarPreference.id.asc())
        .all()
    )
    divisions = Division.query.filter_by(owner_id=user_id).order_by(Division.position.asc(), Division.id.asc()).all()
    items = [{"type": "project", "obj": pref, "position": pref.position or 0, "id": pref.project_id} for pref in top_prefs]
    items += [{"type": "division", "obj": division, "position": division.position or 0, "id": division.id} for division in divisions]
    items.sort(key=lambda item: (item["position"], 0 if item["type"] == "division" else 1, item["id"]))
    bounded_index = max(0, min(insert_index, len(items)))
    moving_pref.division_id = None
    items.insert(bounded_index, {"type": "project", "obj": moving_pref, "id": moving_pref.project_id})
    for index, item in enumerate(items, start=1):
        item["obj"].position = index
