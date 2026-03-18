from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, redirect, session, url_for, render_template

from app.extensions import db
from app.identity import ensure_primary_user_email, find_user_by_email, normalize_email, sync_user_avatar_from_primary_email
from app.models import CalendarAccount, User, UserEmail
from app.oauth import oauth


auth_bp = Blueprint("auth", __name__)


def _get_or_create_user(email: str, display_name: str | None, avatar_url: str | None):
    normalized_email = normalize_email(email)
    user = find_user_by_email(normalized_email)
    if not user:
        user = User(email=normalized_email, display_name=display_name, avatar_url=avatar_url)
        db.session.add(user)
    else:
        if display_name and user.display_name != display_name:
            user.display_name = display_name
    db.session.flush()
    primary_email = ensure_primary_user_email(user)
    login_alias = UserEmail.query.filter(UserEmail.email.ilike(normalized_email)).first()
    if normalized_email and normalized_email != user.email:
        from app.identity import add_user_email
        try:
            login_alias = add_user_email(user, normalized_email)
        except ValueError:
            login_alias = UserEmail.query.filter(UserEmail.email.ilike(normalized_email)).first()
    if avatar_url and login_alias and login_alias.user_id == user.id and login_alias.avatar_url != avatar_url:
        login_alias.avatar_url = avatar_url
    if primary_email and primary_email.id == (login_alias.id if login_alias else primary_email.id):
        user.avatar_url = primary_email.avatar_url or avatar_url
    else:
        sync_user_avatar_from_primary_email(user)
    db.session.commit()
    return user


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.login"))
        from app.utils import current_user
        if not current_user():
            session.clear()
            return redirect(url_for("auth.login"))
        return fn(*args, **kwargs)

    return wrapper


@auth_bp.get("/login")
def login():
    return render_template("login.html")


@auth_bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@auth_bp.get("/login/google")
def login_google():
    redirect_uri = url_for("auth.google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.get("/auth/google/callback")
def google_callback():
    token = oauth.google.authorize_access_token()
    userinfo = oauth.google.get("https://openidconnect.googleapis.com/v1/userinfo").json()

    email = userinfo.get("email")
    if not email:
        return "Email not available from Google", 400

    user = _get_or_create_user(
        email=email, display_name=userinfo.get("name"), avatar_url=userinfo.get("picture")
    )
    session["user_id"] = user.id

    _upsert_calendar_account(
        user_id=user.id,
        provider="google",
        provider_user_id=userinfo.get("sub"),
        access_token=token.get("access_token"),
        refresh_token=token.get("refresh_token"),
        expires_in=token.get("expires_in"),
    )

    return redirect(url_for("ui.dashboard"))


@auth_bp.get("/login/microsoft")
def login_microsoft():
    redirect_uri = url_for("auth.microsoft_callback", _external=True)
    return oauth.microsoft.authorize_redirect(redirect_uri)


@auth_bp.get("/auth/microsoft/callback")
def microsoft_callback():
    token = oauth.microsoft.authorize_access_token()

    # Use Graph for reliable profile + email
    profile = oauth.microsoft.get("https://graph.microsoft.com/v1.0/me").json()
    email = profile.get("mail") or profile.get("userPrincipalName")
    if not email:
        return "Email not available from Microsoft", 400

    user = _get_or_create_user(email=email, display_name=profile.get("displayName"), avatar_url=None)
    session["user_id"] = user.id

    _upsert_calendar_account(
        user_id=user.id,
        provider="microsoft",
        provider_user_id=profile.get("id"),
        access_token=token.get("access_token"),
        refresh_token=token.get("refresh_token"),
        expires_in=token.get("expires_in"),
    )

    return redirect(url_for("ui.dashboard"))


def _upsert_calendar_account(
    *,
    user_id: int,
    provider: str,
    provider_user_id: str | None,
    access_token: str | None,
    refresh_token: str | None,
    expires_in: int | None,
):
    expires_at = None
    if expires_in:
        expires_at = datetime.utcnow().replace(microsecond=0) + timedelta(seconds=expires_in)

    account = CalendarAccount.query.filter_by(user_id=user_id, provider=provider).first()
    if not account:
        account = CalendarAccount(user_id=user_id, provider=provider)
        db.session.add(account)

    account.provider_user_id = provider_user_id
    account.access_token = access_token
    account.refresh_token = refresh_token or account.refresh_token
    account.token_expires_at = expires_at
    db.session.commit()
