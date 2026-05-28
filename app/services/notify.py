from __future__ import annotations

import threading

from sqlalchemy.orm import Session

from app.models.notification import Notification


def _send_assignment_email(to_email: str, entity_type: str, entity_name: str, link: str) -> None:
    """Fire-and-forget SMTP send — runs in a daemon thread so it never blocks the request."""
    try:
        from app.services.smtp_mail import is_smtp_configured, smtp_send
        from app.config import get_settings
        if not is_smtp_configured():
            return
        base_url = get_settings().ms_redirect_uri.rsplit("/", 1)[0] if get_settings().ms_redirect_uri else ""
        full_link = f"{base_url}{link}" if base_url else link
        subject = f"[Radixsol CRM] You were assigned a {entity_type}: {entity_name}"
        body_html = (
            f"<p>Hi,</p>"
            f"<p>You have been assigned a new <strong>{entity_type}</strong>: "
            f"<strong>{entity_name}</strong>.</p>"
            f'<p><a href="{full_link}">View {entity_type} →</a></p>'
            f"<p style='color:#94a3b8;font-size:12px'>Radixsol CRM · assignment notification</p>"
        )
        smtp_send([to_email], None, subject, body_html)
    except Exception as exc:
        print(f"[notify] email to {to_email} failed: {exc}")


def notify_assignment(
    db: Session,
    *,
    assignee_id: int,
    assigned_by_id: int,
    entity_type: str,   # "contact" | "company"
    entity_name: str,
    link: str,
) -> None:
    """Create an in-app notification and send an email to the assigned user.
    Skips entirely if assigner == assignee (self-assignment)."""
    if assignee_id == assigned_by_id:
        return

    # In-app notification
    msg = f"You were assigned a {entity_type}: {entity_name}"
    db.add(Notification(user_id=assignee_id, message=msg, link=link))

    # Email notification — look up the assignee's email address
    from app.models import User
    assignee = db.get(User, assignee_id)
    if assignee and assignee.email:
        t = threading.Thread(
            target=_send_assignment_email,
            args=(assignee.email, entity_type, entity_name, link),
            daemon=True,
        )
        t.start()
