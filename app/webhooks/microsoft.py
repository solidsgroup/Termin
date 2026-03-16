from flask import Blueprint, request


ms_webhooks_bp = Blueprint("microsoft_webhooks", __name__)


@ms_webhooks_bp.route("/calendar", methods=["POST"])
def calendar_webhook():
    # Microsoft Graph sends validationToken on subscription creation
    validation_token = request.args.get("validationToken")
    if validation_token:
        return validation_token, 200, {"Content-Type": "text/plain"}

    payload = request.get_json(silent=True) or {}
    return {"received": True, "payload": payload}, 200
