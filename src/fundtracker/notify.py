"""Email delivery over SMTP.

Configured entirely through environment variables so nothing sensitive lands in
the repo. In GitHub Actions these come from repository secrets.

    SMTP_HOST      default smtp.gmail.com
    SMTP_PORT      default 587 (STARTTLS); 465 switches to implicit TLS
    SMTP_USER      the sending account, e.g. you@gmail.com
    SMTP_PASSWORD  a Gmail *app password*, not the account password
    MAIL_TO        recipient; defaults to SMTP_USER
    MAIL_FROM      defaults to SMTP_USER
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)


class NotConfigured(RuntimeError):
    """Raised when the SMTP environment is incomplete."""


def send_email(subject: str, text_body: str, html_body: str | None = None) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")

    missing = [n for n, v in (("SMTP_USER", user), ("SMTP_PASSWORD", password)) if not v]
    if missing:
        raise NotConfigured(f"Mangler miljøvariabler: {', '.join(missing)}")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.environ.get("MAIL_FROM", user)
    message["To"] = os.environ.get("MAIL_TO", user)
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(message)

    log.info("Sendte e-post til %s", message["To"])
