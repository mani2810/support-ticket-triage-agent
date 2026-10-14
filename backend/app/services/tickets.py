"""Ticket use-cases: create (with AI triage + images), re-route, reassign, status, views, stats."""
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Order, Ticket, TicketEvent, User, utcnow
from ..schemas import ISSUE_LABELS, TicketCreate
from .attachments import PreparedImage, store_images
from .context import customer_history, triage_context
from .llm import llm_status
from .triage import agent_workloads, run_triage

log = logging.getLogger(__name__)

# Allowed status moves. Admins may additionally re-open resolved tickets.
TRANSITIONS = {"open": {"in_progress", "resolved"}, "in_progress": {"open", "resolved"}, "resolved": set()}


def _new_reference(db: Session) -> str:
    while True:
        ref = f"TKT-{secrets.token_hex(3).upper()}"
        if not db.scalar(select(Ticket.id).where(Ticket.reference == ref)):
            return ref


def _event(ticket: Ticket, actor: str, action: str, detail: str = "") -> None:
    ticket.events.append(TicketEvent(actor=actor, action=action, detail=detail))


def create_ticket(db: Session, data: TicketCreate, order: Order | None = None,
                  images: list[PreparedImage] | None = None) -> Ticket:
    """Classify + route (at most one LLM call), then persist ticket, images and audit events atomically."""
    context = triage_context(order, data.issue_type, data.affected_items)
    decision = run_triage(db, data.subject, data.body, context=context, issue_type=data.issue_type,
                          order_total=order.total if order else 0.0)
    ticket = Ticket(
        reference=_new_reference(db),
        customer_id=data.customer_id,
        order_id=order.id if order else data.order_id,
        customer_name=data.customer_name.strip(),
        customer_email=data.customer_email.lower(),
        issue_type=data.issue_type,
        affected_items=list(data.affected_items),
        subject=data.subject.strip(),
        body=data.body.strip(),
        category=decision["category"],
        priority=decision["priority"],
        classified_by=decision["classified_by"],
        ai_reason=decision["reason"],
        assignment_reason=decision["assignment_reason"],
        assigned_to_id=decision["assigned_agent_id"],
    )
    issue = ISSUE_LABELS.get(data.issue_type or "", "")
    _event(ticket, "customer", "created",
           f"Submitted by {ticket.customer_email}"
           + (f" for order {order.order_number}" if order else "") + (f" ({issue})" if issue else ""))
    _event(ticket, "system", "classified",
           f"{decision['category']} / P{decision['priority']} by {decision['classified_by']}: {decision['reason']}"
           + (f" ({decision['error']})" if decision.get("error") else ""))
    _event(ticket, "system", "assigned", decision["assignment_reason"])
    written = store_images(ticket, images or [])
    if written:
        _event(ticket, "customer", "attached", f"{len(written)} photo(s)")
    db.add(ticket)
    try:
        db.commit()
    except Exception:
        db.rollback()
        for path in written:                       # don't leave orphan files behind
            path.unlink(missing_ok=True)
        raise
    db.refresh(ticket)
    log.info("ticket created", extra={"ticket_id": ticket.id, "agent": decision["assigned_agent_id"],
                                      "images": len(written)})
    return ticket


# Events a customer may see on their own ticket (no internal reasoning or notes).
CUSTOMER_VISIBLE = {"created", "attached", "status"}


def customer_view(db: Session, ticket: Ticket) -> dict:
    from .conversation import awaiting, thread

    timeline = [e for e in ticket.events if e.action in CUSTOMER_VISIBLE]
    messages = thread(db, ticket, for_customer=True)
    return {
        "reference": ticket.reference, "subject": ticket.subject, "body": ticket.body, "status": ticket.status,
        "priority": ticket.priority, "category": ticket.category, "issue_type": ticket.issue_type,
        "order_number": ticket.order_number, "affected_items": ticket.affected_items or [],
        "assigned_agent": ticket.assignee.name if ticket.assignee else None,
        "created_at": ticket.created_at, "updated_at": ticket.updated_at,
        "resolution_note": ticket.resolution_note, "attachments": ticket.attachments,
        "timeline": [{"actor": "you" if e.actor == "customer" else "support", "action": e.action,
                      "detail": _public_detail(e), "created_at": e.created_at} for e in timeline],
        "messages": messages, "awaiting": awaiting(messages, ticket.status),
    }


