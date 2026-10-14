"""Builds the compact, factual text the LLM sees about an order / ticket.

Only data the system owns (order lines, amounts, timestamps) plus the customer's own
words go in - never other customers' data, never staff emails or secrets.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Order, Ticket, TicketEvent
from ..schemas import ISSUE_LABELS


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt(dt: datetime | None) -> str:
    return _aware(dt).strftime("%Y-%m-%d %H:%M UTC") if dt else "n/a"


def delivery_delay_minutes(order: Order) -> int | None:
    """Positive = delivered late by N minutes; None if not delivered or no promise."""
    if not order.delivered_at or not order.promised_by:
        return None
    return int((_aware(order.delivered_at) - _aware(order.promised_by)).total_seconds() // 60)


def order_summary(order: Order | None) -> str:
    if order is None:
        return "No order linked."
    lines = [f"Order {order.order_number} | status: {order.status} | payment: {order.payment_method}",
             f"Placed: {_fmt(order.placed_at)} | promised by: {_fmt(order.promised_by)} | "
             f"delivered: {_fmt(order.delivered_at)}"]
    delay = delivery_delay_minutes(order)
    if delay is not None:
        lines.append(f"Delivery was {'LATE by ' + str(delay) + ' min' if delay > 0 else 'on time'}")
    lines.append("Items:")
    lines += [f"  - {i.name} x{i.quantity} @ INR {i.unit_price:.2f} = INR {i.line_total:.2f}" for i in order.items]
    lines.append(f"Delivery fee: INR {order.delivery_fee:.2f} | Order total: INR {order.total:.2f}")
    return "\n".join(lines)


def triage_context(order: Order | None, issue_type: str | None, affected_items: list[str]) -> str:
    parts = [order_summary(order)]
    if issue_type:
        parts.append(f"Customer selected issue: {ISSUE_LABELS.get(issue_type, issue_type)}")
    if affected_items:
        parts.append("Customer marked affected items: " + ", ".join(affected_items))
    return "\n".join(parts)


def customer_history(db: Session, customer_id: int | None) -> dict | None:
    if not customer_id:
        return None
    from ..db.models import User

    user = db.get(User, customer_id)
    orders = db.scalar(select(func.count(Order.id)).where(Order.customer_id == customer_id)) or 0
    tickets = db.scalar(select(func.count(Ticket.id)).where(Ticket.customer_id == customer_id)) or 0
    refunds = db.scalar(select(func.count(Ticket.id)).where(
        Ticket.customer_id == customer_id, Ticket.status == "resolved",
        Ticket.category.in_(("refund", "damaged")))) or 0
    return {"orders_total": orders, "tickets_total": tickets, "refunds_resolved": refunds,
            "member_since": user.created_at if user else None}


def assist_case(db: Session, ticket: Ticket) -> str:
    """Everything the AI assistant may use to propose a resolution."""
    hist = customer_history(db, ticket.customer_id) or {}
    notes = db.scalars(select(TicketEvent.detail).where(TicketEvent.ticket_id == ticket.id,
                                                        TicketEvent.action == "note")).all()
    return "\n".join([
        f"Ticket {ticket.reference} | category: {ticket.category} | priority: P{ticket.priority} | "
        f"status: {ticket.status}",
        f"Reported: {_fmt(ticket.created_at)}",
        f"Customer issue type: {ISSUE_LABELS.get(ticket.issue_type or '', ticket.issue_type or 'n/a')}",
        f"Affected items (customer): {', '.join(ticket.affected_items or []) or 'not specified'}",
        f"Photos attached: {len(ticket.attachments)}",
        f"Customer history: {hist.get('orders_total', 0)} orders, {hist.get('tickets_total', 0)} tickets, "
        f"{hist.get('refunds_resolved', 0)} past refunds/damage claims resolved",
        "",
        order_summary(ticket.order),
        "",
        f"Customer subject: {ticket.subject}",
        f"Customer message: {ticket.body}",
        *([""] + ["Agent notes so far:"] + [f"  - {n}" for n in notes] if notes else []),
    ])
