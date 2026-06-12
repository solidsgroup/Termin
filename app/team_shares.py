from app.extensions import db
from app.models import Group, GroupMember, Project, ProjectMember, ProjectTeamShare


def team_member_user_ids(team_project_id: int) -> set[int]:
    team = Project.query.get(team_project_id)
    if not team or not getattr(team, "is_team", False):
        return set()
    user_ids: set[int] = set()
    if team.owner_id:
        user_ids.add(int(team.owner_id))
    user_ids.update(
        int(row.user_id)
        for row in ProjectMember.query.filter_by(project_id=team.id).all()
        if row.user_id
    )
    return user_ids


def user_team_project_ids(user_id: int) -> set[int]:
    owned_team_ids = {
        int(row.id)
        for row in Project.query.filter_by(owner_id=user_id, is_team=True).all()
        if row.id
    }
    member_team_ids = {
        int(row.project_id)
        for row in ProjectMember.query.filter_by(user_id=user_id).all()
        if row.project_id
    }
    if member_team_ids:
        member_team_ids = {
            int(row.id)
            for row in Project.query.filter(Project.id.in_(sorted(member_team_ids)), Project.is_team.is_(True)).all()
        }
    return owned_team_ids.union(member_team_ids)


def team_shared_project_ids_for_user(user_id: int) -> set[int]:
    team_ids = user_team_project_ids(user_id)
    if not team_ids:
        return set()
    return {
        int(row.project_id)
        for row in ProjectTeamShare.query.filter(ProjectTeamShare.team_project_id.in_(sorted(team_ids))).all()
        if row.project_id
    }


def project_team_member_user_ids(project_id: int) -> set[int]:
    team_ids = [
        int(row.team_project_id)
        for row in ProjectTeamShare.query.filter_by(project_id=project_id).all()
        if row.team_project_id
    ]
    user_ids: set[int] = set()
    for team_id in team_ids:
        user_ids.update(team_member_user_ids(team_id))
    return user_ids


def project_has_team_access(user_id: int, project_id: int) -> bool:
    team_ids = user_team_project_ids(user_id)
    if not team_ids:
        return False
    return (
        ProjectTeamShare.query.filter(
            ProjectTeamShare.project_id == project_id,
            ProjectTeamShare.team_project_id.in_(sorted(team_ids)),
        ).first()
        is not None
    )


def project_access_user_ids(project_id: int) -> set[int]:
    project = Project.query.get(project_id)
    user_ids: set[int] = set()
    if project and project.owner_id:
        user_ids.add(int(project.owner_id))
    user_ids.update(
        int(row.user_id)
        for row in ProjectMember.query.filter_by(project_id=project_id).all()
        if row.user_id
    )
    group_ids = [group_id for (group_id,) in db.session.query(Group.id).filter(Group.project_id == project_id).all()]
    if group_ids:
        user_ids.update(
            int(row.user_id)
            for row in GroupMember.query.filter(GroupMember.group_id.in_(group_ids)).all()
            if row.user_id
        )
    user_ids.update(project_team_member_user_ids(project_id))
    return {user_id for user_id in user_ids if user_id}


def accessible_project_ids_for_user(user_id: int) -> set[int]:
    owned_project_ids = {
        int(project.id)
        for project in Project.query.filter_by(owner_id=user_id).all()
        if project and project.id
    }
    member_project_ids = {
        int(row.project_id)
        for row in ProjectMember.query.filter_by(user_id=user_id).all()
        if row.project_id
    }
    member_group_project_ids = {
        int(project_id)
        for (project_id,) in db.session.query(Group.project_id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(GroupMember.user_id == user_id)
        .all()
        if project_id
    }
    return owned_project_ids.union(member_project_ids).union(member_group_project_ids).union(
        team_shared_project_ids_for_user(user_id)
    )
