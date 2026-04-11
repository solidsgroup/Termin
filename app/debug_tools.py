from flask import current_app


def debug_console_enabled() -> bool:
    return bool(current_app.config.get("TERMIN_DEBUG_CONSOLE"))


def debug_print(*parts) -> None:
    if not debug_console_enabled():
        return
    print("[termin-debug]", *parts)
