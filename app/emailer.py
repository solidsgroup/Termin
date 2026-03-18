from __future__ import annotations

from email.message import EmailMessage
from html import escape
import smtplib

from flask import current_app

from app.extensions import db
from app.models import DevMailboxMessage


class MailDeliveryError(RuntimeError):
    pass


def _mail_config() -> dict[str, object]:
    return {
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
) -> None:
    db.session.add(
        DevMailboxMessage(
            to_email=to_email,
            from_email=from_email,
            from_name=from_name,
            reply_to=reply_to,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            delivery_mode=delivery_mode,
        )
    )


def send_email(
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
    from_email: str | None = None,
    from_name: str | None = None,
    reply_to: str | None = None,
) -> None:
    cfg = _mail_config()
    host = cfg["host"]
    resolved_from_email = from_email or cfg["from_email"]
    resolved_from_name = from_name or str(cfg["from_name"])
    if not resolved_from_email:
        raise MailDeliveryError("Email sender is not configured.")
    if cfg["use_ssl"] and cfg["use_tls"]:
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
            )
        return

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

    smtp_cls = smtplib.SMTP_SSL if cfg["use_ssl"] else smtplib.SMTP
    try:
        with smtp_cls(str(host), int(cfg["port"]), timeout=int(cfg["timeout"])) as smtp:
            if not cfg["use_ssl"] and cfg["use_tls"]:
                smtp.starttls()
            if cfg["username"]:
                smtp.login(str(cfg["username"]), str(cfg["password"] or ""))
            smtp.send_message(message)
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
                )
    except Exception as exc:
        raise MailDeliveryError(str(exc)) from exc


def send_magic_link_email(
    *,
    to_email: str,
    invite_url: str,
    manage_url: str | None = None,
    project_name: str | None,
    task_title: str | None,
    subtask_title: str | None = None,
    inviter_email: str | None = None,
    inviter_name: str | None = None,
) -> None:
    subject_target = subtask_title or task_title or "task"
    subject = f"Magic link for {subject_target}"
    intro = f"You've been invited to collaborate on {subject_target}."
    project_line = f"Project: {project_name}\n" if project_name else ""
    task_line = f"Task: {task_title}\n" if task_title else ""
    subtask_line = f"Subtask: {subtask_title}\n" if subtask_title else ""
    text_body = (
        f"{intro}\n\n"
        f"{project_line}"
        f"{task_line}"
        f"{subtask_line}"
        f"\nOpen this secure link to respond:\n{invite_url}\n"
    )
    if manage_url:
        text_body += f"\nManage all your collaborator settings and decisions:\n{manage_url}\n"
    html_body = (
        "<p>" + escape(intro) + "</p>"
        + ("<p><strong>Project:</strong> " + escape(project_name) + "</p>" if project_name else "")
        + ("<p><strong>Task:</strong> " + escape(task_title) + "</p>" if task_title else "")
        + ("<p><strong>Subtask:</strong> " + escape(subtask_title) + "</p>" if subtask_title else "")
        + '<p><a href="' + escape(invite_url) + '">Open secure magic link</a></p>'
    )
    if manage_url:
        html_body += '<p><a href="' + escape(manage_url) + '">Manage collaborator settings and decisions</a></p>'
    send_email(
        to_email=to_email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        from_email=inviter_email,
        from_name=inviter_name,
        reply_to=inviter_email,
    )


def send_magic_link_digest_email(
    *,
    to_email: str,
    manage_url: str | None,
    items: list[dict[str, str | None]],
    inviter_email: str | None = None,
    inviter_name: str | None = None,
) -> None:
    count = len(items)
    subject = f"Magic links for {count} tasks"
    text_lines = [
        f"You've been invited to collaborate on {count} tasks.",
        "",
    ]
    html_parts = [f"<p>You've been invited to collaborate on {count} tasks.</p>", "<ul>"]

    for item in items:
        project_name = item.get("project_name")
        task_title = item.get("task_title") or "Task"
        subtask_title = item.get("subtask_title")
        invite_url = item.get("invite_url") or ""
        line = f"- {task_title}"
        if subtask_title:
            line += f" / {subtask_title}"
        if project_name:
            line += f" [{project_name}]"
        text_lines.extend([line, f"  {invite_url}", ""])

        html_label = escape(task_title)
        if subtask_title:
            html_label += " / " + escape(subtask_title)
        if project_name:
            html_label += " <span style=\"color:#6b7280;\">[" + escape(project_name) + "]</span>"
        html_parts.append(f'<li>{html_label}<br /><a href="{escape(invite_url)}">Open secure magic link</a></li>')

    html_parts.append("</ul>")
    if manage_url:
        text_lines.extend(["Manage all your collaborator settings and decisions:", manage_url, ""])
        html_parts.append(f'<p><a href="{escape(manage_url)}">Manage collaborator settings and decisions</a></p>')

    send_email(
        to_email=to_email,
        subject=subject,
        text_body="\n".join(text_lines),
        html_body="".join(html_parts),
        from_email=inviter_email,
        from_name=inviter_name,
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
    html_body = (
        "<p>" + escape(intro) + "</p>"
        + '<p><a href="' + escape(verify_url) + '">Open secure verification link</a></p>'
    )
    send_email(
        to_email=to_email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        from_email=inviter_email,
        from_name=inviter_name,
        reply_to=inviter_email,
    )
