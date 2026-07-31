"""Email delivery over SMTP.

Configured entirely through environment variables so nothing sensitive lands in
the repo. In GitHub Actions these come from repository secrets.

    SMTP_HOST      default smtp.gmail.com
    SMTP_PORT      default 587 (STARTTLS); 465 switches to implicit TLS
    SMTP_USER      the sending account, e.g. you@gmail.com
    SMTP_PASSWORD  a Gmail *app password*, not the account password
    MAIL_TO        one or more recipients, separated by comma or semicolon;
                   defaults to SMTP_USER
    MAIL_BCC       optional extra recipients, hidden from the others
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


def env(name: str, default: str = "") -> str:
    """Environment value, treating blank as absent.

    GitHub Actions passes an unset secret through as an empty string rather
    than leaving the variable out, so ``os.environ.get(name, default)`` returns
    "" and the default never applies. That turned a missing SMTP_PORT into an
    int('') crash at the moment of sending.
    """
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def parse_recipients(raw: str | None) -> list[str]:
    """Split a recipient list on commas or semicolons.

    Semicolons are accepted because that is what Outlook puts on the clipboard,
    and a secret pasted from there would otherwise become one nonsense address
    that the mail server quietly refuses.
    """
    if not raw:
        return []
    parts = raw.replace(";", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


def send_email(subject: str, text_body: str, html_body: str | None = None) -> None:
    host = env("SMTP_HOST", "smtp.gmail.com")
    user = env("SMTP_USER")
    password = env("SMTP_PASSWORD")

    missing = [n for n, v in (("SMTP_USER", user), ("SMTP_PASSWORD", password)) if not v]
    if missing:
        raise NotConfigured(f"Mangler miljøvariabler: {', '.join(missing)}")

    raw_port = env("SMTP_PORT", "587")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise NotConfigured(f"SMTP_PORT er ikke et tall: {raw_port!r}") from exc

    to = parse_recipients(env("MAIL_TO")) or [user]
    bcc = parse_recipients(env("MAIL_BCC"))

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = env("MAIL_FROM", user)
    message["To"] = ", ".join(to)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
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

    log.info("Sendte e-post til %d mottaker(e)", len(to) + len(bcc))
