from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import ipaddress
import json
import re
import socket
from threading import Lock, Thread
from urllib.parse import quote, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from app.extensions import db
from app.info_utils import normalize_info_payload
from app.models import (
    Assignment,
    CanvasAssignmentLink,
    CollaboratorTaskRead,
    DiscussionEvent,
    ExternalEvent,
    Group,
    Invite,
    Task,
    TaskCollaboratorStatus,
    TaskComment,
    TaskFollower,
    TaskNotification,
    TaskPrerequisite,
    TaskUserStatus,
    User,
    UserDiscussionActivity,
)
from app.team_shares import accessible_project_ids_for_user


CANVAS_REFRESH_INTERVAL = timedelta(hours=24)
CANVAS_SPECIALTY_TYPE = "canvas"
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_MAX_API_PAGES = 20
_MAX_ASSIGNMENTS = 5000
_COURSE_ASSIGNMENTS_PATH = re.compile(r"^/courses/([^/]+)/assignments/?$")
_COURSE_TIMEZONE = re.compile(r'"(?:CONTEXT_)?TIMEZONE"\s*:\s*"([^"]+)"', re.IGNORECASE)
_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?', re.IGNORECASE)
_background_lock = Lock()
_background_user_ids: set[int] = set()
_active_group_ids: set[int] = set()


class CanvasSyncError(RuntimeError):
    pass


class CanvasSyncBusy(CanvasSyncError):
    pass


@dataclass(frozen=True)
class CanvasAssignment:
    assignment_id: str
    title: str
    description: str
    due_at: datetime | None
    html_url: str
    links: tuple[str, ...]
    position: int
    assignment_group_id: str = ""
    assignment_group_name: str = ""
    points_possible: float | None = None
    unlock_at: str | None = None
    lock_at: str | None = None
    start_at: datetime | None = None
    source_assignment_id: str = ""
    override_id: str = ""
    override_title: str = ""
    submission_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class CanvasCourse:
    source_url: str
    name: str
    course_id: str
    assignments: tuple[CanvasAssignment, ...]


@dataclass
class CanvasSyncResult:
    created_tasks: list[Task] = field(default_factory=list)
    updated_tasks: list[Task] = field(default_factory=list)
    deleted_tasks: list[Task] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "created": len(self.created_tasks),
            "updated": len(self.updated_tasks),
            "removed": len(self.deleted_tasks),
            "total": len(self.created_tasks) + len(self.updated_tasks),
        }


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_title = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


class _LinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "a":
            return
        href = next((value for name, value in attrs if name.lower() == "href"), None)
        if not href:
            return
        absolute = urljoin(self.base_url, str(href).strip())
        if urlparse(absolute).scheme.lower() in {"http", "https"} and absolute not in self.links:
            self.links.append(absolute)


def _validated_canvas_source_url(raw_url: str) -> tuple[str, str, str]:
    value = str(raw_url or "").strip()
    if not value:
        raise CanvasSyncError("A public Canvas assignments URL is required.")
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise CanvasSyncError("Canvas URL must be a public HTTPS URL.")
    match = _COURSE_ASSIGNMENTS_PATH.match(parsed.path)
    if not match:
        raise CanvasSyncError("Use a Canvas course assignments URL ending in /courses/<course-id>/assignments.")
    course_id = match.group(1)
    if not course_id or len(course_id) > 255:
        raise CanvasSyncError("Canvas course ID is invalid.")
    netloc = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise CanvasSyncError("Canvas URL has an invalid port.") from exc
    if port and port != 443:
        netloc = f"{netloc}:{port}"
    normalized = urlunparse(("https", netloc, parsed.path.rstrip("/"), "", "", ""))
    return normalized, parsed.hostname.lower(), course_id


def _assert_public_host(hostname: str) -> None:
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
            if item and item[4]
        }
    except OSError as exc:
        raise CanvasSyncError("Canvas host could not be resolved.") from exc
    if not addresses:
        raise CanvasSyncError("Canvas host could not be resolved.")
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_global:
                raise CanvasSyncError("Canvas URL must resolve to a public host.")
        except ValueError as exc:
            raise CanvasSyncError("Canvas host returned an invalid address.") from exc


