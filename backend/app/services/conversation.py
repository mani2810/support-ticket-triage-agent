"""Customer <-> support conversation on a ticket.

Messages are ticket events with dedicated actions, so they share the audit log:
    "reply"             staff -> customer   (visible to the customer)
    "customer_message"  customer -> staff
    "note"              staff internal note (NEVER visible to the customer)

The thread starts with the customer's original description. A customer message on a
resolved ticket re-opens it, so nothing a customer writes can go unnoticed.
"""
import logging

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Ticket, TicketEvent, User, utcnow

log = logging.getLogger(__name__)
REPLY, CUSTOMER_MSG = "reply", "customer_message"
MESSAGE_ACTIONS = {REPLY, CUSTOMER_MSG}


def _touch(ticket: Ticket) -> None:
    ticket.updated_at = utcnow()          # events don't change ticket columns; bump "last activity"


def add_staff_reply(ticket: Ticket, user: User, body: str) -> None:
    """Message to the customer. Does not commit (callers combine it with status changes)."""
    ticket.events.append(TicketEvent(actor=user.email, action=REPLY, detail=body.strip()))
    _touch(ticket)


def staff_reply(db: Session, ticket: Ticket, user: User, body: str, resolve: bool = False) -> Ticket:
    from .tickets import change_status

    if resolve:
        return change_status(db, ticket, "resolved", body, user)        # posts the reply (also if already resolved)
    add_staff_reply(ticket, user, body)
    if ticket.status == "open":                                        # someone is on it now
        ticket.events.append(TicketEvent(actor=user.email, action="status", detail="open -> in_progress"))
        ticket.status = "in_progress"
    db.commit()
    db.refresh(ticket)
    log.info("staff reply", extra={"ticket_id": ticket.id})
    return ticket


def customer_message(db: Session, ticket: Ticket, user: User, body: str) -> Ticket:
    if ticket.customer_id != user.id:                                   # defence in depth (route checks too)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
    ticket.events.append(TicketEvent(actor="customer", action=CUSTOMER_MSG, detail=body.strip()))
    if ticket.status == "resolved":
        ticket.events.append(TicketEvent(actor="customer", action="status",
                                         detail="resolved -> open (customer replied)"))
        ticket.status, ticket.resolved_at = "open", None
    _touch(ticket)
    db.commit()
    db.refresh(ticket)
    log.info("customer message", extra={"ticket_id": ticket.id})
    return ticket


def _staff_names(db: Session, ticket: Ticket) -> dict[str, str]:
    emails = {e.actor for e in ticket.events if "@" in e.actor}
    if not emails:
        return {}
    return dict(db.execute(select(User.email, User.name).where(User.email.in_(emails))).all())


def thread(db: Session, ticket: Ticket, for_customer: bool) -> list[dict]:
    """Chronological messages. Staff identities are shown to customers by first name only."""
    names = _staff_names(db, ticket)
    msgs = [{"role": "customer", "author": "You" if for_customer else ticket.customer_name,
             "text": ticket.body, "created_at": ticket.created_at}]
    for e in ticket.events:
        text = None
        if e.action == CUSTOMER_MSG:
            role, author, text = "customer", ("You" if for_customer else ticket.customer_name), e.detail
        elif e.action == REPLY:
            role, author, text = "support", _support_name(names.get(e.actor, e.actor), for_customer), e.detail
        elif e.action == "status" and ": " in e.detail:
            # Older tickets: a message typed with a status change was stored inside the status event.
            role, author, text = "support", _support_name(names.get(e.actor, e.actor), for_customer), \
                e.detail.split(": ", 1)[1]
        if text:
            msgs.append({"role": role, "author": author, "text": text, "created_at": e.created_at})
    return msgs


def _support_name(name: str, for_customer: bool) -> str:
    if not for_customer:
        return name
    first = name.split("@")[0].split()[0].title() if name else "Support"
    return f"{first} (FreshCart Support)"


def awaiting(messages: list[dict], ticket_status: str) -> str | None:
    """Who is expected to reply next: 'support' or 'customer' (None when resolved)."""
    if ticket_status == "resolved" or not messages:
        return None
    return "support" if messages[-1]["role"] == "customer" else "customer"
