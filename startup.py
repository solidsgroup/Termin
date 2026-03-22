import os
import socket
import sys
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from dotenv import load_dotenv
from flask_migrate import upgrade
from sqlalchemy import inspect, text

from app import create_app
from app.extensions import socketio


load_dotenv()


app = create_app()


def _build_alembic_config() -> AlembicConfig:
    root = Path(__file__).resolve().parent
    config = AlembicConfig(str(root / "migrations" / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", app.config["SQLALCHEMY_DATABASE_URI"])
    return config


def _prompt_yes_no(message: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        response = input(f"{message} {suffix} ").strip().lower()
        if not response:
            return default
        if response in {"y", "yes"}:
            return True
        if response in {"n", "no"}:
            return False
        print("Please answer y or n.")


def _prompt_text(message: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    response = input(f"{message}{suffix} ").strip()
    return response or (default or "")


def _env_file_path() -> Path:
    return Path(__file__).resolve().parent / ".env"


def _update_env_file(updates: dict[str, str]) -> None:
    env_path = _env_file_path()
    existing_lines: list[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text().splitlines()

    handled: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            handled.add(key)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in handled:
            new_lines.append(f"{key}={value}")

    env_path.write_text("\n".join(new_lines) + "\n")


def _database_status() -> dict:
    engine = app.extensions["sqlalchemy"].engine
    inspector = inspect(engine)
    config = _build_alembic_config()
    script = ScriptDirectory.from_config(config)
    heads = tuple(script.get_heads())

    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
        migration_context = MigrationContext.configure(connection)
        current_heads = tuple(migration_context.get_current_heads())
        try:
            current_revision = migration_context.get_current_revision()
        except CommandError:
            current_revision = None
        table_names = sorted(inspector.get_table_names())

    return {
        "url": engine.url.render_as_string(hide_password=False),
        "tables": table_names,
        "heads": heads,
        "current_revision": current_revision,
        "current_heads": current_heads,
        "effective_current_heads": _effective_current_heads(script, current_heads),
        "has_version_table": "alembic_version" in table_names,
    }


def _effective_current_heads(script: ScriptDirectory, current_heads: tuple[str, ...]) -> tuple[str, ...]:
    if len(current_heads) < 2:
        return current_heads

    ancestor_map: dict[str, set[str]] = {}

    def collect_ancestors(revision_id: str) -> set[str]:
        if revision_id in ancestor_map:
            return ancestor_map[revision_id]
        revision = script.get_revision(revision_id)
        if not revision:
            ancestor_map[revision_id] = set()
            return ancestor_map[revision_id]
        down_revisions = revision.down_revision
        if not down_revisions:
            ancestor_map[revision_id] = set()
            return ancestor_map[revision_id]
        if isinstance(down_revisions, str):
            parents = [down_revisions]
        else:
            parents = [item for item in down_revisions if item]
        ancestors = set(parents)
        for parent in parents:
            ancestors.update(collect_ancestors(parent))
        ancestor_map[revision_id] = ancestors
        return ancestors

    effective = []
    for revision_id in current_heads:
        is_ancestor_of_other = any(
            revision_id != other_revision_id and revision_id in collect_ancestors(other_revision_id)
            for other_revision_id in current_heads
        )
        if not is_ancestor_of_other:
            effective.append(revision_id)
    return tuple(effective)


def _normalize_version_table(script: ScriptDirectory) -> None:
    engine = app.extensions["sqlalchemy"].engine
    inspector = inspect(engine)
    if "alembic_version" not in inspector.get_table_names():
        return

    with engine.begin() as connection:
        migration_context = MigrationContext.configure(connection)
        current_heads = tuple(migration_context.get_current_heads())
        effective_heads = _effective_current_heads(script, current_heads)
        stale_heads = [revision_id for revision_id in current_heads if revision_id not in effective_heads]
        for revision_id in stale_heads:
            connection.execute(
                text("DELETE FROM alembic_version WHERE version_num = :revision_id"),
                {"revision_id": revision_id},
            )


def _run_upgrade() -> None:
    print("Applying database migrations...")
    with app.app_context():
        config = _build_alembic_config()
        script = ScriptDirectory.from_config(config)
        _normalize_version_table(script)
        upgrade(directory="migrations", revision="heads")
    print("Database migrations complete.")


def _mail_status() -> dict:
    return {
        "provider": (app.config.get("MAIL_PROVIDER") or "smtp"),
        "google_service_account_file": app.config.get("GOOGLE_SERVICE_ACCOUNT_FILE"),
        "google_workspace_sender": app.config.get("GOOGLE_WORKSPACE_SENDER"),
        "host": app.config.get("SMTP_HOST"),
        "port": int(app.config.get("SMTP_PORT") or 587),
        "use_tls": bool(app.config.get("SMTP_USE_TLS")),
        "use_ssl": bool(app.config.get("SMTP_USE_SSL")),
        "from_email": app.config.get("MAIL_FROM_EMAIL"),
        "from_name": app.config.get("MAIL_FROM_NAME"),
        "public_base_url": app.config.get("PUBLIC_BASE_URL"),
        "dev_mailbox_enabled": bool(app.config.get("DEV_MAILBOX_ENABLED")),
        "dev_mailbox_capture_only": bool(app.config.get("DEV_MAILBOX_CAPTURE_ONLY")),
    }


def _probe_smtp(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str | None]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except OSError as exc:
        return False, str(exc)


def ensure_mail(interactive: bool | None = None) -> None:
    if interactive is None:
        interactive = sys.stdin.isatty()

    status = _mail_status()
    provider = str(status["provider"] or "smtp")
    if provider == "gmail_api":
        sender = status["google_workspace_sender"] or "not configured"
        print(f"Mail: gmail_api ({sender})")
    else:
        print(
            "Mail: "
            + (
                f"{status['host']}:{status['port']} "
                f"(tls={'on' if status['use_tls'] else 'off'}, ssl={'on' if status['use_ssl'] else 'off'})"
                if status["host"]
                else "not configured"
            )
        )
    print("Sender headers: inviter display name/email, with app fallback if needed.")
    print(f"Magic-link base URL: {status['public_base_url']}")
    if status["dev_mailbox_enabled"]:
        mode = "capture-only" if status["dev_mailbox_capture_only"] else "copy"
        print(f"Dev mailbox: enabled ({mode})")

    if status["dev_mailbox_capture_only"]:
        print("Mail capture mode is enabled. Outbound emails will be stored in the in-app dev mailbox only.")
        return

    if provider == "gmail_api":
        service_account_file = str(status["google_service_account_file"] or "")
        sender = str(status["google_workspace_sender"] or "")
        if not service_account_file or not sender:
            print("Gmail API delivery is not configured yet.")
            print("Set GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_WORKSPACE_SENDER.")
            if interactive:
                raise SystemExit(1)
            print("Continuing without working mail delivery.")
            return
        if not Path(service_account_file).exists():
            print(f"Gmail service account file not found: {service_account_file}")
            if interactive:
                raise SystemExit(1)
            print("Continuing without working mail delivery.")
            return
        print("Gmail API mail configuration looks present.")
        return

    if not status["host"]:
        print("Magic-link email delivery is not configured yet.")
        print("Recommended local setup: install a local SMTP server and listen on localhost:25.")
        print("Example apt path: install `postfix` and choose `Local only` during setup.")
        if interactive and _prompt_yes_no("Write local SMTP defaults to .env now?", default=True):
            host = _prompt_text("SMTP host", "127.0.0.1")
            port = _prompt_text("SMTP port", "25")
            use_tls = _prompt_yes_no("Use STARTTLS for this server?", default=False)
            use_ssl = False
            if not use_tls:
                use_ssl = _prompt_yes_no("Use implicit SSL instead?", default=False)
            updates = {
                "SMTP_HOST": host,
                "SMTP_PORT": port,
                "SMTP_USE_TLS": "true" if use_tls else "false",
                "SMTP_USE_SSL": "true" if use_ssl else "false",
            }
            _update_env_file(updates)
            app.config.update(
                SMTP_HOST=host,
                SMTP_PORT=int(port),
                SMTP_USE_TLS=use_tls,
                SMTP_USE_SSL=use_ssl,
            )
            status = _mail_status()
            print(f"Saved SMTP defaults to {_env_file_path()}.")
        else:
            print("Startup will continue, but magic-link emails cannot be sent until SMTP is configured.")
            return

    ok, error = _probe_smtp(str(status["host"]), int(status["port"]))
    if ok:
        print("SMTP connection check succeeded.")
        return

    print(f"SMTP connection check failed: {error}")
    if str(status["host"]) in {"127.0.0.1", "localhost"}:
        print("Local mail server checklist:")
        print("  1. Install `postfix` with apt.")
        print("  2. Choose `Local only` so it listens on localhost.")
        print("  3. Verify with `ss -ltn sport = :25` and rerun `python3 app.py`.")
    else:
        print("Check that the configured SMTP host/port is reachable from this machine.")

    if interactive and _prompt_yes_no("Continue starting the app without working SMTP?", default=True):
        return
    if not interactive:
        print("Continuing without working SMTP.")
        return
    raise SystemExit(1)


def ensure_database(interactive: bool | None = None) -> None:
    if interactive is None:
        interactive = sys.stdin.isatty()

    with app.app_context():
        status = _database_status()

    print(f"Database: {status['url']}")

    if not status["heads"]:
        print("No Alembic revisions were found. Startup will continue without migration changes.")
        return

    current = status["current_revision"]
    current_heads = tuple(status["effective_current_heads"] or status["current_heads"])
    heads = tuple(status["heads"])

    if heads and set(current_heads) == set(heads):
        label = ", ".join(current_heads) if current_heads else current or "none"
        print(f"Database schema is up to date at revision {label}.")
        return

    is_fresh = not status["tables"] or status["tables"] == ["alembic_version"]
    if is_fresh:
        print("Database is not initialized yet.")
    elif current is None and not status["has_version_table"]:
        print("Database tables exist, but Alembic has not tracked this schema yet.")
        print("Automatic migration is unsafe in this state.")
        print("Recommended: back up the database, then align it with Alembic manually.")
        raise SystemExit(1)
    else:
        current_label = ", ".join(current_heads) if current_heads else current or "none"
        print(f"Database is behind. Current revision: {current_label}, target revision: {', '.join(heads)}.")

    if interactive:
        if not _prompt_yes_no("Run database upgrade now?", default=True):
            raise SystemExit(1)
    else:
        print("Non-interactive startup detected. Running automatic database upgrade.")

    _run_upgrade()


def prepare_runtime(interactive: bool | None = None) -> None:
    ensure_database(interactive=interactive)
    ensure_mail(interactive=interactive)


if __name__ == "__main__":
    prepare_runtime()
