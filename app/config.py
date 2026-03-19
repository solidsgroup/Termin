import os


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

    # OAuth
    GOOGLE_CLIENT_ID = _env("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = _env("GOOGLE_CLIENT_SECRET")
    MS_CLIENT_ID = _env("MS_CLIENT_ID")
    MS_CLIENT_SECRET = _env("MS_CLIENT_SECRET")

    # Webhook endpoints verification
    GOOGLE_WEBHOOK_SECRET = _env("GOOGLE_WEBHOOK_SECRET", "change-me")
    MS_WEBHOOK_SECRET = _env("MS_WEBHOOK_SECRET", "change-me")

    # Base URL for constructing callback/webhook URLs
    _render_hostname = _env("RENDER_EXTERNAL_HOSTNAME")
    PUBLIC_BASE_URL = _env(
        "PUBLIC_BASE_URL",
        f"https://{_render_hostname}" if _render_hostname else "http://localhost:5000",
    )

    # SMTP for magic-link delivery
    SMTP_HOST = _env("SMTP_HOST")
    SMTP_PORT = int(_env("SMTP_PORT", "587"))
    SMTP_USERNAME = _env("SMTP_USERNAME")
    SMTP_PASSWORD = _env("SMTP_PASSWORD")
    SMTP_USE_TLS = _env_bool("SMTP_USE_TLS", True)
    SMTP_USE_SSL = _env_bool("SMTP_USE_SSL", False)
    SMTP_TIMEOUT = int(_env("SMTP_TIMEOUT", "20"))
    MAIL_FROM_EMAIL = _env("MAIL_FROM_EMAIL")
    MAIL_FROM_NAME = _env("MAIL_FROM_NAME", "Termin")
    DEV_MAILBOX_ENABLED = _env_bool("DEV_MAILBOX_ENABLED", True)
    DEV_MAILBOX_CAPTURE_ONLY = _env_bool("DEV_MAILBOX_CAPTURE_ONLY", True)
