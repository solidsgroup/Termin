from datetime import datetime, timezone
from zoneinfo import ZoneInfo, available_timezones

from flask import current_app, session
from app.models import ExternalIdentity, User, UserEmail


DEFAULT_USER_TIMEZONE = "UTC"
COMMON_TIMEZONE_OPTIONS = [
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Phoenix",
    "America/Anchorage",
    "Pacific/Honolulu",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Kolkata",
    "Australia/Sydney",
]
ALL_TIMEZONE_OPTIONS = tuple(sorted(available_timezones()))


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


def normalize_user_timezone(value: str | None) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return DEFAULT_USER_TIMEZONE
    try:
        ZoneInfo(candidate)
    except Exception:
        return DEFAULT_USER_TIMEZONE
    return candidate


def user_timezone_name(user: User | None) -> str:
    if not user:
        return DEFAULT_USER_TIMEZONE
    return normalize_user_timezone(getattr(user, "timezone", None))


def user_timezone(user: User | None) -> ZoneInfo:
    return ZoneInfo(user_timezone_name(user))


def to_user_timezone(value: datetime | None, user: User | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.astimezone(user_timezone(user))


def format_datetime_for_user(value: datetime | None, user: User | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    localized = to_user_timezone(value, user)
    return localized.strftime(fmt) if localized else ""
