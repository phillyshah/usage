"""Outbound email.

The configuration contract — variable names, the provider field, and returning
a *reason* instead of raising — is deliberately identical to the Maxx dashboard's
``lib/mailer.ts``, so one set of relay credentials copies between the two
projects without translation, and "no email configured yet" stays a normal state
the product can ship in and describe in words.

What is NOT copied is that project's hand-rolled SMTP client. It exists because
Node has no SMTP in its standard library; Python does. ``smtplib.SMTP_SSL``
already speaks EHLO/AUTH/MAIL FROM/RCPT TO/DATA, and ``EmailMessage`` already
assembles the MIME and does the dot-stuffing that file warns about — so the
whole protocol becomes the four lines in ``_send_smtp``.

Implicit TLS on 465, the same choice and for the same reason: STARTTLS opens in
plaintext and upgrades, so a downgrade is possible and every failure mode
doubles. The relay offers 465; nothing here needs the other one.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

from app.config import settings

log = logging.getLogger("email")

PROVIDERS = ("smtp", "resend", "postmark")


@dataclass(frozen=True)
class EmailConfig:
    provider: str
    sender: str
    host: str = ""
    port: int = 465
    user: str = ""
    password: str = ""
    api_key: str = ""


def email_configuration(cfg=None) -> tuple[EmailConfig | None, str | None]:
    """``(config, None)`` when mail can be sent, ``(None, reason)`` when it can't.

    A reason rather than an exception: an unconfigured relay is not a bug, it is
    the state every install starts in, and the Notifications card has to be able
    to say which value is missing rather than rendering a traceback.
    """
    cfg = cfg or settings
    provider = (cfg.email_provider or "smtp").strip().lower()
    sender = (cfg.email_from or "").strip()

    if provider not in PROVIDERS:
        return None, (f'EMAIL_PROVIDER must be one of {", ".join(PROVIDERS)} — '
                      f'not "{provider}".')
    if not sender:
        return None, ("EMAIL_FROM is not set. It must be an address the relay is "
                      "authenticated to send as.")
    if "@" not in sender:
        return None, f"EMAIL_FROM does not look like an address: {sender}"

    if provider == "smtp":
        host = (cfg.smtp_host or "").strip()
        user = (cfg.smtp_user or "").strip()
        password = cfg.smtp_password or ""
        port = int(cfg.smtp_port or 465)
        if not host:
            return None, "SMTP_HOST is not set, so nothing can be sent yet."
        if not (user and password):
            return None, "SMTP_USER and SMTP_PASSWORD are not both set."
        if port <= 0:
            return None, f"SMTP_PORT is not a port number: {cfg.smtp_port}"
        return EmailConfig("smtp", sender, host, port, user, password), None

    # resend / postmark are accepted in the contract because the dashboard's
    # .env may name them, but this app has no HTTP sender: it runs behind the
    # same relay and adding a second path nobody uses is a second thing to break.
    return None, (f'EMAIL_PROVIDER="{provider}" is configured, but this app only '
                  'sends over SMTP. Set EMAIL_PROVIDER=smtp and the SMTP_* '
                  'values to the same relay the dashboard uses.')


def build_message(config: EmailConfig, to: list[str], subject: str,
                  text: str, html: str | None = None) -> EmailMessage:
    """Assemble the MIME. Split from sending so the wording can be tested
    without putting real mail on the wire."""
    msg = EmailMessage()
    msg["From"] = config.sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def recipients(raw: str | None) -> list[str]:
    """Split a stored recipient string, de-duplicated, order preserved."""
    seen: dict[str, None] = {}
    for part in (raw or "").replace(";", ",").replace("\n", ",").split(","):
        addr = part.strip()
        if addr and "@" in addr:
            seen.setdefault(addr, None)
    return list(seen)


def _send_smtp(config: EmailConfig, msg: EmailMessage) -> None:
    with smtplib.SMTP_SSL(config.host, config.port,
                          context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(config.user, config.password)
        smtp.send_message(msg)


def send_email(to: list[str], subject: str, text: str,
               html: str | None = None, cfg=None) -> tuple[bool, str]:
    """``(ok, detail)``. Never raises.

    The relay's own words are carried back on refusal: "login denied" and
    "mailbox unavailable" are different problems with different fixes, and only
    it knows which one happened. Returning them instead of a generic failure is
    what stops the next outage taking a day to diagnose.
    """
    config, reason = email_configuration(cfg)
    if config is None:
        return False, reason or "email is not configured"
    if not to:
        return False, "No recipients are set, so there is nobody to notify."
    try:
        _send_smtp(config, build_message(config, to, subject, text, html))
    except smtplib.SMTPAuthenticationError as exc:
        return False, f"The relay rejected the login: {exc}"
    except smtplib.SMTPException as exc:
        return False, f"The relay refused the message: {exc}"
    except OSError as exc:
        return False, f"Could not reach {config.host}:{config.port} — {exc}"
    return True, f"Sent to {len(to)} recipient(s)."
