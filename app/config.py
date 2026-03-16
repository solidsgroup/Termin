import os


def _env(key: str, default: str | None = None) -> str | None:
    return os.getenv(key, default)


class Config:
    SECRET_KEY = _env("SECRET_KEY", "dev-secret")
    SQLALCHEMY_DATABASE_URI = _env("DATABASE_URL", "sqlite:////home/brunnels/Termin/instance/app.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # OAuth
    GOOGLE_CLIENT_ID = _env("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = _env("GOOGLE_CLIENT_SECRET")
    MS_CLIENT_ID = _env("MS_CLIENT_ID")
    MS_CLIENT_SECRET = _env("MS_CLIENT_SECRET")

    # Webhook endpoints verification
    GOOGLE_WEBHOOK_SECRET = _env("GOOGLE_WEBHOOK_SECRET", "change-me")
    MS_WEBHOOK_SECRET = _env("MS_WEBHOOK_SECRET", "change-me")

    # Base URL for constructing callback/webhook URLs
    PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", "http://localhost:5000")
