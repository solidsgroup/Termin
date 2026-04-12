import hashlib
import json
from pathlib import Path
import re
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlparse

import requests
from flask import Flask, Response, send_from_directory

_FAVICON_MAX_AGE_SECONDS = 60 * 60 * 24 * 30


def canonical_favicon_target(target: str) -> str:
    value = str(target or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"

class _IconLinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.icons: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "link":
            return
        attr_map = {str(key).lower(): str(value) for key, value in attrs if key and value is not None}
        rel = attr_map.get("rel", "").lower()
        href = attr_map.get("href", "").strip()
        if not href or "icon" not in rel:
            return
        self.icons.append(href)


def _apply_cache_headers(response: Response, etag_value: str) -> Response:
    response.headers["Cache-Control"] = f"public, max-age={_FAVICON_MAX_AGE_SECONDS}, immutable"
    response.headers["ETag"] = '"' + str(etag_value or "") + '"'
    return response


def placeholder_response(app: Flask):
    response = send_from_directory(app.static_folder, "icons/link-badge.svg", mimetype="image/svg+xml")
    response.headers["X-Termin-Favicon-Source"] = "placeholder"
    return _apply_cache_headers(response, "placeholder-link-badge-v1")


def _favicon_response(payload: bytes, content_type: str, source: str):
    response = Response(payload, mimetype=content_type or "image/x-icon")
    response.headers["X-Termin-Favicon-Source"] = source
    return _apply_cache_headers(response, hashlib.sha256(payload).hexdigest())


def _cache_dir(app: Flask) -> Path:
    path = Path(app.instance_path) / "favicon_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _cache_paths(app: Flask, url: str) -> tuple[Path, Path]:
    key = _cache_key(url)
    base = _cache_dir(app) / key
    return base.with_suffix(".bin"), base.with_suffix(".json")


def _load_cached(app: Flask, url: str) -> tuple[bytes, str, str] | None:
    try:
        payload_path, meta_path = _cache_paths(app, url)
    except Exception:
        return None
    if not payload_path.exists() or not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text())
        payload = payload_path.read_bytes()
    except Exception:
        return None
    if not payload:
        return None
    return payload, str(meta.get("content_type") or "image/x-icon"), str(meta.get("source") or "cache")


def _store_cached(app: Flask, url: str, payload: bytes, content_type: str, source: str) -> None:
    try:
        payload_path, meta_path = _cache_paths(app, url)
        payload_path.write_bytes(payload)
        meta_path.write_text(
            json.dumps(
                {
                    "url": url,
                    "content_type": content_type,
                    "source": source,
                },
                separators=(",", ":"),
            )
        )
    except Exception:
        return


def _fetch_binary(session: requests.Session, url: str):
    response = session.get(url, timeout=4, allow_redirects=True, stream=True)
    response.raise_for_status()
    content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    payload = response.content
    looks_like_ico = payload[:4] in {b"\x00\x00\x01\x00", b"\x00\x00\x02\x00"}
    if not payload:
        raise ValueError("empty payload")
    if not (content_type.startswith("image/") or looks_like_ico):
        raise ValueError(f"non-image content type: {content_type or 'unknown'}")
    return payload, content_type or "image/x-icon"


def _discover_page_icons(session: requests.Session, target: str) -> list[str]:
    response = session.get(target, timeout=4, allow_redirects=True)
    response.raise_for_status()
    content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if "html" not in content_type:
        return []
    parser = _IconLinkParser()
    parser.feed(response.text)
    parser.close()
    icons = [urljoin(response.url, href) for href in parser.icons]
    if not icons:
        # Cheap fallback for malformed HTML where the parser misses icon tags.
        matches = re.findall(r'<link[^>]+rel=["\']?[^>]*icon[^>]+href=["\']([^"\']+)["\']', response.text, flags=re.IGNORECASE)
        icons = [urljoin(response.url, href) for href in matches if href]
    deduped: list[str] = []
    seen: set[str] = set()
    for icon in icons:
        if icon in seen:
            continue
        seen.add(icon)
        deduped.append(icon)
    return deduped


def _candidate_urls(target: str) -> list[str]:
    parsed = urlparse(target)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Termin/1.0 favicon-cache",
            "Accept": "image/*,*/*;q=0.8",
        }
    )
    page_icons: list[str]
    try:
        page_icons = _discover_page_icons(session, target)
    except Exception:
        page_icons = []
    candidates = page_icons + [
        f"{origin}/favicon.ico",
        f"{origin}/favicon.png",
        f"{origin}/apple-touch-icon.png",
        f"{origin}/apple-touch-icon-precomposed.png",
        f"https://www.google.com/s2/favicons?sz=32&domain_url={quote(origin, safe='')}",
    ]
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        deduped.append(candidate)
    return deduped


def cache_favicon_for_link(app: Flask, target: str) -> bool:
    target = str(target or "").strip()
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    cache_target = canonical_favicon_target(target)
    if not cache_target:
        return False
    if _load_cached(app, cache_target):
        return True
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Termin/1.0 favicon-cache",
            "Accept": "image/*,*/*;q=0.8",
        }
    )
    for candidate in _candidate_urls(target):
        try:
            payload, content_type = _fetch_binary(session, candidate)
            _store_cached(app, cache_target, payload, content_type, candidate)
            return True
        except Exception:
            continue
    return False


def cache_favicons_for_links(app: Flask, links: list[str] | tuple[str, ...] | None) -> None:
    for link in list(links or []):
        try:
            cache_favicon_for_link(app, link)
        except Exception:
            continue


def favicon_response_for_link(app: Flask, target: str):
    target = str(target or "").strip()
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return placeholder_response(app)
    cache_target = canonical_favicon_target(target)
    if not cache_target:
        return placeholder_response(app)
    try:
        cached = _load_cached(app, cache_target)
        if cached is None:
            if not cache_favicon_for_link(app, target):
                return placeholder_response(app)
            cached = _load_cached(app, cache_target)
            if cached is None:
                return placeholder_response(app)
        payload, content_type, source = cached
        return _favicon_response(payload, content_type, source)
    except Exception:
        try:
            return placeholder_response(app)
        except Exception:
            response = Response(
                b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#1f2937"/><path d="M10 16h12" stroke="#e5e7eb" stroke-width="2" stroke-linecap="round"/><path d="M16 10v12" stroke="#e5e7eb" stroke-width="2" stroke-linecap="round"/></svg>',
                mimetype="image/svg+xml",
            )
            response.headers["Cache-Control"] = "public, max-age=86400"
            response.headers["X-Termin-Favicon-Source"] = "placeholder-inline"
            return response
