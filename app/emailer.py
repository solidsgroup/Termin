from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
import base64
import json
from pathlib import Path
import smtplib
import traceback

from flask import current_app
import requests

from app.extensions import db
from app.models import DevMailboxMessage


class MailDeliveryError(RuntimeError):
    pass


def _mail_config() -> dict[str, object]:
    return {
        "provider": (current_app.config.get("MAIL_PROVIDER") or "smtp"),
        "google_service_account_file": current_app.config.get("GOOGLE_SERVICE_ACCOUNT_FILE"),
        "google_workspace_sender": current_app.config.get("GOOGLE_WORKSPACE_SENDER"),
        "host": current_app.config.get("SMTP_HOST"),
        "port": int(current_app.config.get("SMTP_PORT") or 587),
        "username": current_app.config.get("SMTP_USERNAME"),
        "password": current_app.config.get("SMTP_PASSWORD"),
        "use_tls": bool(current_app.config.get("SMTP_USE_TLS")),
        "use_ssl": bool(current_app.config.get("SMTP_USE_SSL")),
        "timeout": int(current_app.config.get("SMTP_TIMEOUT") or 20),
        "from_email": current_app.config.get("MAIL_FROM_EMAIL"),
        "from_name": current_app.config.get("MAIL_FROM_NAME") or "Termin",
        "dev_mailbox_enabled": bool(current_app.config.get("DEV_MAILBOX_ENABLED")),
        "dev_mailbox_capture_only": bool(current_app.config.get("DEV_MAILBOX_CAPTURE_ONLY")),
    }


def _sender_header(from_name: str, from_email: str) -> str:
    safe_name = str(from_name).replace('"', "").strip()
    return f'"{safe_name}" <{from_email}>'


def _email_button(url: str, label: str) -> str:
    return (
        '<a href="'
        + escape(url)
        + '" style="display:inline-block;padding:10px 14px;border-radius:999px;'
        + "background:#1677c8;color:#ffffff;text-decoration:none;font-weight:700;font-size:13px;"
        + 'line-height:1;white-space:nowrap;">'
        + escape(label)
        + "</a>"
    )


def _email_subtle_button(url: str, label: str, tone: str = "neutral") -> str:
    if tone == "primary":
        background = "#edf9f1"
        border = "#b7e4c2"
        color = "#1f7a3e"
    elif tone == "danger":
        background = "#fff1f2"
        border = "#fecdd3"
        color = "#b42318"
    else:
        background = "#f7fafc"
        border = "#d7e0ea"
        color = "#334155"
    return (
        '<a href="'
        + escape(url)
        + '" style="display:inline-block;padding:10px 13px;border-radius:999px;'
        + f"background:{background};border:1px solid {border};color:{color};"
        + 'text-decoration:none;font-weight:700;font-size:13px;line-height:1;white-space:nowrap;">'
        + escape(label)
        + "</a>"
    )


def _email_task_row(
    *,
    label_html: str,
    open_url: str | None = None,
    accept_url: str | None = None,
    decline_url: str | None = None,
    complete_url: str | None = None,
) -> str:
    actions: list[str] = []
    if accept_url:
        actions.append(_email_subtle_button(accept_url, "✓ Accept", tone="primary"))
    if complete_url:
        actions.append(_email_subtle_button(complete_url, "✓ Mark complete", tone="primary"))
    if decline_url:
        actions.append(_email_subtle_button(decline_url, "Decline", tone="danger"))
    if open_url:
        actions.append(_email_subtle_button(open_url, "Open"))
    return (
        '<div style="margin-top:10px;padding:11px 13px;border:1px solid #d7e0ea;'
        + 'border-radius:14px;background:#f7fafc;">'
        + '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="border-collapse:collapse;">'
        + '<tr>'
        + '<td style="padding:0 12px 0 0;font-size:14px;font-weight:600;color:#17202b;line-height:1.35;">'
        + label_html
        + "</td>"
        + '<td style="padding:0;text-align:right;white-space:nowrap;">'
        + "".join(actions)
        + "</td>"
        + "</tr>"
        + "</table>"
        + "</div>"
    )


