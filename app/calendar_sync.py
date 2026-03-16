from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import requests
from flask import current_app

from app.extensions import db
from app.models import CalendarAccount, ExternalEvent, Project, Task, User


GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
MS_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"


class CalendarSyncError(Exception):
    pass


def _select_calendar_account(user_id: int) -> CalendarAccount | None:
    account = CalendarAccount.query.filter_by(user_id=user_id, provider="google").first()
    if account:
        return account
    return CalendarAccount.query.filter_by(user_id=user_id, provider="microsoft").first()


def _ensure_access_token(account: CalendarAccount) -> str:
    if account.token_expires_at and account.token_expires_at > datetime.utcnow() + timedelta(seconds=60):
        return account.access_token

    if not account.refresh_token:
        raise CalendarSyncError("Missing refresh token")

    if account.provider == "google":
        data = {
            "client_id": current_app.config["GOOGLE_CLIENT_ID"],
            "client_secret": current_app.config["GOOGLE_CLIENT_SECRET"],
            "refresh_token": account.refresh_token,
            "grant_type": "refresh_token",
        }
        resp = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        account.access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if expires_in:
            account.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
        db.session.commit()
        return account.access_token

    if account.provider == "microsoft":
        data = {
            "client_id": current_app.config["MS_CLIENT_ID"],
            "client_secret": current_app.config["MS_CLIENT_SECRET"],
            "refresh_token": account.refresh_token,
            "grant_type": "refresh_token",
            "scope": "https://graph.microsoft.com/.default",
        }
        resp = requests.post(MS_TOKEN_URL, data=data, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        account.access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if expires_in:
            account.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
        new_refresh = payload.get("refresh_token")
        if new_refresh:
            account.refresh_token = new_refresh
        db.session.commit()
        return account.access_token

    raise CalendarSyncError("Unsupported provider")


def _task_times(task: Task) -> tuple[str, str] | None:
    if not task.due_at:
        return None

    start = task.due_at
    end = task.due_at + timedelta(hours=1)
    return start.isoformat(), end.isoformat()


def _build_google_event(task: Task, invitee_email: str | None) -> dict[str, Any]:
    times = _task_times(task)
    event = {
        "summary": task.title,
        "description": task.description or "",
    }
    if times:
        event["start"] = {"dateTime": times[0]}
        event["end"] = {"dateTime": times[1]}
    if invitee_email:
        event["attendees"] = [{"email": invitee_email}]
    return event


def _build_ms_event(task: Task, invitee_email: str | None) -> dict[str, Any]:
    times = _task_times(task)
    event = {
        "subject": task.title,
        "body": {"contentType": "text", "content": task.description or ""},
    }
    if times:
        event["start"] = {"dateTime": times[0], "timeZone": "UTC"}
        event["end"] = {"dateTime": times[1], "timeZone": "UTC"}
    if invitee_email:
        event["attendees"] = [{"emailAddress": {"address": invitee_email}, "type": "required"}]
    return event


def _google_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _ms_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _google_create_event(token: str, task: Task, invitee_email: str | None) -> dict[str, Any]:
    event = _build_google_event(task, invitee_email)
    params = {"sendUpdates": "all"} if invitee_email else None
    resp = requests.post(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        headers=_google_headers(token),
        params=params,
        json=event,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _google_get_event(token: str, event_id: str) -> dict[str, Any]:
    resp = requests.get(
        f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{event_id}",
        headers=_google_headers(token),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _google_patch_event(token: str, event_id: str, payload: dict[str, Any], send_updates: bool) -> dict[str, Any]:
    params = {"sendUpdates": "all"} if send_updates else None
    resp = requests.patch(
        f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{event_id}",
        headers=_google_headers(token),
        params=params,
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _ms_create_event(token: str, task: Task, invitee_email: str | None) -> dict[str, Any]:
    event = _build_ms_event(task, invitee_email)
    resp = requests.post(
        "https://graph.microsoft.com/v1.0/me/events",
        headers=_ms_headers(token),
        json=event,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _ms_get_event(token: str, event_id: str) -> dict[str, Any]:
    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/me/events/{event_id}",
        headers=_ms_headers(token),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _ms_patch_event(token: str, event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = requests.patch(
        f"https://graph.microsoft.com/v1.0/me/events/{event_id}",
        headers=_ms_headers(token),
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def ensure_task_event(task_id: int, invitee_email: str | None = None) -> None:
    task = Task.query.get(task_id)
    if not task:
        return

    project = Project.query.get(task.project_id)
    if not project:
        return

    owner = User.query.get(project.owner_id)
    if not owner:
        return

    account = _select_calendar_account(owner.id)
    if not account:
        return

    token = _ensure_access_token(account)

    external = ExternalEvent.query.filter_by(task_id=task.id, provider=account.provider).first()
    has_time = task.due_at is not None

    if account.provider == "google":
        if external:
            if invitee_email:
                event = _google_get_event(token, external.event_id)
                attendees = event.get("attendees", [])
                if not any(a.get("email") == invitee_email for a in attendees):
                    attendees.append({"email": invitee_email})
                    _google_patch_event(token, external.event_id, {"attendees": attendees}, True)
                    external.last_sync_at = datetime.utcnow()
                    db.session.commit()
        else:
            if not has_time:
                return
            event = _google_create_event(token, task, invitee_email)
            external = ExternalEvent(
                task_id=task.id,
                provider="google",
                calendar_id="primary",
                event_id=event.get("id"),
                last_sync_at=datetime.utcnow(),
            )
            db.session.add(external)
            db.session.commit()
        return

    if account.provider == "microsoft":
        if external:
            if invitee_email:
                event = _ms_get_event(token, external.event_id)
                attendees = event.get("attendees", [])
                if not any(a.get("emailAddress", {}).get("address") == invitee_email for a in attendees):
                    attendees.append({"emailAddress": {"address": invitee_email}, "type": "required"})
                    _ms_patch_event(token, external.event_id, {"attendees": attendees})
                    external.last_sync_at = datetime.utcnow()
                    db.session.commit()
        else:
            if not has_time:
                return
            event = _ms_create_event(token, task, invitee_email)
            external = ExternalEvent(
                task_id=task.id,
                provider="microsoft",
                calendar_id="primary",
                event_id=event.get("id"),
                last_sync_at=datetime.utcnow(),
            )
            db.session.add(external)
            db.session.commit()
        return
