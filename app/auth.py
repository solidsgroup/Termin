from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from app.extensions import db
from app.identity import (
    add_user_email,
    claim_collaborator_profile,
    ensure_primary_user_email,
    find_user_by_email,
    find_user_by_external_identity,
    normalize_email,
    remove_external_identity,
    sync_user_avatar_from_primary_email,
    upsert_external_identity,
)
from app.models import CollaboratorProfile, User, UserEmail
from app.oauth import oauth


auth_bp = Blueprint("auth", __name__)
OWNER_BOOTSTRAP_EMAIL = "brunnels@solids.group"


def _can_self_bootstrap_account(email: str | None) -> bool:
    return normalize_email(email) == OWNER_BOOTSTRAP_EMAIL


def _provider_login_error(message: str):
    session["auth_error"] = message
    return redirect(url_for("auth.login"))


def _public_url(endpoint: str, **values) -> str:
    base_url = str(current_app.config.get("PUBLIC_BASE_URL") or "").rstrip("/")
    if base_url:
        return f"{base_url}{url_for(endpoint, **values)}"
    return url_for(endpoint, _external=True, **values)


def _get_or_create_user(email: str, display_name: str | None, avatar_url: str | None):
    normalized_email = normalize_email(email)
    user = find_user_by_email(normalized_email)
    if not user:
        if not _can_self_bootstrap_account(normalized_email):
            raise ValueError("No account is available for that email yet. Use your invitation email link to claim access first.")
        user = User(email=normalized_email, display_name=display_name, avatar_url=avatar_url)
        db.session.add(user)
    else:
        if display_name and user.display_name != display_name:
            user.display_name = display_name
    db.session.flush()
    primary_email = ensure_primary_user_email(user)
    login_alias = UserEmail.query.filter(UserEmail.email.ilike(normalized_email)).first()
    if normalized_email and normalized_email != user.email:
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


def _store_external_identity(
    *,
    user: User,
    provider: str,
    provider_user_id: str | None,
    email: str | None,
    display_name: str | None,
    avatar_url: str | None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    expires_in: int | None = None,
):
    if not provider_user_id:
        return
    token_expires_at = datetime.utcnow() + timedelta(seconds=int(expires_in)) if expires_in else None
    upsert_external_identity(
        user=user,
        provider=provider,
        provider_user_id=str(provider_user_id),
        email=email,
        display_name=display_name,
        avatar_url=avatar_url,
        access_token=access_token,
        refresh_token=refresh_token,
        token_expires_at=token_expires_at,
    )


def _link_provider_account(
    *,
    provider: str,
    provider_user_id: str | None,
    email: str | None,
    display_name: str | None,
    avatar_url: str | None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    expires_in: int | None = None,
) -> User:
    link_user_id = session.pop("oauth_link_user_id", None)
    if not link_user_id or session.get("user_id") != link_user_id:
        raise ValueError("Provider linking session expired. Start the link again from the Account page.")

    user = User.query.get(link_user_id)
    if not user:
        raise ValueError("Signed-in account not found.")

    normalized_email = normalize_email(email)
    existing_user = find_user_by_email(normalized_email) if normalized_email else None
    if existing_user and existing_user.id != user.id:
        raise ValueError("That email is already attached to another account.")

    if normalized_email:
        ensure_primary_user_email(user)
        if normalized_email != user.email:
            add_user_email(user, normalized_email)

    _store_external_identity(
        user=user,
        provider=provider,
        provider_user_id=provider_user_id,
        email=normalized_email,
        display_name=display_name,
        avatar_url=avatar_url,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )

    login_alias = UserEmail.query.filter(UserEmail.email.ilike(normalized_email)).first() if normalized_email else None
    if avatar_url and login_alias and login_alias.user_id == user.id:
        login_alias.avatar_url = avatar_url
    sync_user_avatar_from_primary_email(user)
    db.session.commit()
    session["user_id"] = user.id
    return user