def _render_email_template(
    *,
    eyebrow: str,
    title: str,
    intro: str,
    body_html: str,
    footer_html: str | None = None,
) -> str:
    footer = footer_html or '<p style="margin:20px 0 0;color:#5f6b7a;font-size:12px;line-height:1.5;">Sent by Termin.</p>'
    return (
        "<!doctype html>"
        '<html lang="en"><body style="margin:0;padding:0;background:#eef3f8;color:#17202b;'
        + 'font-family:Space Grotesk, IBM Plex Sans, Segoe UI, sans-serif;">'
        + '<div style="max-width:640px;margin:0 auto;padding:32px 18px;">'
        + '<div style="padding:28px 28px 24px;border-radius:24px;background:#ffffff;'
        + 'border:1px solid #d7e0ea;box-shadow:0 14px 34px rgba(23,32,43,0.08);">'
        + '<div style="font-size:11px;font-weight:700;letter-spacing:0.14em;text-transform:uppercase;'
        + 'color:#1677c8;margin-bottom:10px;">'
        + escape(eyebrow)
        + "</div>"
        + '<h1 style="margin:0 0 12px;font-size:28px;line-height:1.1;color:#17202b;">'
        + escape(title)
        + "</h1>"
        + '<p style="margin:0 0 18px;font-size:15px;line-height:1.6;color:#334155;">'
        + escape(intro)
        + "</p>"
        + body_html
        + footer
        + "</div></div></body></html>"
    )


def _format_ics_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y%m%dT%H%M%SZ")


def _escape_ics_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\n", r"\n")
    )


def _calendar_attachment(
    *,
    uid: str,
    summary: str,
    description: str,
    start_at: datetime,
    end_at: datetime,
    attendee_email: str,
    organizer_email: str,
    organizer_name: str,
) -> tuple[str, bytes, str]:
    start_date = start_at.date()
    end_date = end_at.date()
    if end_date <= start_date:
        end_date = start_date + timedelta(days=1)
    ics = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Termin//Task Calendar//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:REQUEST",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{_format_ics_timestamp(datetime.utcnow())}",
            f"DTSTART;VALUE=DATE:{start_date.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{end_date.strftime('%Y%m%d')}",
            f"SUMMARY:{_escape_ics_text(summary)}",
            f"DESCRIPTION:{_escape_ics_text(description)}",
            f"ORGANIZER;CN={_escape_ics_text(organizer_name)}:mailto:{organizer_email}",
            f"ATTENDEE;CN={attendee_email};ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{attendee_email}",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )
    return ("invite.ics", ics.encode("utf-8"), "text/calendar; method=REQUEST; charset=UTF-8")


def _store_dev_mailbox_message(
    *,
    to_email: str,
    from_email: str,
    from_name: str | None,
    reply_to: str | None,
    subject: str,
    text_body: str,
    html_body: str | None,
    delivery_mode: str,
    transport_status: str,
    provider_message_id: str | None = None,
    transport_details: str | None = None,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
) -> None:
    with db.engine.begin() as conn:
        conn.execute(
            DevMailboxMessage.__table__.insert().values(
                to_email=to_email,
                from_email=from_email,
                from_name=from_name,
                reply_to=reply_to,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                delivery_mode=delivery_mode,
                transport_status=transport_status,
                provider_message_id=provider_message_id,
                transport_details=transport_details,
                smtp_host=smtp_host,
                smtp_port=smtp_port,
            )
        )


def _send_via_gmail_api(
    *,
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str | None,
    attachments: list[tuple[str, bytes, str]] | None,
    from_email: str,
    from_name: str,
    reply_to: str | None,
    service_account_file: str,
    workspace_sender: str,
) -> dict[str, object]:
    try:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import service_account
    except ModuleNotFoundError as exc:
        raise MailDeliveryError(
            "Gmail API mail requires the google-auth package. Install the system package or pip dependency first."
        ) from exc

    scopes = ["https://www.googleapis.com/auth/gmail.send"]
    credentials = service_account.Credentials.from_service_account_file(
        service_account_file,
        scopes=scopes,
    ).with_subject(workspace_sender)
    credentials.refresh(GoogleAuthRequest())

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = _sender_header(from_name, from_email)
    message["To"] = to_email
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    for attachment in attachments or []:
        filename, content, content_type = attachment
        maintype, subtype = content_type.split(";", 1)[0].split("/", 1)
        message.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    response = requests.post(
        f"https://gmail.googleapis.com/gmail/v1/users/{workspace_sender}/messages/send",
        headers={
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        },
        json={"raw": raw_message},
        timeout=20,
    )
    if response.status_code >= 400:
        raise MailDeliveryError(f"Gmail API send failed: {response.status_code} {response.text}")
    payload = response.json() if response.content else {}
    return {
        "provider_message_id": str(payload.get("id") or "") or None,
        "transport_details": json.dumps(
            {
                "status_code": response.status_code,
                "response": payload,
            },
            indent=2,
            sort_keys=True,
        ),
    }


