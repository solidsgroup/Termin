import os

from flask import Flask
from app.config import Config
from app.extensions import db, migrate
from app.routes import api_bp
from app.auth import auth_bp
from app.ui import ui_bp
from app.webhooks.google import google_webhooks_bp
from app.oauth import init_oauth
from app.utils import current_user, is_admin


def create_app() -> Flask:
    instance_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "instance")
    app = Flask(__name__, instance_path=instance_path)
    app.config.from_object(Config)
    os.makedirs(app.instance_path, exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)
    init_oauth(app)

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
        return {
            "user": user,
            "is_admin_user": is_admin(user),
        }

    return app
