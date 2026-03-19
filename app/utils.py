from flask import current_app, session
from app.models import User, UserEmail


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
