import os
from time import perf_counter

from engineio.payload import Payload
from flask import Flask, abort, g, jsonify, request, send_from_directory, url_for
from sqlalchemy import event
from werkzeug.middleware.proxy_fix import ProxyFix
from app.config import Config
from app.debug_tools import debug_console_enabled, debug_print, record_timing_event, recent_timing_events
from app.extensions import db, migrate, socketio
from app.favicon_cache import favicon_response_for_link
from app.realtime import register_socket_handlers
from app.routes import api_bp
from app.auth import auth_bp
from app.ui import ui_bp, calendar_subscription_urls_for_user
from app.webhooks.google import google_webhooks_bp
from app.oauth import init_oauth
from app.themes import THEME_DEFINITIONS, normalize_theme_mode, normalize_theme_name, theme_interface
from app.utils import current_user, format_datetime_for_user, is_admin, user_timezone_name
from app.web_push import public_web_push_config


_PROFILED_PATH_PREFIXES = (
    "/api/dashboard-bootstrap",
    "/api/dashboard-changes",
    "/api/projects/",
    "/api/tasks/",
    "/link-favicon",
    "/tree/",
    "/todo",
    "/dashboard",
)


def _should_profile_request(path: str) -> bool:
    normalized = str(path or "").strip()
    if not normalized:
        return False
    if normalized.startswith("/api/projects/") and normalized.endswith("/tree_snapshot"):
        return True
    if normalized.startswith("/api/tasks/"):
        return True
    return any(normalized.startswith(prefix) for prefix in _PROFILED_PATH_PREFIXES)


