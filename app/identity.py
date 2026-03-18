from __future__ import annotations

from datetime import datetime
import secrets

from sqlalchemy import or_

from app.extensions import db
from app.models import Assignment, CalendarAccount, EmailVerification, GroupMember, Project, ProjectMember, User, UserEmail


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def find_user_by_email(email: str | None) -> User | None:
    normalized = normalize_email(email)
    if not normalized:
        return None
    alias = UserEmail.query.filter(UserEmail.email.ilike(normalized)).first()
    if alias:
        return User.query.get(alias.user_id)
    return User.query.filter(User.email.ilike(normalized)).first()


def ensure_primary_user_email(user: User) -> UserEmail:
    normalized = normalize_email(user.email)
    primary = UserEmail.query.filter_by(user_id=user.id, is_primary=True).first()
    if primary:
        if primary.email != normalized:
            primary.email = normalized
        if user.avatar_url and primary.avatar_url != user.avatar_url:
            primary.avatar_url = user.avatar_url
        return primary

    alias = UserEmail.query.filter(UserEmail.email.ilike(normalized)).first()
    if alias:
        alias.user_id = user.id
        alias.is_primary = True
        alias.email = normalized
        if user.avatar_url and alias.avatar_url != user.avatar_url:
            alias.avatar_url = user.avatar_url
        return alias

    alias = UserEmail(user_id=user.id, email=normalized, avatar_url=user.avatar_url, is_primary=True)
    db.session.add(alias)
    return alias


def list_user_emails(user_id: int) -> list[UserEmail]:
    return UserEmail.query.filter_by(user_id=user_id).order_by(UserEmail.is_primary.desc(), UserEmail.email.asc()).all()


def add_user_email(user: User, email: str) -> UserEmail:
    normalized = normalize_email(email)
    if not normalized:
        raise ValueError("Email is required.")
    existing = UserEmail.query.filter(UserEmail.email.ilike(normalized)).first()
    if existing and existing.user_id != user.id:
        raise ValueError("That email is already attached to another account.")
    if User.query.filter(User.email.ilike(normalized), User.id != user.id).first():
        raise ValueError("That email is already the primary email for another account.")
    if existing:
        return existing
    alias = UserEmail(user_id=user.id, email=normalized, is_primary=False)
    db.session.add(alias)
    return alias


def sync_user_avatar_from_primary_email(user: User) -> None:
    primary = UserEmail.query.filter_by(user_id=user.id, is_primary=True).first()
    user.avatar_url = primary.avatar_url if primary and primary.avatar_url else None


def create_email_verification(*, user: User, email: str, purpose: str, source_user: User | None = None) -> EmailVerification:
    normalized = normalize_email(email)
    if not normalized:
        raise ValueError("Email is required.")
    if purpose not in {"add_email", "merge_account"}:
        raise ValueError("Unsupported verification purpose.")

    if purpose == "add_email":
        existing = UserEmail.query.filter(UserEmail.email.ilike(normalized)).first()
        if existing and existing.user_id != user.id:
            raise ValueError("That email is already attached to another account.")
        if User.query.filter(User.email.ilike(normalized), User.id != user.id).first():
            raise ValueError("That email is already the primary email for another account.")
    if purpose == "merge_account":
        if not source_user or source_user.id == user.id:
            raise ValueError("Choose a different account to merge.")

    pending = EmailVerification.query.filter(
        EmailVerification.user_id == user.id,
        EmailVerification.email.ilike(normalized),
        EmailVerification.purpose == purpose,
        EmailVerification.consumed_at.is_(None),
    ).first()
    if pending:
        pending.token = secrets.token_hex(24)
        pending.created_at = datetime.utcnow()
        pending.source_user_id = source_user.id if source_user else None
        return pending

    verification = EmailVerification(
        user_id=user.id,
        source_user_id=source_user.id if source_user else None,
        email=normalized,
        token=secrets.token_hex(24),
        purpose=purpose,
        created_at=datetime.utcnow(),
    )
    db.session.add(verification)
    return verification