def _public_detail(e: TicketEvent) -> str:
    if e.action == "status":
        # "open -> resolved: note" (older format) -> keep only the status move; the note is in `messages`
        return e.detail.split(":", 1)[0]
    if e.action == "created":
        return "Ticket created"
    return e.detail


def staff_detail(db: Session, ticket: Ticket) -> dict:
    from ..schemas import AISuggestionOut, TicketDetail

    data = TicketDetail.model_validate(ticket).model_dump()      # order + items load via relationships
    if ticket.suggestions:
        latest = ticket.suggestions[-1]
        data["suggestion"] = AISuggestionOut.model_validate(latest).model_dump()
        data["suggestion"]["stale"] = any(
            e.action in ("customer_message", "reply", "note", "attached") and e.created_at > latest.created_at
            for e in ticket.events)
    data["customer_history"] = customer_history(db, ticket.customer_id)
    from .conversation import awaiting, thread
    data["messages"] = thread(db, ticket, for_customer=False)
    data["awaiting"] = awaiting(data["messages"], ticket.status)
    return data


def reroute_agent_tickets(db: Session, agent: User, actor: User) -> int:
    """Agent deactivated: move each of their open tickets to the best ACTIVE agent (0 LLM calls)."""
    open_tickets = db.scalars(select(Ticket).where(Ticket.assigned_to_id == agent.id,
                                                   Ticket.status != "resolved")).all()
    for t in open_tickets:
        preset = {"category": t.category, "priority": t.priority, "reason": t.ai_reason,
                  "classified_by": t.classified_by}
        decision = run_triage(db, t.subject, t.body, preset=preset, exclude_agent_id=agent.id)
        t.assigned_to_id, t.assignment_reason = decision["assigned_agent_id"], decision["assignment_reason"]
        _event(t, actor.email, "rerouted", f"{agent.name} was deactivated -> {decision['assignment_reason']}")
    db.commit()
    return len(open_tickets)


def retriage(db: Session, ticket: Ticket, actor: User, reclassify: bool = False) -> Ticket:
    """Re-route a ticket (e.g. after its assignee left).

    reclassify=False (default): keep the stored category/priority -> NO LLM call.
    reclassify=True: ask the LLM again, bypassing the cache (explicit admin action).
    """
    preset = None if reclassify else {"category": ticket.category, "priority": ticket.priority,
                                      "reason": ticket.ai_reason, "classified_by": ticket.classified_by}
    decision = run_triage(db, ticket.subject, ticket.body, preset=preset, use_cache=not reclassify,
                          context=triage_context(ticket.order, ticket.issue_type, ticket.affected_items),
                          issue_type=ticket.issue_type, order_total=ticket.order.total if ticket.order else 0.0)
    ticket.category, ticket.priority = decision["category"], decision["priority"]
    ticket.classified_by, ticket.ai_reason = decision["classified_by"], decision["reason"]
    ticket.assigned_to_id, ticket.assignment_reason = decision["assigned_agent_id"], decision["assignment_reason"]
    _event(ticket, actor.email, "reclassified" if reclassify else "rerouted",
           f"{ticket.category} / P{ticket.priority} -> {decision['assignment_reason']}"
           + (f" ({decision['error']})" if decision.get("error") else ""))
    db.commit()
    db.refresh(ticket)
    return ticket


def reassign(db: Session, ticket: Ticket, agent_id: int, actor: User) -> Ticket:
    agent = db.get(User, agent_id)
    if not agent or agent.role != "agent" or not agent.is_active:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Target must be an active agent")
    old = ticket.assignee.name if ticket.assignee else "unassigned"
    ticket.assigned_to_id = agent.id
    ticket.assignment_reason = f"Manually reassigned by {actor.name}."
    _event(ticket, actor.email, "reassigned", f"{old} -> {agent.name}")
    db.commit()
    db.refresh(ticket)
    return ticket


