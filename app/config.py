import os
from urllib.parse import urlparse


def _env(key: str, default: str | None = None) -> str | None:
    return os.getenv(key, default)


def _env_bool(key: str, default: bool = False) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    SECRET_KEY = _env("SECRET_KEY", "dev-secret")
    _default_sqlite_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "instance",
        "app.db",
    )
    SQLALCHEMY_DATABASE_URI = _env("DATABASE_URL", f"sqlite:///{_default_sqlite_path}")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    _db_scheme = urlparse(SQLALCHEMY_DATABASE_URI).scheme
    SQLALCHEMY_ENGINE_OPTIONS = (
        {"connect_args": {"timeout": 10}}
        if _db_scheme == "sqlite"
        else {}
    )

    # OAuth
    GOOGLE_CLIENT_ID = _env("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = _env("GOOGLE_CLIENT_SECRET")
    GITHUB_CLIENT_ID = _env("GITHUB_CLIENT_ID")
    GITHUB_CLIENT_SECRET = _env("GITHUB_CLIENT_SECRET")
    MICROSOFT_CLIENT_ID = _env("MICROSOFT_CLIENT_ID")
    MICROSOFT_CLIENT_SECRET = _env("MICROSOFT_CLIENT_SECRET")

    # Webhook endpoints verification
    GOOGLE_WEBHOOK_SECRET = _env("GOOGLE_WEBHOOK_SECRET", "change-me")

    # Base URL for constructing callback/webhook URLs
    _render_hostname = _env("RENDER_EXTERNAL_HOSTNAME")
    PUBLIC_BASE_URL = _env(
        "PUBLIC_BASE_URL",
        f"https://{_render_hostname}" if _render_hostname else "http://localhost:5000",
    )
    PREFERRED_URL_SCHEME = _env("PREFERRED_URL_SCHEME", "https")

    # Mail delivery
    MAIL_PROVIDER = _env("MAIL_PROVIDER", "smtp")
    GOOGLE_SERVICE_ACCOUNT_FILE = _env("GOOGLE_SERVICE_ACCOUNT_FILE")
    GOOGLE_WORKSPACE_SENDER = _env("GOOGLE_WORKSPACE_SENDER")
    SMTP_HOST = _env("SMTP_HOST")
    SMTP_PORT = int(_env("SMTP_PORT", "587"))
    SMTP_USERNAME = _env("SMTP_USERNAME")
    SMTP_PASSWORD = _env("SMTP_PASSWORD")
    SMTP_USE_TLS = _env_bool("SMTP_USE_TLS", True)
    SMTP_USE_SSL = _env_bool("SMTP_USE_SSL", False)
    SMTP_TIMEOUT = int(_env("SMTP_TIMEOUT", "20"))
    MAIL_FROM_EMAIL = _env("MAIL_FROM_EMAIL") or GOOGLE_WORKSPACE_SENDER
    MAIL_FROM_NAME = _env("MAIL_FROM_NAME", "Termin")
    MAIL_REPLY_TO_POLICY = _env("MAIL_REPLY_TO_POLICY", "none")
    DEV_MAILBOX_ENABLED = _env_bool("DEV_MAILBOX_ENABLED", True)
    DEV_MAILBOX_CAPTURE_ONLY = _env_bool("DEV_MAILBOX_CAPTURE_ONLY", True)
    ADMIN_EMAILS = _env("ADMIN_EMAILS", "")
    LOCAL_ADMIN_PASSWORD = _env("LOCAL_ADMIN_PASSWORD")

    # Web Push / PWA
    WEB_PUSH_ENABLED = _env_bool("WEB_PUSH_ENABLED", False)
    WEB_PUSH_PUBLIC_KEY = _env("WEB_PUSH_PUBLIC_KEY")
    WEB_PUSH_PRIVATE_KEY = _env("WEB_PUSH_PRIVATE_KEY")
    WEB_PUSH_SUBJECT = _env("WEB_PUSH_SUBJECT", "mailto:admin@example.com")