def consume_email_verification(token: str) -> EmailVerification | None:
    verification = EmailVerification.query.filter_by(token=token).first()
    if not verification or verification.consumed_at is not None:
        return None
    verification.consumed_at = datetime.utcnow()
    return verification


def set_primary_user_email(user: User, email_id: int) -> None:
    alias = UserEmail.query.filter_by(id=email_id, user_id=user.id).first()
    if not alias:
        raise ValueError("Email not found on this account.")
    UserEmail.query.filter_by(user_id=user.id, is_primary=True).update({"is_primary": False})
    alias.is_primary = True
    user.email = alias.email
    user.avatar_url = alias.avatar_url


def remove_user_email(user: User, email_id: int) -> None:
    alias = UserEmail.query.filter_by(id=email_id, user_id=user.id).first()
    if not alias:
        raise ValueError("Email not found on this account.")
    if alias.is_primary:
        raise ValueError("Choose another primary email before unlinking this one.")

    EmailVerification.query.filter(
        EmailVerification.user_id == user.id,
        EmailVerification.email.ilike(alias.email),
        EmailVerification.consumed_at.is_(None),
    ).delete(synchronize_session=False)
    db.session.delete(alias)


def search_users_by_identity(query: str, exclude_user_id: int | None = None, limit: int = 10) -> list[User]:
    q = normalize_email(query)
    if not q:
        return []
    alias_user_ids = [row.user_id for row in UserEmail.query.filter(UserEmail.email.ilike(f"%{q}%")).limit(limit * 2).all()]
    clauses = [User.email.ilike(f"%{q}%"), User.display_name.ilike(f"%{q}%")]
    if alias_user_ids:
        clauses.append(User.id.in_(alias_user_ids))
    users = User.query.filter(or_(*clauses)).limit(limit * 2).all()
    deduped: list[User] = []
    seen: set[int] = set()
    for user in users:
        if exclude_user_id and user.id == exclude_user_id:
            continue
        if user.id in seen:
            continue
        seen.add(user.id)
        deduped.append(user)
        if len(deduped) >= limit:
            break
    return deduped


def merge_users(*, target_user: User, source_user: User) -> None:
    if target_user.id == source_user.id:
        raise ValueError("Cannot merge the same account.")

    Project.query.filter_by(owner_id=source_user.id).update({"owner_id": target_user.id})

    for row in ProjectMember.query.filter_by(user_id=source_user.id).all():
        exists = ProjectMember.query.filter_by(project_id=row.project_id, user_id=target_user.id).first()
        if exists:
            db.session.delete(row)
        else:
            row.user_id = target_user.id

    for row in GroupMember.query.filter_by(user_id=source_user.id).all():
        exists = GroupMember.query.filter_by(group_id=row.group_id, user_id=target_user.id).first()
        if exists:
            db.session.delete(row)
        else:
            row.user_id = target_user.id

    Assignment.query.filter_by(user_id=source_user.id).update({"user_id": target_user.id})

    for account in CalendarAccount.query.filter_by(user_id=source_user.id).all():
        existing = CalendarAccount.query.filter_by(user_id=target_user.id, provider=account.provider).first()
        if existing:
            existing.provider_user_id = existing.provider_user_id or account.provider_user_id
            existing.access_token = existing.access_token or account.access_token
            existing.refresh_token = existing.refresh_token or account.refresh_token
            existing.token_expires_at = existing.token_expires_at or account.token_expires_at
            db.session.delete(account)
        else:
            account.user_id = target_user.id

    ensure_primary_user_email(target_user)
    source_aliases = list_user_emails(source_user.id)
    for alias in source_aliases:
        duplicate = UserEmail.query.filter(UserEmail.email.ilike(alias.email), UserEmail.user_id == target_user.id).first()
        if duplicate:
            if not duplicate.avatar_url and alias.avatar_url:
                duplicate.avatar_url = alias.avatar_url
            db.session.delete(alias)
            continue
        alias.user_id = target_user.id
        alias.is_primary = False

    sync_user_avatar_from_primary_email(target_user)
    db.session.delete(source_user)
