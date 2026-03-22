import json
import uuid
from datetime import datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from werkzeug.utils import secure_filename


ALLOWED_TAGS = {"a", "b", "blockquote", "br", "code", "em", "i", "li", "ol", "p", "pre", "strong", "u", "ul"}
ALLOWED_ATTRS = {"a": {"href", "target", "rel"}}


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

    if not html and not attachments and not links:
        return None

    return json.dumps({"html": html, "attachments": attachments, "links": links}, separators=(",", ":"))


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
