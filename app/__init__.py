import os

from engineio.payload import Payload
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix
from app.config import Config
from app.extensions import db, migrate, socketio
from app.realtime import register_socket_handlers
from app.routes import api_bp
from app.auth import auth_bp
from app.ui import ui_bp, calendar_subscription_urls_for_user
from app.webhooks.google import google_webhooks_bp
from app.oauth import init_oauth
from app.utils import current_user, format_datetime_for_user, is_admin, user_timezone_name


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

    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(auth_bp)
    app.register_blueprint(ui_bp)
    app.register_blueprint(google_webhooks_bp, url_prefix="/webhooks/google")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.context_processor
    def inject_template_globals():
        user = current_user()
        calendar_urls = calendar_subscription_urls_for_user(user) if user else {"https": "", "webcal": ""}
        return {
            "user": user,
            "is_admin_user": is_admin(user),
            "calendar_subscription_url": calendar_urls["https"],
            "calendar_subscription_webcal_url": calendar_urls["webcal"],
            "current_user_timezone": user_timezone_name(user),
            "format_user_datetime": lambda value, fmt="%Y-%m-%d %H:%M": format_datetime_for_user(value, user, fmt),
        }

    return app
