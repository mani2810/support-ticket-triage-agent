"""E-mail notifications.

* SMTP_HOST set  -> real e-mail via SMTP (STARTTLS)
* otherwise      -> written as .eml files to backend/outbox/ (open them in any mail client) - handy locally

Sent from FastAPI BackgroundTasks AFTER the response, so a slow or broken mail server never delays
or fails a user's request; failures are logged. Messages contain no internal notes, AI reasoning or
staff e-mail addresses - only what the recipient may see anyway.
"""
import logging
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from ..core.config import get_settings

log = logging.getLogger("notify")


def _build(to: str, subject: str, body: str) -> EmailMessage:
    s = get_settings()
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.smtp_from, to, subject
    msg.set_content(body + f"\n\n--\nFreshCart Support · {s.app_url}\nThis is an automated message.")
    return msg


def send(to: str, subject: str, body: str) -> None:
    s = get_settings()
    if not s.notify_enabled or not to:
        return
    msg = _build(to, subject, body)
    try:
        if s.smtp_host:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as smtp:
                smtp.starttls()
                if s.smtp_user:
                    smtp.login(s.smtp_user, s.smtp_password or "")
                smtp.send_message(msg)
            channel = "smtp"
        else:
            outbox = Path(s.outbox_dir)
            outbox.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            safe_to = re.sub(r"[^a-zA-Z0-9.@_-]", "_", to)
            (outbox / f"{stamp}_{safe_to}.eml").write_bytes(bytes(msg))
            channel = "outbox"
        log.info("notification sent", extra={"channel": channel, "subject": subject})
    except Exception as exc:  # never let a notification failure surface to the user
        log.error("notification failed: %s", type(exc).__name__, extra={"subject": subject})


# ---------------------------------------------------------------- templates
def ticket_created(email: str, name: str, ref: str, order: str | None) -> tuple:
    return (email, f"We received your request {ref}",
            f"Hi {name},\n\nThanks for contacting FreshCart. Your ticket {ref}"
            + (f" for order {order}" if order else "") + " has been created and routed to our team.\n"
            "You can follow it any time under My tickets.")


def support_replied(email: str, name: str, ref: str, text: str, resolved: bool) -> tuple:
    head = "has been resolved" if resolved else "has a new reply"
    return (email, f"Your ticket {ref} {head}",
            f"Hi {name},\n\nYour ticket {ref} {head}:\n\n{text}\n\n"
            "Reply under My tickets if anything is still not right - it will re-open the ticket.")


def customer_replied(agent_email: str, agent_name: str, ref: str, customer: str, text: str) -> tuple:
    return (agent_email, f"[{ref}] {customer} replied",
            f"Hi {agent_name},\n\n{customer} replied on {ref}:\n\n{text}\n\nOpen your queue to respond.")
