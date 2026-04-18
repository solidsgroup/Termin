from __future__ import annotations

from datetime import datetime
import json

from flask import current_app
from pywebpush import WebPushException, webpush

from app.debug_tools import debug_print
from app.extensions import db
from app.models import WebPushSubscription


def web_push_is_configured() -> bool:
    config = current_app.config
    return bool(
        config.get("WEB_PUSH_ENABLED")
        and config.get("WEB_PUSH_PUBLIC_KEY")
        and config.get("WEB_PUSH_PRIVATE_KEY")
        and config.get("WEB_PUSH_SUBJECT")
    )


def public_web_push_config() -> dict:
    return {
        "enabled": bool(
            current_app.config.get("WEB_PUSH_ENABLED")
            and current_app.config.get("WEB_PUSH_PUBLIC_KEY")
        ),
        "public_key": current_app.config.get("WEB_PUSH_PUBLIC_KEY") or "",
    }


def upsert_web_push_subscription(
    *,
    user_id: int,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None = None,
    device_label: str | None = None,
) -> WebPushSubscription:
    now = datetime.utcnow()
    row = WebPushSubscription.query.filter_by(endpoint=endpoint).first()
    if row is None:
        row = WebPushSubscription(
            user_id=user_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=(user_agent or "")[:512] or None,
            device_label=(device_label or "")[:255] or None,
            last_seen_at=now,
            created_at=now,
            updated_at=now,
        )
        db.session.add(row)
    else:
        row.user_id = user_id
        row.p256dh = p256dh
        row.auth = auth
        row.user_agent = (user_agent or "")[:512] or None
        row.device_label = (device_label or "")[:255] or None
        row.last_seen_at = now
        row.updated_at = now
    return row


def delete_web_push_subscription(*, user_id: int, endpoint: str) -> bool:
    row = WebPushSubscription.query.filter_by(user_id=user_id, endpoint=endpoint).first()
    if row is None:
        return False
    db.session.delete(row)
    return True


def active_web_push_subscriptions_for_user(user_id: int) -> list[WebPushSubscription]:
    return (
        WebPushSubscription.query
        .filter_by(user_id=user_id)
        .order_by(WebPushSubscription.updated_at.desc(), WebPushSubscription.id.desc())
        .all()
    )


def send_web_push_notification(*, user_id: int, payload: dict) -> dict:
    result = {
        "configured": bool(web_push_is_configured()),
        "subscription_count": 0,
        "sent_count": 0,
        "stale_count": 0,
        "failure_count": 0,
        "failures": [],
    }
    if not result["configured"]:
        debug_print("web-push", "send skipped", "user_id=", user_id, "reason=config-disabled")
        return result

    subscriptions = active_web_push_subscriptions_for_user(user_id)
    result["subscription_count"] = len(subscriptions)
    if not subscriptions:
        debug_print("web-push", "send skipped", "user_id=", user_id, "reason=no-subscriptions")
        return result

    vapid_private_key = current_app.config["WEB_PUSH_PRIVATE_KEY"]
    vapid_claims = {"sub": current_app.config["WEB_PUSH_SUBJECT"]}
    payload_json = json.dumps(payload, sort_keys=True)
    ttl_seconds = max(300, int(current_app.config.get("WEB_PUSH_TTL_SECONDS") or 21600))
    stale_rows: list[WebPushSubscription] = []

    for row in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": row.endpoint,
                    "keys": {
                        "p256dh": row.p256dh,
                        "auth": row.auth,
                    },
                },
                data=payload_json,
                vapid_private_key=vapid_private_key,
                vapid_claims=vapid_claims,
                ttl=ttl_seconds,
            )
        except WebPushException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in {404, 410}:
                stale_rows.append(row)
                result["stale_count"] += 1
                continue
            result["failure_count"] += 1
            result["failures"].append(
                {
                    "endpoint": row.endpoint,
                    "status_code": status_code,
                    "error": str(exc),
                }
            )
            current_app.logger.warning(
                "web push delivery failed",
                extra={"user_id": user_id, "endpoint": row.endpoint, "status_code": status_code},
            )
        except Exception:
            result["failure_count"] += 1
            result["failures"].append(
                {
                    "endpoint": row.endpoint,
                    "status_code": None,
                    "error": "unexpected error",
                }
            )
            current_app.logger.exception(
                "web push delivery failed",
                extra={"user_id": user_id, "endpoint": row.endpoint},
            )
        else:
            result["sent_count"] += 1

    if stale_rows:
        for row in stale_rows:
            db.session.delete(row)
        db.session.commit()
    debug_print(
        "web-push",
        "send result",
        "user_id=",
        user_id,
        "ttl=",
        ttl_seconds,
        "subscriptions=",
        result["subscription_count"],
        "sent=",
        result["sent_count"],
        "stale=",
        result["stale_count"],
        "failed=",
        result["failure_count"],
    )
    return result
