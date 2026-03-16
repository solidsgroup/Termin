from flask import Flask
from app.config import Config
from app.extensions import db, migrate
from app.routes import api_bp
from app.auth import auth_bp
from app.ui import ui_bp
from app.webhooks.google import google_webhooks_bp
from app.webhooks.microsoft import ms_webhooks_bp
from app.oauth import init_oauth


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    migrate.init_app(app, db)
    init_oauth(app)

    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(auth_bp)
    app.register_blueprint(ui_bp)
    app.register_blueprint(google_webhooks_bp, url_prefix="/webhooks/google")
    app.register_blueprint(ms_webhooks_bp, url_prefix="/webhooks/microsoft")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app
