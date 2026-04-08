import os

from engineio.payload import Payload
from flask import Flask, send_from_directory
from sqlalchemy import event
from werkzeug.middleware.proxy_fix import ProxyFix
from app.config import Config
from app.extensions import db, migrate, socketio
from app.realtime import register_socket_handlers
from app.routes import api_bp
from app.auth import auth_bp
from app.ui import ui_bp, calendar_subscription_urls_for_user
from app.webhooks.google import google_webhooks_bp
from app.oauth import init_oauth
from app.themes import THEME_DEFINITIONS, normalize_theme_mode, normalize_theme_name, theme_interface
from app.utils import current_user, format_datetime_for_user, is_admin, user_timezone_name
from app.web_push import public_web_push_config


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

    @app.context_processor
    def inject_template_globals():
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
        }

    return app