def _read_response(response, *, label: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > _MAX_RESPONSE_BYTES:
                raise CanvasSyncError(f"Canvas {label} response was too large.")
            chunks.append(chunk)
    except requests.RequestException as exc:
        raise CanvasSyncError(f"Canvas {label} response was interrupted.") from exc
    return b"".join(chunks)


def _session_get(session: requests.Session, url: str, *, accept: str) -> tuple[object, bytes, str]:
    current_url = url
    for _ in range(6):
        parsed = urlparse(current_url)
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise CanvasSyncError("Canvas redirected to an invalid URL.")
        _assert_public_host(parsed.hostname)
        try:
            response = session.get(
                current_url,
                headers={"Accept": accept},
                timeout=(4, 15),
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            raise CanvasSyncError("Could not connect to Canvas.") from exc
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise CanvasSyncError("Canvas returned an invalid redirect.")
            current_url = urljoin(current_url, location)
            continue
        try:
            body = _read_response(response, label="page" if "html" in accept else "API")
        except Exception:
            response.close()
            raise
        if response.status_code >= 400:
            status_code = response.status_code
            response.close()
            if status_code in {401, 403}:
                raise CanvasSyncError("This Canvas assignments page is not publicly accessible.")
            raise CanvasSyncError(f"Canvas returned HTTP {status_code}.")
        return response, body, current_url
    raise CanvasSyncError("Canvas redirected too many times.")


def _course_name_from_html(body: bytes, course_id: str) -> str:
    parser = _PageParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    title = " ".join("".join(parser.title_parts).split())
    if title.lower().startswith("assignments:"):
        title = title.split(":", 1)[1].strip()
    return title[:255] or f"Canvas Course {course_id}"


def _course_timezone_from_html(body: bytes):
    match = _COURSE_TIMEZONE.search(body.decode("utf-8", errors="replace"))
    if not match:
        return timezone.utc
    try:
        return ZoneInfo(match.group(1))
    except ZoneInfoNotFoundError:
        return timezone.utc


def _parse_canvas_datetime(value, course_timezone) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(course_timezone).replace(tzinfo=None)
    return parsed


def _assignment_links(description: str, html_url: str, source_url: str) -> tuple[str, ...]:
    links: list[str] = []
    normalized_assignment_url = urljoin(source_url, html_url) if html_url else ""
    if normalized_assignment_url and urlparse(normalized_assignment_url).scheme.lower() in {"http", "https"}:
        links.append(normalized_assignment_url)
    parser = _LinkParser(normalized_assignment_url or source_url)
    parser.feed(description)
    for link in parser.links:
        if link not in links:
            links.append(link)
    return tuple(links)


def _next_page_url(link_header: str | None) -> str | None:
    if not link_header:
        return None
    match = _NEXT_LINK.search(link_header)
    return match.group(1) if match else None


def fetch_canvas_course(source_url: str) -> CanvasCourse:
    normalized_source, source_host, course_id = _validated_canvas_source_url(source_url)
    _assert_public_host(source_host)
    session = requests.Session()
    session.headers.update({"User-Agent": "Termin Canvas Sync/1.0"})
    page_response, page_body, final_page_url = _session_get(session, normalized_source, accept="text/html")
    final_page = urlparse(final_page_url)
    final_match = _COURSE_ASSIGNMENTS_PATH.match(final_page.path)
    if not final_page.hostname or not final_match or final_match.group(1) != course_id:
        raise CanvasSyncError("Canvas redirected away from the public assignments page.")
    canvas_host = final_page.hostname.lower()
    course_name = _course_name_from_html(page_body, course_id)
    course_timezone = _course_timezone_from_html(page_body)
    page_response.close()

    api_path = f"/api/v1/courses/{quote(course_id, safe='')}/assignment_groups"
    query = urlencode([("include[]", "assignments"), ("include[]", "overrides"), ("per_page", "100")])
    api_url = urlunparse(("https", final_page.netloc, api_path, "", query, ""))
    assignment_groups: list[dict] = []
    for _ in range(_MAX_API_PAGES):
        parsed_api = urlparse(api_url)
        if parsed_api.hostname != canvas_host or parsed_api.path != api_path:
            raise CanvasSyncError("Canvas returned an invalid pagination URL.")
        response, body, final_api_url = _session_get(session, api_url, accept="application/json")
        final_api = urlparse(final_api_url)
        if final_api.hostname != canvas_host or final_api.path != api_path:
            response.close()
            raise CanvasSyncError("Canvas redirected away from its assignments API.")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CanvasSyncError("Canvas returned invalid assignment data.") from exc
        if not isinstance(payload, list):
            raise CanvasSyncError("Canvas returned invalid assignment data.")
        assignment_groups.extend(row for row in payload if isinstance(row, dict))
        next_url = _next_page_url(response.headers.get("Link"))
        response.close()
        if not next_url:
            break
        api_url = urljoin(final_api_url, next_url)
    else:
        raise CanvasSyncError("Canvas returned too many pages of assignments.")

    def position_value(value, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    ordered_groups = sorted(
        enumerate(assignment_groups),
        key=lambda pair: (position_value(pair[1].get("position"), pair[0] + 1), pair[0]),
    )
    assignments: list[CanvasAssignment] = []
    seen_assignment_ids: set[str] = set()
    for group_index, (_, assignment_group) in enumerate(ordered_groups):
        group_id = str(assignment_group.get("id") or "")
        group_name = str(assignment_group.get("name") or "").strip()
        rows = assignment_group.get("assignments")
        if not isinstance(rows, list):
            continue
        ordered_rows = sorted(
            enumerate(row for row in rows if isinstance(row, dict)),
            key=lambda pair: (position_value(pair[1].get("position"), pair[0] + 1), pair[0]),
        )
        for row_index, (_, row) in enumerate(ordered_rows):
            source_assignment_id = str(row.get("id") or "").strip()
            title = str(row.get("name") or "").strip()
            if not source_assignment_id or not title:
                continue
            description = str(row.get("description") or "")
            html_url = str(row.get("html_url") or "").strip()
            submission_types = row.get("submission_types") if isinstance(row.get("submission_types"), list) else []
            points_possible = row.get("points_possible")
            try:
                points_possible = float(points_possible) if points_possible is not None else None
            except (TypeError, ValueError):
                points_possible = None

            raw_overrides = row.get("overrides") if isinstance(row.get("overrides"), list) else []
            active_overrides = [override for override in raw_overrides if isinstance(override, dict) and not override.get("unassign_item")]
            visible_to_everyone = bool(row.get("visible_to_everyone")) or not bool(row.get("only_visible_to_overrides"))
            variants: list[tuple[str, str, str, object, object, object]] = []
            if active_overrides:
                if visible_to_everyone:
                    variants.append((source_assignment_id, "", "", row.get("due_at"), row.get("unlock_at"), row.get("lock_at")))
                for override_index, override in enumerate(active_overrides, start=1):
                    override_id = str(override.get("id") or override_index).strip()
                    override_title = str(override.get("title") or "").strip() or f"Override {override_id}"
                    variant_id = f"{source_assignment_id}:override:{override_id}"
                    variants.append((
                        variant_id,
                        override_id,
                        override_title,
                        override.get("due_at", row.get("due_at")),
                        override.get("unlock_at", row.get("unlock_at")),
                        override.get("lock_at", row.get("lock_at")),
                    ))
            else:
                variants.append((source_assignment_id, "", "", row.get("due_at"), row.get("unlock_at"), row.get("lock_at")))

            for variant_index, (assignment_id, override_id, override_title, due_raw, unlock_raw, lock_raw) in enumerate(variants):
                if assignment_id in seen_assignment_ids:
                    continue
                seen_assignment_ids.add(assignment_id)
                display_title = title if not override_title else f"{title} - {override_title}"
                assignments.append(CanvasAssignment(
                    assignment_id=assignment_id,
                    title=display_title[:255],
                    description=description,
                    due_at=_parse_canvas_datetime(due_raw, course_timezone),
                    html_url=urljoin(normalized_source, html_url) if html_url else "",
                    links=_assignment_links(description, html_url, normalized_source),
                    position=(group_index * 100000) + (row_index * 1000) + variant_index,
                    assignment_group_id=group_id,
                    assignment_group_name=group_name,
                    points_possible=points_possible,
                    unlock_at=str(unlock_raw or "").strip() or None,
                    lock_at=str(lock_raw or "").strip() or None,
                    start_at=_parse_canvas_datetime(unlock_raw, course_timezone),
                    source_assignment_id=source_assignment_id,
                    override_id=override_id,
                    override_title=override_title,
                    submission_types=tuple(str(value) for value in submission_types if value),
                ))
                if len(assignments) > _MAX_ASSIGNMENTS:
                    raise CanvasSyncError("Canvas course has too many assignments to import.")

    return CanvasCourse(
        source_url=normalized_source,
        name=course_name,
        course_id=course_id,
        assignments=tuple(assignments),
    )


def _task_info_for_assignment(assignment: CanvasAssignment) -> str:
    meta = {
        "assignee_mode": "none",
        "due_mode": "date" if assignment.due_at else "none",
        "canvas": {
            "assignment_id": assignment.source_assignment_id or assignment.assignment_id,
            "canvas_task_key": assignment.assignment_id,
            "assignment_group_id": assignment.assignment_group_id,
            "assignment_group_name": assignment.assignment_group_name,
            "override_id": assignment.override_id,
            "override_title": assignment.override_title,
            "points_possible": assignment.points_possible,
            "unlock_at": assignment.unlock_at,
            "lock_at": assignment.lock_at,
            "submission_types": list(assignment.submission_types),
        },
    }
    if assignment.start_at:
        meta["start_date"] = assignment.start_at.date().isoformat()
    return normalize_info_payload({"links": list(assignment.links), "meta": meta}, assignment.html_url or None)


def _delete_canvas_task(task: Task, link: CanvasAssignmentLink) -> None:
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


def apply_canvas_course(group: Group, course: CanvasCourse, *, actor_user_id: int) -> CanvasSyncResult:
    if not group.id:
        db.session.flush()
    now = datetime.utcnow()
    existing_links = {
        row.canvas_assignment_id: row
        for row in CanvasAssignmentLink.query.filter_by(group_id=group.id).all()
    }
    result = CanvasSyncResult()
    seen_ids: set[str] = set()

    for position, assignment in enumerate(course.assignments, start=1):
        seen_ids.add(assignment.assignment_id)
        link = existing_links.get(assignment.assignment_id)
        task = db.session.get(Task, link.task_id) if link else None
        if task is None:
            task = Task(
                project_id=group.project_id,
                group_id=group.id,
                creator_user_id=actor_user_id,
                position=position,
                title=assignment.title,
                status="open",
                owner_calendar_opt_in=False,
            )
            db.session.add(task)
            db.session.flush()
            if link is not None:
                link.task_id = task.id
                link.last_seen_at = now
            else:
                link = CanvasAssignmentLink(
                    group_id=group.id,
                    task_id=task.id,
                    canvas_assignment_id=assignment.assignment_id,
                    last_seen_at=now,
                )
                db.session.add(link)
            result.created_tasks.append(task)
        else:
            result.updated_tasks.append(task)

        task.project_id = group.project_id
        task.group_id = group.id
        task.position = position
        task.title = assignment.title
        task.description = assignment.description or None
        task.description_format = "html"
        task.info = _task_info_for_assignment(assignment)
        task.link = assignment.links[0] if assignment.links else (assignment.html_url or None)
        task.due_at = assignment.due_at
        task.locked = True
        task.assign_group_members = False
        task.updated_at = now
        link.last_seen_at = now
        Assignment.query.filter_by(task_id=task.id).delete(synchronize_session=False)

    for assignment_id, link in existing_links.items():
        if assignment_id in seen_ids:
            continue
        task = db.session.get(Task, link.task_id)
        if not task:
            db.session.delete(link)
            continue
        result.deleted_tasks.append(task)
        _delete_canvas_task(task, link)

    group.specialty_type = CANVAS_SPECIALTY_TYPE
    group.specialty_source_url = course.source_url
    group.specialty_last_synced_at = now
    group.specialty_last_sync_attempt_at = now
    group.specialty_sync_error = None
    group.updated_at = now
    return result


def refresh_canvas_group(group: Group, *, actor_user_id: int) -> CanvasSyncResult:
    if group.specialty_type != CANVAS_SPECIALTY_TYPE or not group.specialty_source_url:
        raise CanvasSyncError("This is not a Canvas group.")
    group_id = int(group.id)
    with _background_lock:
        if group_id in _active_group_ids:
            raise CanvasSyncBusy("This Canvas group is already refreshing.")
        _active_group_ids.add(group_id)
    try:
        try:
            course = fetch_canvas_course(group.specialty_source_url)
        except CanvasSyncError as exc:
            group.specialty_last_sync_attempt_at = datetime.utcnow()
            group.specialty_sync_error = str(exc)[:2000]
            group.updated_at = datetime.utcnow()
            db.session.commit()
            raise
        result = apply_canvas_course(group, course, actor_user_id=actor_user_id)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return result
    finally:
        with _background_lock:
            _active_group_ids.discard(group_id)


def canvas_group_metadata(group: Group) -> dict:
    specialty_type = str(getattr(group, "specialty_type", None) or "").strip().lower() or None
    payload = {"specialty_type": specialty_type}
    if specialty_type != CANVAS_SPECIALTY_TYPE:
        return payload
    last_synced = getattr(group, "specialty_last_synced_at", None)
    last_attempted = getattr(group, "specialty_last_sync_attempt_at", None)
    payload["canvas"] = {
        "source_url": getattr(group, "specialty_source_url", None),
        "last_synced_at": last_synced.isoformat() if last_synced else None,
        "last_attempted_at": last_attempted.isoformat() if last_attempted else None,
        "next_sync_at": (last_synced + CANVAS_REFRESH_INTERVAL).isoformat() if last_synced else None,
        "sync_error": getattr(group, "specialty_sync_error", None),
        "refresh_interval_hours": 24,
    }
    return payload


def should_sync_canvas_groups_for_user(user_id: int, *, project_ids=None) -> bool:
    project_ids = set(project_ids) if project_ids is not None else accessible_project_ids_for_user(user_id)
    if not project_ids:
        return False
    cutoff = datetime.utcnow() - CANVAS_REFRESH_INTERVAL
    return Group.query.filter(
        Group.project_id.in_(project_ids),
        Group.specialty_type == CANVAS_SPECIALTY_TYPE,
        (
            Group.specialty_last_sync_attempt_at.is_(None)
            | (Group.specialty_last_sync_attempt_at <= cutoff)
        ),
    ).first() is not None


def _emit_sync_result(group: Group, result: CanvasSyncResult, actor_user_id: int) -> None:
    from app.realtime import emit_group_updated, emit_task_updated, emit_tasks_updated

    emit_group_updated(group, actor_user_id=actor_user_id)
    changed_tasks = result.created_tasks + result.updated_tasks
    if changed_tasks:
        emit_tasks_updated(changed_tasks, action="canvas_synced", actor_user_id=actor_user_id)
    for task in result.deleted_tasks:
        emit_task_updated(
            task,
            action="deleted",
            old_project_id=task.project_id,
            old_group_id=task.group_id,
            actor_user_id=actor_user_id,
        )


def schedule_canvas_sync_for_user(app, user_id: int) -> bool:
    normalized_user_id = int(user_id)
    with _background_lock:
        if normalized_user_id in _background_user_ids:
            return False
        _background_user_ids.add(normalized_user_id)

    def worker() -> None:
        try:
            with app.app_context():
                user = db.session.get(User, normalized_user_id)
                if not user:
                    return
                project_ids = accessible_project_ids_for_user(normalized_user_id)
                cutoff = datetime.utcnow() - CANVAS_REFRESH_INTERVAL
                groups = Group.query.filter(
                    Group.project_id.in_(project_ids),
                    Group.specialty_type == CANVAS_SPECIALTY_TYPE,
                    (
                        Group.specialty_last_sync_attempt_at.is_(None)
                        | (Group.specialty_last_sync_attempt_at <= cutoff)
                    ),
                ).all() if project_ids else []
                for group in groups:
                    try:
                        result = refresh_canvas_group(group, actor_user_id=normalized_user_id)
                        _emit_sync_result(group, result, normalized_user_id)
                    except CanvasSyncBusy:
                        continue
                    except CanvasSyncError as exc:
                        app.logger.warning("Canvas sync failed for group %s: %s", group.id, exc)
        except Exception:
            app.logger.exception("Background Canvas sync failed for user %s", normalized_user_id)
        finally:
            with app.app_context():
                db.session.remove()
            with _background_lock:
                _background_user_ids.discard(normalized_user_id)

    Thread(
        target=worker,
        name=f"termin-canvas-sync-{normalized_user_id}",
        daemon=True,
    ).start()
    return True
