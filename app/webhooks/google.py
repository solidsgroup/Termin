from flask import Blueprint, request


google_webhooks_bp = Blueprint("google_webhooks", __name__)


@google_webhooks_bp.route("/calendar", methods=["POST"])
def calendar_webhook():
    # Google Calendar push notifications come as headers; body is empty.
    headers = dict(request.headers)
    return {"received": True, "headers": headers}, 200