def create_app() -> Flask:
    instance_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "instance")
    app = Flask(__name__, instance_path=instance_path)
    app.config.from_object(Config)
    os.makedirs(app.instance_path, exist_ok=True)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    Payload.max_decode_packets = max(int(getattr(Payload, "max_decode_packets", 16) or 16), 200)

    db.init_app(app)
    migrate.init_app(app, db)
    socketio.init_app(app)
    init_oauth(app)
    register_socket_handlers()

    if str(app.config.get("SQLALCHEMY_DATABASE_URI", "")).startswith("sqlite:"):
        with app.app_context():
            @event.listens_for(db.engine, "connect")
            def _configure_sqlite_connection(dbapi_connection, _connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout = 10000")
                cursor.close()

    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(auth_bp)
    app.register_blueprint(ui_bp)
    app.register_blueprint(google_webhooks_bp, url_prefix="/webhooks/google")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/manifest.webmanifest")
    def web_manifest():
        response = send_from_directory(app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/service-worker.js")
    def service_worker():
        response = send_from_directory(app.static_folder, "service-worker.js", mimetype="application/javascript")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/uploads/<path:filename>")
    def uploaded_file(filename: str):
        uploads_root = os.path.join(app.instance_path, "uploads")
        return send_from_directory(uploads_root, filename)

    @app.get("/link-favicon")
    def link_favicon():
        target = (request.args.get("url") or "").strip()
        return favicon_response_for_link(app, target)

    @app.get("/debug/performance")
    def debug_performance_page():
        if not debug_console_enabled():
            abort(404)
        return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Termin Performance</title>
    <style>
      :root {
        --bg: #0b1220;
        --panel: #121c2f;
        --panel-alt: #17243a;
        --ink: #e5eefc;
        --muted: #91a4c2;
        --border: rgba(145, 164, 194, 0.2);
        --accent: #78c0ff;
        --warn: #ffd166;
        --bad: #ff7b7b;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        padding: 24px;
        font: 14px/1.45 "SFMono-Regular", Menlo, Consolas, monospace;
        background: linear-gradient(180deg, #08101c 0%, var(--bg) 100%);
        color: var(--ink);
      }
      h1 {
        margin: 0 0 8px;
        font-size: 20px;
      }
      .muted {
        color: var(--muted);
      }
      .layout {
        display: grid;
        grid-template-columns: 360px minmax(0, 1fr);
        gap: 16px;
        align-items: start;
        margin-top: 18px;
      }
      .panel {
        background: color-mix(in srgb, var(--panel) 92%, transparent);
        border: 1px solid var(--border);
        border-radius: 16px;
        padding: 16px;
        box-shadow: 0 16px 40px rgba(0, 0, 0, 0.28);
      }
      .stats {
        display: grid;
        gap: 10px;
      }
      .stat {
        padding: 12px;
        border-radius: 12px;
        background: color-mix(in srgb, var(--panel-alt) 86%, transparent);
        border: 1px solid var(--border);
      }
      .stat-label {
        display: block;
        color: var(--muted);
        font-size: 12px;
        margin-bottom: 6px;
      }
      .stat-value {
        font-size: 24px;
        font-weight: 700;
      }
      table {
        width: 100%;
        border-collapse: collapse;
      }
      th, td {
        padding: 10px 8px;
        border-bottom: 1px solid var(--border);
        text-align: left;
        vertical-align: top;
      }
      th {
        color: var(--muted);
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }
      .duration {
        font-weight: 700;
        white-space: nowrap;
      }
      .duration.warn { color: var(--warn); }
      .duration.bad { color: var(--bad); }
      .path {
        word-break: break-word;
      }
      .toolbar {
        display: flex;
        gap: 10px;
        align-items: center;
        margin-top: 12px;
      }
      button {
        border: 1px solid var(--border);
        background: var(--panel-alt);
        color: var(--ink);
        border-radius: 10px;
        padding: 8px 12px;
        font: inherit;
        cursor: pointer;
      }
      @media (max-width: 900px) {
        .layout {
          grid-template-columns: 1fr;
        }
      }
    </style>
  </head>
  <body>
    <h1>Termin Performance</h1>
    <div class="muted">Recent profiled request timings from the running debug server.</div>
    <div class="toolbar">
      <button type="button" id="refresh-button">Refresh</button>
      <label><input type="checkbox" id="auto-refresh" checked /> Auto refresh</label>
      <span class="muted" id="last-updated">Waiting for first sample…</span>
    </div>
    <div class="layout">
      <section class="panel">
        <div class="stats">
          <div class="stat">
            <span class="stat-label">Recent Samples</span>
            <span class="stat-value" id="sample-count">0</span>
          </div>
          <div class="stat">
            <span class="stat-label">Average Duration</span>
            <span class="stat-value" id="avg-duration">0.0 ms</span>
          </div>
          <div class="stat">
            <span class="stat-label">Slowest Recent Request</span>
            <span class="stat-value" id="max-duration">0.0 ms</span>
          </div>
        </div>
      </section>
      <section class="panel">
        <table>
          <thead>
            <tr>
              <th>Method</th>
              <th>Status</th>
              <th>Duration</th>
              <th>Payload</th>
              <th>Path</th>
            </tr>
          </thead>
          <tbody id="timing-rows">
            <tr><td colspan="5" class="muted">No profiled requests yet.</td></tr>
          </tbody>
        </table>
      </section>
    </div>
    <script>
      let pollingStopped = false;

      async function loadPerformance() {
        const response = await fetch('/debug/performance.json', { cache: 'no-store', credentials: 'same-origin' });
        if (!response.ok) {
          pollingStopped = true;
          const updatedEl = document.getElementById('last-updated');
          const rowsEl = document.getElementById('timing-rows');
          if (updatedEl) {
            updatedEl.textContent = 'Performance JSON unavailable (' + String(response.status) + ')';
          }
          if (rowsEl) {
            rowsEl.innerHTML = '<tr><td colspan="5" class="muted">Performance JSON is unavailable. If you are not running with --debug, this is expected.</td></tr>';
          }
          throw new Error('Could not load performance JSON');
        }
        const data = await response.json();
        const events = Array.isArray(data.events) ? data.events : [];
        const countEl = document.getElementById('sample-count');
        const avgEl = document.getElementById('avg-duration');
        const maxEl = document.getElementById('max-duration');
        const rowsEl = document.getElementById('timing-rows');
        const updatedEl = document.getElementById('last-updated');
        countEl.textContent = String(events.length);
        const total = events.reduce((sum, item) => sum + Number(item.duration_ms || 0), 0);
        const max = events.reduce((top, item) => Math.max(top, Number(item.duration_ms || 0)), 0);
        avgEl.textContent = (events.length ? (total / events.length) : 0).toFixed(1) + ' ms';
        maxEl.textContent = max.toFixed(1) + ' ms';
        updatedEl.textContent = 'Updated ' + new Date().toLocaleTimeString();
        if (!events.length) {
          rowsEl.innerHTML = '<tr><td colspan="5" class="muted">No profiled requests yet.</td></tr>';
          return;
        }
        rowsEl.innerHTML = events.map(function (item) {
          const duration = Number(item.duration_ms || 0);
          const cls = duration >= 250 ? 'duration bad' : (duration >= 100 ? 'duration warn' : 'duration');
          const payloadBytes = Number(item.payload_bytes || 0);
          const payloadLabel = payloadBytes >= 1048576
            ? (payloadBytes / 1048576).toFixed(2) + ' MB'
            : (payloadBytes >= 1024 ? (payloadBytes / 1024).toFixed(1) + ' KB' : payloadBytes + ' B');
          return '<tr>' +
            '<td>' + String(item.method || '') + '</td>' +
            '<td>' + String(item.status || '') + '</td>' +
            '<td class="' + cls + '">' + duration.toFixed(1) + ' ms</td>' +
            '<td>' + payloadLabel + '</td>' +
            '<td class="path">' + String(item.path || '') + '</td>' +
          '</tr>';
        }).join('');
      }
      document.getElementById('refresh-button').addEventListener('click', function () {
        loadPerformance().catch(function () {});
      });
      loadPerformance().catch(function () {});
      window.setInterval(function () {
        if (pollingStopped) return;
        const checkbox = document.getElementById('auto-refresh');
        if (!checkbox || !checkbox.checked) return;
        loadPerformance().catch(function () {});
      }, 2000);
    </script>
  </body>
</html>"""

    @app.get("/debug/performance.json")
    def debug_performance_data():
        if not debug_console_enabled():
            abort(404)
        events = recent_timing_events(200)
        return jsonify({"events": events})

    @app.before_request
    def begin_request_timing():
        if _should_profile_request(request.path):
            g._termin_request_started_at = perf_counter()

    @app.after_request
    def apply_asset_cache_headers(response):
        path = (request.path or "").strip()
        started_at = getattr(g, "_termin_request_started_at", None)
        if started_at is not None:
            duration_ms = max(0.0, (perf_counter() - started_at) * 1000.0)
            existing_server_timing = response.headers.get("Server-Timing", "").strip()
            timing_value = "app;dur=" + format(duration_ms, ".1f")
            payload_bytes_header = response.headers.get("X-Termin-Payload-Bytes", "").strip()
            payload_bytes = int(payload_bytes_header) if payload_bytes_header.isdigit() else 0
            response.headers["Server-Timing"] = (
                existing_server_timing + ", " + timing_value
                if existing_server_timing
                else timing_value
            )
            debug_print(
                "timing",
                request.method,
                path,
                "status=",
                response.status_code,
                "dur_ms=",
                format(duration_ms, ".1f"),
                "payload_bytes=",
                payload_bytes,
            )
            record_timing_event(
                method=request.method,
                path=path,
                status=response.status_code,
                duration_ms=duration_ms,
                payload_bytes=payload_bytes,
            )
        if path.startswith("/static/") and response.status_code == 200:
            response.cache_control.public = True
            response.cache_control.max_age = 31536000
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @app.context_processor
    def inject_template_globals():
        def asset_url(filename: str) -> str:
            version = None
            try:
                asset_path = os.path.join(app.static_folder or "", filename)
                version = str(int(os.path.getmtime(asset_path)))
            except OSError:
                version = None
            if version is None:
                return url_for("static", filename=filename)
            return url_for("static", filename=filename, v=version)

        user = current_user()
        calendar_urls = calendar_subscription_urls_for_user(user) if user else {"https": "", "webcal": ""}
        active_theme_name = normalize_theme_name(getattr(user, "theme_name", None) if user else None)
        active_theme_mode = normalize_theme_mode(getattr(user, "theme_mode", None) if user else None)
        return {
            "user": user,
            "is_admin_user": is_admin(user),
            "calendar_subscription_url": calendar_urls["https"],
            "calendar_subscription_webcal_url": calendar_urls["webcal"],
            "current_user_timezone": user_timezone_name(user),
            "format_user_datetime": lambda value, fmt="%Y-%m-%d %H:%M": format_datetime_for_user(value, user, fmt),
            "theme_definitions": THEME_DEFINITIONS,
            "global_active_theme_name": active_theme_name,
            "global_active_theme_mode": active_theme_mode,
            "global_theme_interface": theme_interface(active_theme_name, active_theme_mode),
            "global_web_push_config": public_web_push_config(),
            "asset_url": asset_url,
        }

    return app
