from collections import deque
from flask import current_app

_TIMING_EVENTS = deque(maxlen=500)


def debug_console_enabled() -> bool:
    return bool(current_app.config.get("TERMIN_DEBUG_CONSOLE"))


def debug_print(*parts) -> None:
    if not debug_console_enabled():
        return
    print("[termin-debug]", *parts)


def record_timing_event(*, method: str, path: str, status: int, duration_ms: float) -> None:
    _TIMING_EVENTS.appendleft({
        "method": str(method or "").upper(),
        "path": str(path or ""),
        "status": int(status or 0),
        "duration_ms": round(float(duration_ms or 0.0), 1),
    })


def recent_timing_events(limit: int = 200) -> list[dict]:
    max_items = max(1, min(int(limit or 200), len(_TIMING_EVENTS) or 1))
    return list(_TIMING_EVENTS)[:max_items]