def send_email(
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
    from_email: str | None = None,
    from_name: str | None = None,
    reply_to: str | None = None,
) -> None:
    cfg = _mail_config()
    provider = str(cfg["provider"] or "smtp").strip().lower()
    host = cfg["host"]
    resolved_from_email = from_email or cfg["from_email"] or cfg["google_workspace_sender"]
    resolved_from_name = from_name or str(cfg["from_name"])
    if not resolved_from_email:
        raise MailDeliveryError("Email sender is not configured.")
    if provider == "smtp" and cfg["use_ssl"] and cfg["use_tls"]:
        raise MailDeliveryError("Set only one of SMTP_USE_SSL or SMTP_USE_TLS.")

    if cfg["dev_mailbox_capture_only"]:
        if cfg["dev_mailbox_enabled"]:
            _store_dev_mailbox_message(
                to_email=to_email,
                from_email=str(resolved_from_email),
                from_name=str(resolved_from_name),
                reply_to=reply_to,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                delivery_mode="captured",
                transport_status="captured",
                transport_details=json.dumps(
                    {
                        "provider": provider,
                        "capture_only": True,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                smtp_host=str(host) if host else None,
                smtp_port=int(cfg["port"]) if cfg.get("port") else None,
            )
        return

    if provider == "gmail_api":
        service_account_file = str(cfg["google_service_account_file"] or "")
        workspace_sender = str(cfg["google_workspace_sender"] or "")
        if not service_account_file or not workspace_sender:
            raise MailDeliveryError("Gmail API mail is not configured. Set GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_WORKSPACE_SENDER.")
        if not Path(service_account_file).exists():
            raise MailDeliveryError("Google service account file was not found.")
        resolved_from_email = workspace_sender
        try:
            send_result = _send_via_gmail_api(
                to_email=to_email,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                attachments=attachments,
                from_email=str(resolved_from_email),
                from_name=str(resolved_from_name),
                reply_to=reply_to,
                service_account_file=service_account_file,
                workspace_sender=workspace_sender,
            )
            if cfg["dev_mailbox_enabled"]:
                _store_dev_mailbox_message(
                    to_email=to_email,
                    from_email=str(resolved_from_email),
                    from_name=str(resolved_from_name),
                    reply_to=reply_to,
                    subject=subject,
                    text_body=text_body,
                    html_body=html_body,
                    delivery_mode="gmail_api",
                    transport_status="accepted",
                    provider_message_id=send_result.get("provider_message_id"),
                    transport_details=str(send_result.get("transport_details") or ""),
                )
            return
        except Exception as exc:
            if cfg["dev_mailbox_enabled"]:
                _store_dev_mailbox_message(
                    to_email=to_email,
                    from_email=str(resolved_from_email),
                    from_name=str(resolved_from_name),
                    reply_to=reply_to,
                    subject=subject,
                    text_body=text_body,
                    html_body=html_body,
                    delivery_mode="gmail_api",
                    transport_status="failed",
                    transport_details=json.dumps(
                        {
                            "provider": "gmail_api",
                            "error": str(exc),
                            "traceback": traceback.format_exc(),
                        },
                        indent=2,
                        sort_keys=True,
                    ),
                )
            if isinstance(exc, MailDeliveryError):
                raise
            raise MailDeliveryError(str(exc)) from exc

    if not host:
        raise MailDeliveryError("SMTP is not configured. Set SMTP_HOST or enable DEV_MAILBOX_CAPTURE_ONLY.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = _sender_header(str(resolved_from_name), str(resolved_from_email))
    message["To"] = to_email
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    for attachment in attachments or []:
        filename, content, content_type = attachment
        maintype, subtype = content_type.split(";", 1)[0].split("/", 1)
        message.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)

    smtp_cls = smtplib.SMTP_SSL if cfg["use_ssl"] else smtplib.SMTP
    smtp_port = int(cfg["port"])
    try:
        with smtp_cls(str(host), smtp_port, timeout=int(cfg["timeout"])) as smtp:
            smtp_debug: list[str] = []

            def _capture(line: str) -> None:
                smtp_debug.append(line)

            smtp.set_debuglevel(0)
            try:
                smtp._print_debug = _capture
            except Exception:
                pass
            if not cfg["use_ssl"] and cfg["use_tls"]:
                smtp.starttls()
            if cfg["username"]:
                smtp.login(str(cfg["username"]), str(cfg["password"] or ""))
            refused = smtp.send_message(message)
            if refused:
                raise MailDeliveryError(f"SMTP refused recipients: {refused}")
            if cfg["dev_mailbox_enabled"]:
                _store_dev_mailbox_message(
                    to_email=to_email,
                    from_email=str(resolved_from_email),
                    from_name=str(resolved_from_name),
                    reply_to=reply_to,
                    subject=subject,
                    text_body=text_body,
                    html_body=html_body,
                    delivery_mode="smtp",
                    transport_status="accepted",
                    provider_message_id=message.get("Message-ID"),
                    transport_details=json.dumps(
                        {
                            "provider": "smtp",
                            "host": str(host),
                            "port": smtp_port,
                            "username": str(cfg["username"] or ""),
                            "tls": bool(cfg["use_tls"]),
                            "ssl": bool(cfg["use_ssl"]),
                            "refused_recipients": refused,
                            "message_id": message.get("Message-ID"),
                            "debug": smtp_debug,
                        },
                        indent=2,
                        sort_keys=True,
                        default=str,
                    ),
                    smtp_host=str(host),
                    smtp_port=smtp_port,
                )
    except Exception as exc:
        if cfg["dev_mailbox_enabled"]:
            _store_dev_mailbox_message(
                to_email=to_email,
                from_email=str(resolved_from_email),
                from_name=str(resolved_from_name),
                reply_to=reply_to,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                delivery_mode="smtp",
                transport_status="failed",
                provider_message_id=message.get("Message-ID"),
                transport_details=json.dumps(
                    {
                        "provider": "smtp",
                        "host": str(host),
                        "port": smtp_port,
                        "username": str(cfg["username"] or ""),
                        "tls": bool(cfg["use_tls"]),
                        "ssl": bool(cfg["use_ssl"]),
                        "error": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                    indent=2,
                    sort_keys=True,
                ),
                smtp_host=str(host),
                smtp_port=smtp_port,
            )
        raise MailDeliveryError(str(exc)) from exc


def send_magic_link_email(
    *,
    to_email: str,
    invite_url: str | None = None,
    manage_url: str | None = None,
    accept_url: str | None = None,
    decline_url: str | None = None,
    project_name: str | None,
    task_title: str | None,
    subtask_title: str | None = None,
    inviter_email: str | None = None,
    inviter_name: str | None = None,
) -> None:
    subject_target = subtask_title or task_title or "task"
    subject = f"Task for your review: {subject_target}"
    intro = f"You have a task ready for review."
    primary_url = manage_url or invite_url or ""
    project_line = f"Project: {project_name}\n" if project_name else ""
    task_line = f"Task: {task_title}\n" if task_title else ""
    subtask_line = f"Subtask: {subtask_title}\n" if subtask_title else ""
    text_body = (
        f"{intro}\n\n"
        f"{project_line}"
        f"{task_line}"
        f"{subtask_line}"
        f"\nOpen this task:\n{primary_url}\n"
    )
    label_html = escape(task_title or "Task")
    if subtask_title:
        label_html += " / " + escape(subtask_title)
    if project_name:
        label_html += ' <span style="color:#5f6b7a;">[' + escape(project_name) + "]</span>"
    body_html = _email_task_row(
        label_html=label_html,
        open_url=primary_url,
        accept_url=accept_url,
        decline_url=decline_url,
    )
    html_body = _render_email_template(
        eyebrow="Termin Task",
        title="A task is ready for you",
        intro=intro,
        body_html=body_html,
    )
    send_email(
        to_email=to_email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        reply_to=inviter_email,
    )


def send_magic_link_digest_email(
    *,
    to_email: str,
    manage_url: str | None,
    accept_all_url: str | None,
    decline_all_url: str | None,
    items: list[dict[str, str | None]],
    inviter_email: str | None = None,
    inviter_name: str | None = None,
) -> None:
    count = len(items)
    subject = f"{count} tasks are ready for your review"
    text_lines = [
        f"You have {count} tasks ready for review.",
        "",
    ]
    html_parts = []
    if accept_all_url or decline_all_url:
        bulk_actions = []
        if accept_all_url:
            bulk_actions.append(_email_subtle_button(accept_all_url, "✓ Accept all", tone="primary"))
        if decline_all_url:
            bulk_actions.append(_email_subtle_button(decline_all_url, "Decline all", tone="danger"))
        html_parts.append('<div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">' + "".join(bulk_actions) + "</div>")
    html_parts.append('<div style="margin-top:10px;">')

    for item in items:
        project_name = item.get("project_name")
        task_title = item.get("task_title") or "Task"
        subtask_title = item.get("subtask_title")
        invite_url = item.get("invite_url") or ""
        item_url = item.get("portal_url") or manage_url or invite_url
        accept_url = item.get("accept_url") or ""
        decline_url = item.get("decline_url") or ""
        line = f"- {task_title}"
        if subtask_title:
            line += f" / {subtask_title}"
        if project_name:
            line += f" [{project_name}]"
        text_lines.extend([line, f"  {item_url}", ""])

        html_label = escape(task_title)
        if subtask_title:
            html_label += " / " + escape(subtask_title)
        if project_name:
            html_label += ' <span style="color:#5f6b7a;">[' + escape(project_name) + "]</span>"
        html_parts.append(
            _email_task_row(
                label_html=html_label,
                open_url=item_url,
                accept_url=accept_url,
                decline_url=decline_url,
            )
        )

    html_parts.append("</div>")
    if manage_url:
        text_lines.extend(["Open your collaborator portal:", manage_url, ""])

    html_body = _render_email_template(
        eyebrow="Termin Tasks",
        title=f"{count} tasks are ready for you",
        intro=f"You have {count} tasks ready for review.",
        body_html="".join(html_parts),
    )

    send_email(
        to_email=to_email,
        subject=subject,
        text_body="\n".join(text_lines),
        html_body=html_body,
        reply_to=inviter_email,
    )


def send_account_verification_email(
    *,
    to_email: str,
    verify_url: str,
    purpose: str,
    inviter_email: str | None = None,
    inviter_name: str | None = None,
) -> None:
    if purpose == "merge_account":
        subject = "Verify account merge"
        intro = "Use this secure link to approve merging another account into yours."
    else:
        subject = "Verify email for your account"
        intro = "Use this secure link to add this email to your account."

    text_body = f"{intro}\n\nOpen this secure verification link:\n{verify_url}\n"
    html_body = _render_email_template(
        eyebrow="Termin Verification",
        title="Verify your email",
        intro=intro,
        body_html='<div style="margin-top:22px;">' + _email_button(verify_url, "Open verification link") + "</div>",
    )
    send_email(
        to_email=to_email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        reply_to=inviter_email,
    )


def send_calendar_invite_email(
    *,
    to_email: str,
    open_url: str,
    complete_url: str | None,
    summary: str,
    description: str,
    start_at: datetime,
    end_at: datetime,
    uid: str,
    from_email: str | None = None,
    from_name: str | None = None,
    reply_to: str | None = None,
) -> None:
    subject = f"Calendar invite: {summary}"
    text_body = (
        f"{summary}\n\n"
        f"{description}\n\n"
        f"Open task:\n{open_url}\n"
    )
    html_body = _render_email_template(
        eyebrow="Termin Calendar",
        title="Calendar invitation",
        intro="This task is available as a calendar event.",
        body_html=_email_task_row(label_html=escape(summary), open_url=open_url, complete_url=complete_url),
    )
    organizer_email = (
        from_email
        or str(current_app.config.get("MAIL_FROM_EMAIL") or current_app.config.get("GOOGLE_WORKSPACE_SENDER") or "")
    )
    organizer_name = from_name or str(current_app.config.get("MAIL_FROM_NAME") or "Termin")
    attachment = _calendar_attachment(
        uid=uid,
        summary=summary,
        description=description,
        start_at=start_at,
        end_at=end_at,
        attendee_email=to_email,
        organizer_email=organizer_email,
        organizer_name=organizer_name,
    )
    send_email(
        to_email=to_email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        attachments=[attachment],
        from_email=from_email,
        from_name=from_name,
        reply_to=reply_to,
    )