def change_status(db: Session, ticket: Ticket, new_status: str, note: str | None, actor: User) -> Ticket:
    if new_status == ticket.status:
        # Same status: still deliver the message instead of silently dropping it.
        if note and note.strip():
            from .conversation import add_staff_reply
            add_staff_reply(ticket, actor, note)
            db.commit()
            db.refresh(ticket)
        return ticket
    allowed = TRANSITIONS[ticket.status] | ({"open"} if actor.role == "admin" else set())
    if new_status not in allowed:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot move ticket from {ticket.status} to {new_status}")
    if new_status == "resolved" and not (note and note.strip()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A resolution note is required")
    _event(ticket, actor.email, "status", f"{ticket.status} -> {new_status}")
    if note and note.strip():
        # A message typed with a status change is FOR THE CUSTOMER: post it to the conversation.
        from .conversation import add_staff_reply
        add_staff_reply(ticket, actor, note)
    ticket.status = new_status
    if new_status == "resolved":
        ticket.resolved_at, ticket.resolution_note = utcnow(), note
    elif ticket.resolved_at:
        ticket.resolved_at = None
    db.commit()
    db.refresh(ticket)
    return ticket


def stats(db: Session) -> dict:
    def grouped(col):
        return {str(k): v for k, v in db.execute(select(col, func.count()).group_by(col)).all()}

    total = db.scalar(select(func.count(Ticket.id))) or 0
    llm = db.scalar(select(func.count(Ticket.id)).where(Ticket.classified_by == "llm")) or 0
    resolved = db.execute(select(Ticket.created_at, Ticket.resolved_at).where(Ticket.resolved_at.is_not(None))).all()
    hours = [(_aware(r) - _aware(c)).total_seconds() / 3600 for c, r in resolved]

    since = utcnow() - timedelta(days=13)
    created = db.scalars(select(Ticket.created_at).where(Ticket.created_at >= since)).all()
    days = {(since + timedelta(days=i)).date().isoformat(): 0 for i in range(14)}
    for c in created:
        d = _aware(c).date().isoformat()
        if d in days:
            days[d] += 1

    resolved_by = dict(db.execute(select(Ticket.assigned_to_id, func.count())
                                  .where(Ticket.status == "resolved").group_by(Ticket.assigned_to_id)).all())
    agents = []
    load = {a["id"]: a for a in agent_workloads(db)}
    for u in db.scalars(select(User).where(User.role == "agent").order_by(User.name)).all():
        w = load.get(u.id, {"open_tickets": 0, "high_priority_open": 0})
        agents.append({"id": u.id, "name": u.name, "email": u.email, "skills": u.skills or [],
                       "is_active": u.is_active, "open_tickets": w["open_tickets"],
                       "high_priority_open": w["high_priority_open"], "resolved_total": resolved_by.get(u.id, 0)})
    return {
        "total": total,
        "by_status": grouped(Ticket.status),
        "by_category": grouped(Ticket.category),
        "by_priority": {f"P{k}": v for k, v in grouped(Ticket.priority).items()},
        "unassigned": db.scalar(select(func.count(Ticket.id)).where(Ticket.assigned_to_id.is_(None),
                                                                    Ticket.status != "resolved")) or 0,
        "llm_share": round(llm / total, 3) if total else 0.0,
        "avg_resolution_hours": round(sum(hours) / len(hours), 1) if hours else None,
        "agents": agents,
        "daily_created": [{"date": d, "count": n} for d, n in days.items()],
        "customers": db.scalar(select(func.count(User.id)).where(User.role == "customer")) or 0,
        "orders": db.scalar(select(func.count(Order.id))) or 0,
        "llm": llm_status(),
    }


def _aware(dt: datetime) -> datetime:
    # SQLite returns naive datetimes; treat them as UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
