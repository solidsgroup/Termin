from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from threading import Lock
from urllib.parse import parse_qs, quote, urlparse

import requests
from flask import current_app
from sqlalchemy.orm.attributes import flag_modified

from app.extensions import db
from app.google_oauth import GOOGLE_DRIVE_SCOPE, GoogleOAuthError, ensure_google_access_token
from app.identity import find_user_by_email, normalize_email
from app.info_utils import normalize_info_payload
from app.models import (
    Assignment,
    CalendarAccount,
    CollaboratorTaskRead,
    DiscussionEvent,
    ExternalEvent,
    GoogleDriveCommentLink,
    GoogleDriveIntegration,
    Group,
    Invite,
    Task,
    TaskCollaboratorStatus,
    TaskComment,
    TaskFollower,
    TaskNotification,
    TaskPrerequisite,
    TaskUserStatus,
    UserDiscussionActivity,
)


GOOGLE_DRIVE_SPECIALTY_TYPE = "google_drive"
GOOGLE_DRIVE_API_ROOT = "https://www.googleapis.com/drive/v3"
GOOGLE_DRIVE_FULL_SYNC_INTERVAL = timedelta(hours=1)
GOOGLE_DRIVE_SYNC_OVERLAP = timedelta(minutes=2)
_FILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{10,255}$")
_active_group_ids: set[int] = set()
_active_group_lock = Lock()
_worker_started = False


class GoogleDriveSyncError(RuntimeError):
    pass


class GoogleDriveSyncBusy(GoogleDriveSyncError):
    pass


class GoogleDriveAuthorizationRequired(GoogleDriveSyncError):
    pass


@dataclass(frozen=True)
class GoogleDriveComment:
    comment_id: str
    content: str
    created_at: datetime | None
    modified_at: datetime | None
    resolved: bool
    deleted: bool
    assignee_email: str
    author_name: str
    author_avatar_url: str
    quoted_content: str
    replies: tuple[dict, ...]


@dataclass(frozen=True)
class GoogleDriveFile:
    file_id: str
    name: str
    mime_type: str
    web_view_link: str
    comments: tuple[GoogleDriveComment, ...]


@dataclass
class GoogleDriveSyncResult:
    created_tasks: list[Task] = field(default_factory=list)
    updated_tasks: list[Task] = field(default_factory=list)
    deleted_tasks: list[Task] = field(default_factory=list)
    scanned: int = 0
    full_sync: bool = False
    group_changed: bool = False

    def as_dict(self) -> dict:
        return {
            "created": len(self.created_tasks),
            "updated": len(self.updated_tasks),
            "removed": len(self.deleted_tasks),
            "total": len(self.created_tasks) + len(self.updated_tasks),
            "scanned": self.scanned,
            "full_sync": self.full_sync,
        }


def extract_google_drive_file_id(value: str) -> str:
    raw = str(value or "").strip()
    if _FILE_ID_PATTERN.fullmatch(raw):
        return raw
    parsed = urlparse(raw)
    hostname = str(parsed.hostname or "").lower()
    if parsed.scheme != "https" or hostname not in {
        "drive.google.com",
        "docs.google.com",
        "sheets.google.com",
        "slides.google.com",
    }:
        raise GoogleDriveSyncError("Use a Google Drive, Docs, Sheets, or Slides URL.")
    parts = [part for part in parsed.path.split("/") if part]
    file_id = ""
    if "d" in parts:
        index = parts.index("d")
        file_id = parts[index + 1] if index + 1 < len(parts) else ""
    if not file_id:
        file_id = str((parse_qs(parsed.query).get("id") or [""])[0]).strip()
    if not _FILE_ID_PATTERN.fullmatch(file_id):
        raise GoogleDriveSyncError("The Google Drive URL does not contain a valid file ID.")
    return file_id


