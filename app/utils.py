from flask import current_app, session
from app.models import ExternalIdentity, User, UserEmail


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return User.query.get(user_id)


def is_admin(user: User | None) -> bool:
    if not user:
        return False

    configured = (current_app.config.get("ADMIN_EMAILS") or "").strip()
    if configured:
        allowed = {
            item.strip().lower()
            for item in configured.split(",")
            if item.strip()
        }
        if user.email and user.email.lower() in allowed:
            return True
        aliases = UserEmail.query.filter_by(user_id=user.id).all()
        return any(alias.email and alias.email.lower() in allowed for alias in aliases)

    return user.id == 1


def display_name_for_user(user: User | None) -> str | None:
    if not user:
        return None
    if user.display_name:
        return user.display_name
    identity = (
        ExternalIdentity.query.filter(
            ExternalIdentity.user_id == user.id,
            ExternalIdentity.display_name.isnot(None),
        )
        .order_by(ExternalIdentity.created_at.desc())
        .first()
    )
    if identity and identity.display_name:
        return identity.display_name
    return user.email
