from __future__ import annotations

from datetime import datetime
import secrets

from app.extensions import db
from app.models import CollaboratorProfile


def get_or_create_collaborator_profile(
    email: str,
    default_calendar_opt_in: bool = False,
) -> CollaboratorProfile:
    normalized = (email or "").strip().lower()
    profile = CollaboratorProfile.query.filter_by(email=normalized).first()
    if profile:
        return profile

    profile = CollaboratorProfile(
        email=normalized,
        access_token=secrets.token_hex(24),
        default_calendar_opt_in=default_calendar_opt_in,
        updated_at=datetime.utcnow(),
    )
    db.session.add(profile)
    db.session.flush()
    return profile