def _parse_google_datetime(value) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _google_timestamp(value: datetime) -> str:
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _drive_get(account: CalendarAccount, path: str, *, params: dict | None = None) -> dict:
    try:
        token = ensure_google_access_token(account, required_scope=GOOGLE_DRIVE_SCOPE)
    except GoogleOAuthError as exc:
        raise GoogleDriveAuthorizationRequired(str(exc)) from exc

    url = GOOGLE_DRIVE_API_ROOT + path
    for attempt in range(2):
        try:
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params=params,
                timeout=(5, 20),
            )
        except requests.RequestException as exc:
            raise GoogleDriveSyncError("Could not connect to Google Drive.") from exc
        if response.status_code != 401 or attempt:
            break
        account.access_token = None
        account.token_expires_at = None
        try:
            token = ensure_google_access_token(account, required_scope=GOOGLE_DRIVE_SCOPE)
        except GoogleOAuthError as exc:
            raise GoogleDriveAuthorizationRequired(str(exc)) from exc

    if response.status_code in {401, 403}:
        raise GoogleDriveAuthorizationRequired(
            "Termin cannot access this file. Reconnect Google Drive, then choose the file with Google Picker."
        )
    if response.status_code == 404:
        raise GoogleDriveSyncError("The Google Drive file no longer exists or is not accessible.")
    if response.status_code == 429 or response.status_code >= 500:
        raise GoogleDriveSyncError("Google Drive is temporarily unavailable. Termin will retry automatically.")
    if response.status_code >= 400:
        raise GoogleDriveSyncError(f"Google Drive returned HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise GoogleDriveSyncError("Google Drive returned invalid data.") from exc
    if not isinstance(payload, dict):
        raise GoogleDriveSyncError("Google Drive returned invalid data.")
    return payload


def fetch_google_drive_file(
    account: CalendarAccount,
    file_id: str,
    *,
    modified_since: datetime | None = None,
) -> GoogleDriveFile:
    normalized_file_id = extract_google_drive_file_id(file_id)
    file_payload = _drive_get(
        account,
        f"/files/{quote(normalized_file_id, safe='')}",
        params={"fields": "id,name,mimeType,webViewLink"},
    )
    name = str(file_payload.get("name") or "Google Drive file").strip()[:255]
    web_view_link = str(file_payload.get("webViewLink") or "").strip()
    comments: list[GoogleDriveComment] = []
    page_token = ""
    for _ in range(100):
        params = {
            "includeDeleted": "true",
            "pageSize": "100",
            "fields": (
                "nextPageToken,comments(id,content,createdTime,modifiedTime,resolved,deleted,"
                "quotedFileContent(value,mimeType),author(displayName,photoLink,me),assigneeEmailAddress,"
                "replies(id,content,createdTime,modifiedTime,deleted,author(displayName,photoLink,me)))"
            ),
        }
        if modified_since:
            params["startModifiedTime"] = _google_timestamp(modified_since)
        if page_token:
            params["pageToken"] = page_token
        payload = _drive_get(
            account,
            f"/files/{quote(normalized_file_id, safe='')}/comments",
            params=params,
        )
        rows = payload.get("comments") if isinstance(payload.get("comments"), list) else []
        for row in rows:
            if not isinstance(row, dict) or not str(row.get("id") or "").strip():
                continue
            author = row.get("author") if isinstance(row.get("author"), dict) else {}
            quoted_content = row.get("quotedFileContent") if isinstance(row.get("quotedFileContent"), dict) else {}
            replies = tuple(reply for reply in (row.get("replies") or []) if isinstance(reply, dict))
            comments.append(
                GoogleDriveComment(
                    comment_id=str(row.get("id")).strip(),
                    content=str(row.get("content") or "").strip(),
                    created_at=_parse_google_datetime(row.get("createdTime")),
                    modified_at=_parse_google_datetime(row.get("modifiedTime")),
                    resolved=bool(row.get("resolved")),
                    deleted=bool(row.get("deleted")),
                    assignee_email=normalize_email(row.get("assigneeEmailAddress")),
                    author_name=str(author.get("displayName") or "").strip(),
                    author_avatar_url=str(author.get("photoLink") or "").strip(),
                    quoted_content=str(quoted_content.get("value") or "").strip(),
                    replies=replies,
                )
            )
        page_token = str(payload.get("nextPageToken") or "").strip()
        if not page_token:
            break
    else:
        raise GoogleDriveSyncError("This file has too many pages of comments to import.")

    comments.sort(key=lambda comment: (comment.created_at or datetime.min, comment.comment_id))
    return GoogleDriveFile(
        file_id=normalized_file_id,
        name=name or "Google Drive file",
        mime_type=str(file_payload.get("mimeType") or "").strip(),
        web_view_link=web_view_link,
        comments=tuple(comments),
    )


def _comment_title(comment: GoogleDriveComment) -> str:
    for line in comment.content.splitlines():
        normalized = " ".join(line.split())
        if normalized:
            return normalized[:255]
    return "Google Drive comment"


def _comment_description(comment: GoogleDriveComment) -> str:
    parts = [comment.content.strip()]
    if comment.quoted_content:
        quoted = "\n".join("> " + line for line in comment.quoted_content.splitlines())
        parts.append("Referenced text:\n\n" + quoted)
    reply_lines: list[str] = []
    for reply in comment.replies:
        if reply.get("deleted"):
            continue
        content = str(reply.get("content") or "").strip()
        if not content:
            continue
        author = reply.get("author") if isinstance(reply.get("author"), dict) else {}
        author_name = str(author.get("displayName") or "Reply").strip()
        reply_lines.append(f"**{author_name}:** {content}")
    if reply_lines:
        parts.append("Replies:\n\n" + "\n\n".join(reply_lines))
    return "\n\n".join(part for part in parts if part).strip()


def _comment_info(comment: GoogleDriveComment, drive_file: GoogleDriveFile) -> str:
    meta = {
        "due_mode": "none",
        "google_drive": {
            "file_id": drive_file.file_id,
            "file_name": drive_file.name,
            "comment_id": comment.comment_id,
            "comment_modified_at": comment.modified_at.isoformat() if comment.modified_at else None,
            "resolved": comment.resolved,
            "assignee_email": comment.assignee_email or None,
            "author_name": comment.author_name or None,
            "author_avatar_url": comment.author_avatar_url or None,
        },
    }
    if not comment.assignee_email:
        meta["assignee_mode"] = "none"
    links = [drive_file.web_view_link] if drive_file.web_view_link else []
    return normalize_info_payload({"links": links, "meta": meta}, drive_file.web_view_link or None)


def _assignment_signature(task_id: int) -> tuple[tuple[int | None, str, str], ...]:
    rows = Assignment.query.filter_by(task_id=task_id).order_by(Assignment.id.asc()).all()
    return tuple((row.user_id, normalize_email(row.email), str(row.status or "assigned")) for row in rows)


def _sync_comment_assignee(task: Task, email: str) -> bool:
    normalized = normalize_email(email)
    matched_user = find_user_by_email(normalized) if normalized else None
    desired = ((matched_user.id if matched_user else None, "" if matched_user else normalized, "assigned"),) if normalized else ()
    if _assignment_signature(task.id) == desired:
        return False
    Assignment.query.filter_by(task_id=task.id).delete(synchronize_session=False)
    if normalized:
        db.session.add(
            Assignment(
                task_id=task.id,
                user_id=matched_user.id if matched_user else None,
                email=None if matched_user else normalized,
                status="assigned",
            )
        )
    return True


def _delete_drive_task(task: Task, link: GoogleDriveCommentLink) -> None:
    task_id = int(task.id)
    TaskNotification.query.filter_by(task_id=task_id).delete(synchronize_session=False)
    TaskComment.query.filter_by(task_id=task_id).delete(synchronize_session=False)
    TaskFollower.query.filter_by(task_id=task_id).delete(synchronize_session=False)
    TaskUserStatus.query.filter_by(task_id=task_id).delete(synchronize_session=False)
    TaskCollaboratorStatus.query.filter_by(task_id=task_id).delete(synchronize_session=False)
    CollaboratorTaskRead.query.filter_by(task_id=task_id).delete(synchronize_session=False)
    ExternalEvent.query.filter_by(task_id=task_id).delete(synchronize_session=False)
    UserDiscussionActivity.query.filter_by(task_id=task_id).delete(synchronize_session=False)
    UserDiscussionActivity.query.filter_by(entity_type="task", entity_id=task_id).delete(synchronize_session=False)
    DiscussionEvent.query.filter_by(task_id=task_id).delete(synchronize_session=False)
    DiscussionEvent.query.filter_by(entity_type="task", entity_id=task_id).delete(synchronize_session=False)
    Invite.query.filter_by(task_id=task_id).delete(synchronize_session=False)
    Assignment.query.filter_by(task_id=task_id).delete(synchronize_session=False)
    TaskPrerequisite.query.filter(
        (TaskPrerequisite.task_id == task_id) | (TaskPrerequisite.prerequisite_task_id == task_id)
    ).delete(synchronize_session=False)
    db.session.delete(link)
    db.session.delete(task)


def apply_google_drive_file(
    group: Group,
    integration: GoogleDriveIntegration,
    drive_file: GoogleDriveFile,
    *,
    actor_user_id: int,
    full_sync: bool,
) -> GoogleDriveSyncResult:
    if not group.id:
        db.session.flush()
    now = datetime.utcnow()
    existing_links = {
        row.comment_id: row
        for row in GoogleDriveCommentLink.query.filter_by(group_id=group.id).all()
    }
    result = GoogleDriveSyncResult(scanned=len(drive_file.comments), full_sync=full_sync)
    seen_ids: set[str] = set()
    max_position = db.session.query(db.func.max(Task.position)).filter_by(group_id=group.id).scalar() or 0

    for index, comment in enumerate(drive_file.comments, start=1):
        seen_ids.add(comment.comment_id)
        link = existing_links.get(comment.comment_id)
        task = db.session.get(Task, link.task_id) if link else None
        if comment.deleted:
            if task and link:
                result.deleted_tasks.append(task)
                _delete_drive_task(task, link)
            elif link:
                db.session.delete(link)
            continue

        created = task is None
        if created:
            max_position += 1
            task = Task(
                project_id=group.project_id,
                group_id=group.id,
                creator_user_id=actor_user_id,
                position=index if full_sync else max_position,
                title=_comment_title(comment),
                status="complete" if comment.resolved else "open",
                status_mode="single",
                owner_calendar_opt_in=False,
            )
            db.session.add(task)
            db.session.flush()
            if link:
                link.task_id = task.id
            else:
                link = GoogleDriveCommentLink(
                    group_id=group.id,
                    task_id=task.id,
                    comment_id=comment.comment_id,
                    last_seen_at=now,
                )
                db.session.add(link)

        desired = {
            "project_id": group.project_id,
            "group_id": group.id,
            "position": index if full_sync else task.position,
            "title": _comment_title(comment),
            "description": _comment_description(comment) or None,
            "description_format": "markdown",
            "info": _comment_info(comment, drive_file),
            "link": drive_file.web_view_link or None,
            "due_at": None,
            "locked": True,
            "status": "complete" if comment.resolved else "open",
            "status_mode": "single",
            "per_user_status_enabled": False,
            "assign_group_members": False,
            "owner_calendar_opt_in": False,
        }
        changed = created
        for attribute, value in desired.items():
            if getattr(task, attribute) != value:
                setattr(task, attribute, value)
                changed = True
        if _sync_comment_assignee(task, comment.assignee_email):
            changed = True
        if changed:
            task.updated_at = now
            if created:
                result.created_tasks.append(task)
            else:
                result.updated_tasks.append(task)
        link.comment_modified_at = comment.modified_at
        link.last_seen_at = now

    if full_sync:
        for comment_id, link in existing_links.items():
            if comment_id in seen_ids:
                continue
            task = db.session.get(Task, link.task_id)
            if task:
                result.deleted_tasks.append(task)
                _delete_drive_task(task, link)
            else:
                db.session.delete(link)

    old_group_name = group.name
    old_source_url = group.specialty_source_url
    old_sync_error = group.specialty_sync_error
    old_group_updated_at = group.updated_at
    group.name = drive_file.name
    group.specialty_type = GOOGLE_DRIVE_SPECIALTY_TYPE
    group.specialty_source_url = drive_file.web_view_link or group.specialty_source_url
    group.specialty_last_synced_at = now
    group.specialty_last_sync_attempt_at = now
    group.specialty_sync_error = None
    integration.file_name = drive_file.name
    integration.mime_type = drive_file.mime_type
    integration.web_view_link = drive_file.web_view_link or integration.web_view_link
    integration.updated_at = now
    if full_sync:
        integration.last_full_synced_at = now
    result.group_changed = (
        old_group_name != group.name
        or old_source_url != group.specialty_source_url
        or bool(old_sync_error)
    )
    if result.group_changed:
        group.updated_at = now
    elif old_group_updated_at is not None:
        group.updated_at = old_group_updated_at
        flag_modified(group, "updated_at")
    return result


def refresh_google_drive_group(group: Group, *, full_sync: bool | None = None) -> GoogleDriveSyncResult:
    if group.specialty_type != GOOGLE_DRIVE_SPECIALTY_TYPE:
        raise GoogleDriveSyncError("This is not a Google Drive group.")
    integration = GoogleDriveIntegration.query.filter_by(group_id=group.id).first()
    if not integration:
        raise GoogleDriveSyncError("Google Drive connection data is missing for this group.")
    account = CalendarAccount.query.filter_by(
        user_id=integration.authorized_user_id,
        provider="google",
    ).first()
    if not account:
        raise GoogleDriveAuthorizationRequired("Reconnect Google Drive to refresh this group.")
    group_id = int(group.id)
    with _active_group_lock:
        if group_id in _active_group_ids:
            raise GoogleDriveSyncBusy("This Google Drive group is already refreshing.")
        _active_group_ids.add(group_id)
    try:
        now = datetime.utcnow()
        do_full_sync = full_sync if full_sync is not None else (
            integration.last_full_synced_at is None
            or integration.last_full_synced_at <= now - GOOGLE_DRIVE_FULL_SYNC_INTERVAL
        )
        modified_since = None
        if not do_full_sync and group.specialty_last_synced_at:
            modified_since = group.specialty_last_synced_at - GOOGLE_DRIVE_SYNC_OVERLAP
        try:
            drive_file = fetch_google_drive_file(account, integration.file_id, modified_since=modified_since)
        except GoogleDriveSyncError as exc:
            attempted_at = datetime.utcnow()
            next_error = str(exc)[:2000]
            old_group_updated_at = group.updated_at
            group.specialty_last_sync_attempt_at = attempted_at
            if str(group.specialty_sync_error or "") != next_error:
                group.specialty_sync_error = next_error
                group.updated_at = attempted_at
            elif old_group_updated_at is not None:
                group.updated_at = old_group_updated_at
                flag_modified(group, "updated_at")
            db.session.commit()
            raise
        result = apply_google_drive_file(
            group,
            integration,
            drive_file,
            actor_user_id=integration.authorized_user_id,
            full_sync=do_full_sync,
        )
        db.session.commit()
        return result
    finally:
        with _active_group_lock:
            _active_group_ids.discard(group_id)


def google_drive_group_metadata(group: Group) -> dict:
    if str(getattr(group, "specialty_type", None) or "").strip().lower() != GOOGLE_DRIVE_SPECIALTY_TYPE:
        return {}
    integration = GoogleDriveIntegration.query.filter_by(group_id=group.id).first()
    last_synced = getattr(group, "specialty_last_synced_at", None)
    last_attempted = getattr(group, "specialty_last_sync_attempt_at", None)
    interval_seconds = int(current_app.config.get("GOOGLE_DRIVE_POLL_INTERVAL_SECONDS", 30))
    return {
        "google_drive": {
            "file_id": integration.file_id if integration else None,
            "file_name": integration.file_name if integration else group.name,
            "mime_type": integration.mime_type if integration else None,
            "source_url": (integration.web_view_link if integration else None) or group.specialty_source_url,
            "authorized_user_id": integration.authorized_user_id if integration else None,
            "last_synced_at": last_synced.isoformat() if last_synced else None,
            "last_attempted_at": last_attempted.isoformat() if last_attempted else None,
            "last_full_synced_at": (
                integration.last_full_synced_at.isoformat()
                if integration and integration.last_full_synced_at
                else None
            ),
            "next_sync_at": (last_attempted + timedelta(seconds=interval_seconds)).isoformat() if last_attempted else None,
            "sync_error": getattr(group, "specialty_sync_error", None),
            "refresh_interval_seconds": interval_seconds,
        }
    }


def emit_google_drive_sync_result(group: Group, result: GoogleDriveSyncResult, *, emit_group: bool = True) -> None:
    from app.realtime import emit_group_updated, emit_task_updated, emit_tasks_updated

    actor_user_id = GoogleDriveIntegration.query.filter_by(group_id=group.id).with_entities(
        GoogleDriveIntegration.authorized_user_id
    ).scalar()
    if emit_group or result.group_changed:
        emit_group_updated(group, actor_user_id=actor_user_id)
    changed_tasks = result.created_tasks + result.updated_tasks
    if changed_tasks:
        emit_tasks_updated(changed_tasks, action="google_drive_synced", actor_user_id=actor_user_id)
    for task in result.deleted_tasks:
        emit_task_updated(
            task,
            action="deleted",
            old_project_id=task.project_id,
            old_group_id=task.group_id,
            actor_user_id=actor_user_id,
        )


def poll_google_drive_integrations() -> int:
    interval_seconds = max(int(current_app.config.get("GOOGLE_DRIVE_POLL_INTERVAL_SECONDS", 30)), 10)
    cutoff = datetime.utcnow() - timedelta(seconds=interval_seconds)
    integration_ids = [
        row[0]
        for row in db.session.query(GoogleDriveIntegration.id)
        .join(Group, Group.id == GoogleDriveIntegration.group_id)
        .filter(
            Group.specialty_type == GOOGLE_DRIVE_SPECIALTY_TYPE,
            (
                Group.specialty_last_sync_attempt_at.is_(None)
                | (Group.specialty_last_sync_attempt_at <= cutoff)
            ),
        )
        .all()
    ]
    refreshed = 0
    for integration_id in integration_ids:
        integration = db.session.get(GoogleDriveIntegration, integration_id)
        group = db.session.get(Group, integration.group_id) if integration else None
        if not group:
            continue
        previous_error = str(group.specialty_sync_error or "")
        try:
            result = refresh_google_drive_group(group)
            emit_google_drive_sync_result(group, result, emit_group=False)
            refreshed += 1
        except GoogleDriveSyncBusy:
            continue
        except GoogleDriveSyncError:
            if str(group.specialty_sync_error or "") != previous_error:
                from app.realtime import emit_group_updated

                emit_group_updated(group, actor_user_id=integration.authorized_user_id)
            current_app.logger.warning("Google Drive sync failed for group %s", group.id, exc_info=True)
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Unexpected Google Drive sync failure for group %s", group.id)
    return refreshed


def start_google_drive_poll_worker(app, socketio) -> None:
    global _worker_started
    if _worker_started or app.config.get("TESTING"):
        return
    if not app.config.get("GOOGLE_CLIENT_ID") or not app.config.get("GOOGLE_CLIENT_SECRET"):
        return
    _worker_started = True
    interval_seconds = max(int(app.config.get("GOOGLE_DRIVE_POLL_INTERVAL_SECONDS", 30)), 10)

    def runner() -> None:
        socketio.sleep(interval_seconds)
        while True:
            with app.app_context():
                poll_google_drive_integrations()
            socketio.sleep(interval_seconds)

    socketio.start_background_task(runner)
