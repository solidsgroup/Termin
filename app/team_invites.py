from datetime import datetime
import secrets

from app.extensions import db
from app.identity import normalize_email
from app.models import Project, ProjectMember, TeamInvite, User, UserEmail


def pending_team_invite_for_email(token: str | None, email: str | None) -> TeamInvite | None:
    normalized_email = normalize_email(email)
    if not token or not normalized_email:
        return None
    invite = TeamInvite.query.filter_by(token=token, status="sent").first()
    if not invite or normalize_email(invite.email) != normalized_email:
        return None
    team = Project.query.get(invite.team_project_id)
    if not team or not getattr(team, "is_team", False):
        return None
    return invite


def get_or_create_team_invite(team: Project, inviter: User, email: str) -> TeamInvite:
    normalized_email = normalize_email(email)
    if not normalized_email:
        raise ValueError("Email is required.")
    if not team or not getattr(team, "is_team", False):
        raise ValueError("Team not found.")
    invite = TeamInvite.query.filter_by(
        team_project_id=team.id,
        email=normalized_email,
        status="sent",
    ).first()
    if invite:
        invite.inviter_user_id = inviter.id
        return invite
    invite = TeamInvite(
        team_project_id=team.id,
        inviter_user_id=inviter.id,
        email=normalized_email,
        token=secrets.token_urlsafe(32),
        status="sent",
    )
    db.session.add(invite)
    return invite


def user_matches_team_invite_email(user: User, email: str | None) -> bool:
    normalized_email = normalize_email(email)
    if not user or not normalized_email:
        return False
    if normalize_email(user.email) == normalized_email:
        return True
    return UserEmail.query.filter_by(user_id=user.id, email=normalized_email).first() is not None


def accept_team_invite(invite: TeamInvite, user: User) -> bool:
    if not invite or invite.status != "sent":
        raise ValueError("This invitation link is invalid or already used.")
    if not user_matches_team_invite_email(user, invite.email):
        raise ValueError("This invitation was sent to a different email address.")
    team = Project.query.get(invite.team_project_id)
    if not team or not getattr(team, "is_team", False):
        raise ValueError("The invited Team no longer exists.")
    created = False
    if not ProjectMember.query.filter_by(project_id=team.id, user_id=user.id).first():
        db.session.add(ProjectMember(project_id=team.id, user_id=user.id))
        created = True
    invite.status = "accepted"
    invite.accepted_user_id = user.id
    invite.accepted_at = datetime.utcnow()
    return created
