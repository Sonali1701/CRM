"""SMTP fallback for sending mail when Microsoft Graph is not connected.

This is the simple path — no OAuth, just credentials. Configure via env:
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_USE_TLS
"""

from __future__ import annotations

import re
import smtplib
from email.message import EmailMessage

from app.config import get_settings


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def is_smtp_configured() -> bool:
    s = get_settings()
    return bool(s.smtp_host and s.smtp_user and s.smtp_password)


def _html_to_plain(html: str) -> str:
    text = html.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = text.replace("</p>", "\n\n").replace("</div>", "\n")
    return _HTML_TAG_RE.sub("", text).strip()


def smtp_send(
    to_list: list[str],
    cc_list: list[str] | None,
    subject: str,
    body_html: str,
) -> None:
    """Send a single email via SMTP. Raises on failure."""
    s = get_settings()
    if not is_smtp_configured():
        raise RuntimeError("SMTP is not configured")

    msg = EmailMessage()
    msg["From"] = s.smtp_from or s.smtp_user
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject

    from app.services.email_format import text_to_html
    html_body = text_to_html(body_html)
    msg.set_content(_html_to_plain(html_body) or " ")
    msg.add_alternative(html_body, subtype="html")

    rcpt = list(to_list) + (cc_list or [])

    if s.smtp_use_tls:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as srv:
            srv.starttls()
            srv.login(s.smtp_user, s.smtp_password)
            srv.send_message(msg, to_addrs=rcpt)
    else:
        with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=30) as srv:
            srv.login(s.smtp_user, s.smtp_password)
            srv.send_message(msg, to_addrs=rcpt)