def _claim_collaborator_with_provider(
    *,
    provider: str,
    provider_user_id: str | None,
    email: str | None,
    display_name: str | None,
    avatar_url: str | None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    expires_in: int | None = None,
) -> User:
    collaborator_token = session.pop("collaborator_claim_token", None)
    if not collaborator_token:
        raise ValueError("Collaborator claim session expired. Start again from the collaborator portal.")

    collaborator = CollaboratorProfile.query.filter_by(access_token=collaborator_token).first()
    if not collaborator:
        raise ValueError("Collaborator link not found.")

    user = claim_collaborator_profile(
        collaborator,
        display_name=display_name,
        avatar_url=avatar_url,
    )

    normalized_email = normalize_email(email)
    if normalized_email and normalized_email != collaborator.email:
        existing_user = find_user_by_email(normalized_email)
        if existing_user and existing_user.id != user.id:
            raise ValueError("That provider email is already attached to another account.")
        add_user_email(user, normalized_email)

    _store_external_identity(
        user=user,
        provider=provider,
        provider_user_id=provider_user_id,
        email=normalized_email,
        display_name=display_name,
        avatar_url=avatar_url,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )

    login_alias = UserEmail.query.filter(UserEmail.email.ilike(normalized_email)).first() if normalized_email else None
    if avatar_url and login_alias and login_alias.user_id == user.id:
        login_alias.avatar_url = avatar_url
    sync_user_avatar_from_primary_email(user)
    db.session.commit()
    session["user_id"] = user.id
    return user


def _login_with_provider(
    *,
    provider: str,
    provider_user_id: str | None,
    email: str | None,
    display_name: str | None,
    avatar_url: str | None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    expires_in: int | None = None,
) -> User:
    user = find_user_by_external_identity(provider, provider_user_id)
    if not user:
        normalized_email = normalize_email(email)
        if not normalized_email:
            raise ValueError(f"Email not available from {provider.title()}")
        user = _get_or_create_user(
            email=normalized_email,
            display_name=display_name,
            avatar_url=avatar_url,
        )
    _store_external_identity(
        user=user,
        provider=provider,
        provider_user_id=provider_user_id,
        email=email,
        display_name=display_name,
        avatar_url=avatar_url,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )
    db.session.commit()
    session["user_id"] = user.id
    return user


def _github_primary_email(token: dict) -> str | None:
    response = oauth.github.get(
        "user/emails",
        token=token,
        headers={"Accept": "application/vnd.github+json"},
    )
    emails = response.json()
    if not isinstance(emails, list):
        return None
    primary_verified = next((row for row in emails if row.get("primary") and row.get("verified") and row.get("email")), None)
    if primary_verified:
        return primary_verified.get("email")
    verified = next((row for row in emails if row.get("verified") and row.get("email")), None)
    if verified:
        return verified.get("email")
    fallback = next((row for row in emails if row.get("email")), None)
    if fallback:
        return fallback.get("email")
    return None


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


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    error = session.pop("auth_error", None)
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password") or ""
        normalized_email = normalize_email(email)
        admin_emails = (current_app.config.get("ADMIN_EMAILS") or "").strip()
        admin_password = current_app.config.get("LOCAL_ADMIN_PASSWORD") or ""
        if admin_emails and admin_password:
            admin_set = {normalize_email(item) for item in admin_emails.split(",") if item.strip()}
            if normalized_email in admin_set and password == admin_password:
                user = find_user_by_email(normalized_email)
                if not user:
                    user = User(email=normalized_email, display_name=normalized_email)
                    db.session.add(user)
                    db.session.flush()
                ensure_primary_user_email(user)
                db.session.commit()
                session["user_id"] = user.id
                return redirect(url_for("ui.dashboard"))
        user = find_user_by_email(email)
        if not user or not user.password_hash or not check_password_hash(user.password_hash, password):
            error = "Invalid email or password."
        else:
            session["user_id"] = user.id
            return redirect(url_for("ui.dashboard"))

    if session.get("collaborator_claim_token"):
        error = error or "Finish claiming your collaborator account by setting a password or linking an account."
    if session.get("oauth_link_user_id"):
        error = error or "Finish linking your provider account from the Account page."

    return render_template("login.html", error=error)


