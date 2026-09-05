from __future__ import annotations

from datetime import datetime, timedelta

import requests
from flask import current_app

from app.extensions import db
from app.models import CalendarAccount


GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleOAuthError(RuntimeError):
    pass


def normalize_google_scopes(value) -> set[str]:
    if isinstance(value, str):
        values = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []
    return {str(item).strip() for item in values if str(item).strip()}


def google_account_for_user(user_id: int) -> CalendarAccount | None:
    return CalendarAccount.query.filter_by(user_id=user_id, provider="google").first()


def google_account_has_scope(account: CalendarAccount | None, scope: str) -> bool:
    return bool(account and scope in normalize_google_scopes(account.scopes))


def ensure_google_access_token(account: CalendarAccount, *, required_scope: str | None = None) -> str:
    if required_scope and not google_account_has_scope(account, required_scope):
        raise GoogleOAuthError("Reconnect Google Drive to grant Termin access to selected files.")
    if (
        account.access_token
        and account.token_expires_at
        and account.token_expires_at > datetime.utcnow() + timedelta(seconds=60)
    ):
        return account.access_token
    if not account.refresh_token:
        raise GoogleOAuthError("Google authorization expired. Reconnect Google Drive.")

    try:
        response = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": current_app.config["GOOGLE_CLIENT_ID"],
                "client_secret": current_app.config["GOOGLE_CLIENT_SECRET"],
                "refresh_token": account.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        raise GoogleOAuthError("Google authorization could not be refreshed.") from exc
    if response.status_code >= 400:
        raise GoogleOAuthError("Google authorization expired. Reconnect Google Drive.")
    payload = response.json()
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise GoogleOAuthError("Google did not return an access token.")
    account.access_token = access_token
    expires_in = payload.get("expires_in")
    if expires_in:
        account.token_expires_at = datetime.utcnow() + timedelta(seconds=int(expires_in))
    returned_scopes = normalize_google_scopes(payload.get("scope"))
    if returned_scopes:
        account.scopes = " ".join(sorted(normalize_google_scopes(account.scopes) | returned_scopes))
    db.session.commit()
    return access_token
