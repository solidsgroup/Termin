import json
import re
import uuid
from datetime import datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from werkzeug.utils import secure_filename


ALLOWED_TAGS = {"a", "b", "blockquote", "br", "code", "em", "h1", "h2", "h3", "h4", "h5", "h6", "i", "li", "ol", "p", "pre", "span", "strong", "u", "ul"}
ALLOWED_ATTRS = {
    "a": {"href", "target", "rel"},
    "span": {"class", "data-mentioned-user-id", "data-mentioned-email"},
}
ALLOWED_META_KEYS = {"due_mode", "due_relative_days", "due_relative_start_days", "due_relative_task_id", "follow_project_members", "gantt_ranges", "poll", "start_date", "status_mode", "status_percentage", "task_type"}


def _normalize_gantt_ranges(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    ranges = []
    seen_ids = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        start = str(item.get("start") or "").strip()[:10]
        end = str(item.get("end") or "").strip()[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", start) or not re.match(r"^\d{4}-\d{2}-\d{2}$", end):
            continue
        if end < start:
            start, end = end, start
        range_id = str(item.get("id") or f"range-{index}").strip()
        if not range_id or range_id in seen_ids:
            range_id = uuid.uuid4().hex[:12]
        seen_ids.add(range_id)
        color = str(item.get("color") or "").strip()
        if not re.match(r"^#[0-9a-fA-F]{6}$", color):
            color = "#4cc9f0"
        ranges.append({
            "id": range_id,
            "label": str(item.get("label") or "Range").strip() or "Range",
            "start": start,
            "end": end,
            "color": color,
        })
    return ranges


def _normalize_poll_meta(value) -> dict:
    poll = value if isinstance(value, dict) else {}
    question = str(poll.get("question") or "").strip()
    allows_multiple = bool(poll.get("allows_multiple"))
    closed = bool(poll.get("closed"))
    results_visibility = str(poll.get("results_visibility") or "everyone").strip().lower()
    if results_visibility not in {"everyone", "creator"}:
        results_visibility = "everyone"
    options = []
    seen_option_ids = set()
    raw_options = poll.get("options")
    if isinstance(raw_options, list):
        for item in raw_options:
            if isinstance(item, dict):
                label = str(item.get("label") or "").strip()
                option_id = str(item.get("id") or "").strip()
            else:
                label = str(item or "").strip()
                option_id = ""
            if not label:
                continue
            if not option_id or option_id in seen_option_ids:
                option_id = uuid.uuid4().hex[:8]
                while option_id in seen_option_ids:
                    option_id = uuid.uuid4().hex[:8]
            seen_option_ids.add(option_id)
            options.append({"id": option_id, "label": label})
    valid_option_ids = {str(item.get("id") or "").strip() for item in options if str(item.get("id") or "").strip()}
    responses = []
    seen_response_keys = set()
    raw_responses = poll.get("responses")
    if isinstance(raw_responses, list):
        for item in raw_responses:
            if not isinstance(item, dict):
                continue
            user_id_raw = item.get("user_id")
            try:
                user_id = int(user_id_raw) if user_id_raw not in (None, "", "null") else None
            except (TypeError, ValueError):
                user_id = None
            email = str(item.get("email") or "").strip().lower()
            key = f"user:{user_id}" if user_id is not None else (f"email:{email}" if email else "")
            if not key or key in seen_response_keys:
                continue
            selected_option_ids = []
            raw_option_ids = item.get("option_ids")
            if isinstance(raw_option_ids, list):
                for option_id in raw_option_ids:
                    normalized_option_id = str(option_id or "").strip()
                    if not normalized_option_id or normalized_option_id not in valid_option_ids:
                        continue
                    if normalized_option_id not in selected_option_ids:
                        selected_option_ids.append(normalized_option_id)
            if not allows_multiple and len(selected_option_ids) > 1:
                selected_option_ids = selected_option_ids[:1]
            seen_response_keys.add(key)
            responses.append(
                {
                    "user_id": user_id,
                    "email": email,
                    "option_ids": selected_option_ids,
                    "updated_at": str(item.get("updated_at") or "").strip() or datetime.utcnow().isoformat(timespec="seconds"),
                }
            )
    return {
        "question": question,
        "allows_multiple": allows_multiple,
        "closed": closed,
        "results_visibility": results_visibility,
        "options": options,
        "responses": responses,
    }


class _InfoSanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            return
        clean_attrs = []
        allowed = ALLOWED_ATTRS.get(tag, set())
        for key, value in attrs:
            if key not in allowed or value is None:
                continue
            if tag == "a" and key == "href":
                parsed = urlparse(value)
                if parsed.scheme not in {"http", "https", "mailto"}:
                    continue
            clean_attrs.append((key, escape(value, quote=True)))
        attr_text = "".join(f' {key}="{value}"' for key, value in clean_attrs)
        self.parts.append(f"<{tag}{attr_text}>")

    def handle_endtag(self, tag):
        if tag in ALLOWED_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(escape(data))

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def get_html(self):
        return "".join(self.parts)


def sanitize_info_html(value: str | None) -> str:
    if not value:
        return ""
    parser = _InfoSanitizer()
    parser.feed(value)
    parser.close()
    return parser.get_html().strip()


def normalize_info_payload(payload, legacy_link: str | None = None) -> str | None:
    if payload is None:
        if legacy_link:
            payload = {"html": f'<p><a href="{escape(legacy_link, quote=True)}">{escape(legacy_link)}</a></p>', "attachments": []}
        else:
            return None
    if isinstance(payload, str):
        payload = {"html": payload, "attachments": []}

    html = sanitize_info_html((payload or {}).get("html"))
    links = []
    for item in (payload or {}).get("links", []):
        if not item:
            continue
        link = str(item).strip()
        parsed = urlparse(link)
        if parsed.scheme not in {"http", "https"}:
            continue
        if link not in links:
            links.append(link)
    if not links and legacy_link:
        parsed_legacy = urlparse(legacy_link)
        if parsed_legacy.scheme in {"http", "https"}:
            links.append(legacy_link)
    attachments = []
    for item in (payload or {}).get("attachments", []):
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        path = item.get("path")
        name = item.get("name")
        if not url or not path or not name:
            continue
        attachments.append(
            {
                "id": item.get("id") or uuid.uuid4().hex,
                "name": str(name),
                "url": str(url),
                "path": str(path),
                "size": int(item.get("size") or 0),
                "uploaded_at": item.get("uploaded_at") or datetime.utcnow().isoformat(timespec="seconds"),
            }
        )

    meta = {}
    raw_meta = (payload or {}).get("meta")
    if isinstance(raw_meta, dict):
        for key, value in raw_meta.items():
            if key not in ALLOWED_META_KEYS or value is None:
                continue
            if key == "task_type":
                text = str(value).strip().lower()
                if text in {"standard", "poll"}:
                    meta[key] = text
                continue
            if key == "poll":
                meta[key] = _normalize_poll_meta(value)
                continue
            if key == "gantt_ranges":
                ranges = _normalize_gantt_ranges(value)
                if ranges:
                    meta[key] = ranges
                continue
            text = str(value).strip()
            if not text:
                continue
            meta[key] = text

    if not html and not attachments and not links and not meta:
        return None

    return json.dumps({"html": html, "attachments": attachments, "links": links, "meta": meta}, separators=(",", ":"))


def load_info_payload(raw_value: str | None, legacy_link: str | None = None) -> dict:
    if raw_value:
        try:
            data = json.loads(raw_value)
        except json.JSONDecodeError:
            data = {"html": sanitize_info_html(raw_value), "attachments": []}
    elif legacy_link:
        data = {"html": f'<p><a href="{escape(legacy_link, quote=True)}">{escape(legacy_link)}</a></p>', "attachments": [], "links": [legacy_link]}
    else:
        data = {"html": "", "attachments": [], "links": []}
    data["html"] = sanitize_info_html(data.get("html"))
    data["attachments"] = [item for item in data.get("attachments", []) if isinstance(item, dict)]
    links = []
    for item in data.get("links", []):
        if not item:
            continue
        link = str(item).strip()
        parsed = urlparse(link)
        if parsed.scheme not in {"http", "https"}:
            continue
        if link not in links:
            links.append(link)
    if not links and legacy_link:
        parsed_legacy = urlparse(legacy_link)
        if parsed_legacy.scheme in {"http", "https"}:
            links.append(legacy_link)
    data["links"] = links
    raw_meta = data.get("meta")
    meta = {}
    if isinstance(raw_meta, dict):
        for key, value in raw_meta.items():
            if key not in ALLOWED_META_KEYS or value is None:
                continue
            if key == "task_type":
                text = str(value).strip().lower()
                if text in {"standard", "poll"}:
                    meta[key] = text
                continue
            if key == "poll":
                meta[key] = _normalize_poll_meta(value)
                continue
            if key == "gantt_ranges":
                ranges = _normalize_gantt_ranges(value)
                if ranges:
                    meta[key] = ranges
                continue
            text = str(value).strip()
            if not text:
                continue
            meta[key] = text
    data["meta"] = meta
    return data


def save_uploaded_file(file_storage, upload_root: Path, item_type: str, item_id: int) -> dict:
    original_name = secure_filename(file_storage.filename or "upload")
    ext = Path(original_name).suffix
    safe_name = f"{uuid.uuid4().hex}{ext}"
    target_dir = upload_root / item_type / str(item_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_name
    file_storage.save(target_path)
    return {
        "id": uuid.uuid4().hex,
        "name": original_name or safe_name,
        "path": str(target_path.relative_to(upload_root.parent)),
        "url": f"/uploads/{item_type}/{item_id}/{safe_name}",
        "size": target_path.stat().st_size,
        "uploaded_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