@auth_bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@auth_bp.get("/login/google")
def login_google():
    redirect_uri = _public_url("auth.google_callback")
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.get("/connect/google")
@login_required
def connect_google():
    session["oauth_link_user_id"] = session.get("user_id")
    redirect_uri = _public_url("auth.google_callback")
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.get("/auth/google/callback")
def google_callback():
    token = oauth.google.authorize_access_token()
    userinfo = oauth.google.get("https://openidconnect.googleapis.com/v1/userinfo").json()

    email = userinfo.get("email")
    if not email:
        return "Email not available from Google", 400

    try:
        if session.get("collaborator_claim_token"):
            user = _claim_collaborator_with_provider(
                provider="google",
                provider_user_id=userinfo.get("sub"),
                email=email,
                display_name=userinfo.get("name"),
                avatar_url=userinfo.get("picture"),
            )
            _upsert_calendar_account(
                user_id=user.id,
                provider="google",
                provider_user_id=userinfo.get("sub"),
                access_token=token.get("access_token"),
                refresh_token=token.get("refresh_token"),
                expires_in=token.get("expires_in"),
            )
            return redirect(url_for("ui.dashboard"))
        if session.get("oauth_link_user_id"):
            user = _link_provider_account(
                provider="google",
                provider_user_id=userinfo.get("sub"),
                email=email,
                display_name=userinfo.get("name"),
                avatar_url=userinfo.get("picture"),
            )
            _upsert_calendar_account(
                user_id=user.id,
                provider="google",
                provider_user_id=userinfo.get("sub"),
                access_token=token.get("access_token"),
                refresh_token=token.get("refresh_token"),
                expires_in=token.get("expires_in"),
            )
            return redirect(url_for("ui.account"))

        user = _login_with_provider(
            provider="google",
            provider_user_id=userinfo.get("sub"),
            email=email,
            display_name=userinfo.get("name"),
            avatar_url=userinfo.get("picture"),
        )
    except ValueError as exc:
        return _provider_login_error(str(exc))

    _upsert_calendar_account(
        user_id=user.id,
        provider="google",
        provider_user_id=userinfo.get("sub"),
        access_token=token.get("access_token"),
        refresh_token=token.get("refresh_token"),
        expires_in=token.get("expires_in"),
    )

    return redirect(url_for("ui.dashboard"))

@auth_bp.get("/login/github")
def login_github():
    redirect_uri = _public_url("auth.github_callback")
    return oauth.github.authorize_redirect(redirect_uri)


@auth_bp.get("/connect/github")
@login_required
def connect_github():
    session["oauth_link_user_id"] = session.get("user_id")
    redirect_uri = _public_url("auth.github_callback")
    return oauth.github.authorize_redirect(redirect_uri)


@auth_bp.get("/auth/github/callback")
def github_callback():
    token = oauth.github.authorize_access_token()
    profile = oauth.github.get(
        "user",
        token=token,
        headers={"Accept": "application/vnd.github+json"},
    ).json()
    email = profile.get("email") or _github_primary_email(token)
    avatar_url = profile.get("avatar_url")
    display_name = profile.get("name") or profile.get("login")
    provider_user_id = profile.get("id")

    try:
        if session.get("collaborator_claim_token"):
            _claim_collaborator_with_provider(
                provider="github",
                provider_user_id=provider_user_id,
                email=email,
                display_name=display_name,
                avatar_url=avatar_url,
                access_token=token.get("access_token"),
                refresh_token=token.get("refresh_token"),
                expires_in=token.get("expires_in"),
            )
            return redirect(url_for("ui.dashboard"))
        if session.get("oauth_link_user_id"):
            _link_provider_account(
                provider="github",
                provider_user_id=provider_user_id,
                email=email,
                display_name=display_name,
                avatar_url=avatar_url,
                access_token=token.get("access_token"),
                refresh_token=token.get("refresh_token"),
                expires_in=token.get("expires_in"),
            )
            return redirect(url_for("ui.account"))

        _login_with_provider(
            provider="github",
            provider_user_id=provider_user_id,
            email=email,
            display_name=display_name,
            avatar_url=avatar_url,
            access_token=token.get("access_token"),
            refresh_token=token.get("refresh_token"),
            expires_in=token.get("expires_in"),
        )
    except ValueError as exc:
        return _provider_login_error(str(exc))

    return redirect(url_for("ui.dashboard"))


@auth_bp.post("/account/identities/<int:identity_id>/unlink")
@login_required
def unlink_external_account(identity_id: int):
    user = User.query.get(session.get("user_id"))
    try:
        remove_external_identity(user, identity_id)
        db.session.commit()
    except ValueError as exc:
        return str(exc), 400
    return redirect(url_for("ui.account"))


def _upsert_calendar_account(
    *,
    user_id: int,
    provider: str,
    provider_user_id: str | None,
    access_token: str | None,
    refresh_token: str | None,
    expires_in: int | None,
):
    from app.models import CalendarAccount

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
