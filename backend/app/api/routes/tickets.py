"""Staff ticket endpoints. Agents see only their own queue; admins see everything."""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select

from ...db.models import Attachment, Order, Ticket, TicketEvent
from ...schemas import (
    AISuggestionOut,
    Category,
    CommentIn,
    Reassign,
    StaffReplyIn,
    Status,
    StatusUpdate,
    TicketDetail,
    TicketPage,
    TicketUpdate,
)
from ...services import assist as assist_svc
from ...services import conversation, notify
from ...services import tickets as svc
from ...services.attachments import attachment_path
from ..deps import DB, AccessibleTicket, Admin, Staff

router = APIRouter(prefix="/tickets", tags=["tickets (staff)"])
LIKE_ESCAPE = "!"


def escape_like(text: str) -> str:
    """Make %, _ and the escape char literal in a LIKE pattern."""
    return text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2).replace("%", LIKE_ESCAPE + "%").replace("_", LIKE_ESCAPE + "_")


@router.get("", response_model=TicketPage)
def list_tickets(
    db: DB, user: Staff,
    status_: Status | None = Query(None, alias="status"), category: Category | None = None,
    priority: int | None = Query(None, ge=1, le=3), assignee_id: int | None = None,
    unassigned: bool = False, q: str | None = Query(None, max_length=100),
    page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=100),
):
    stmt = select(Ticket)
    if user.role == "agent":
        stmt = stmt.where(Ticket.assigned_to_id == user.id)
    elif assignee_id:
        stmt = stmt.where(Ticket.assigned_to_id == assignee_id)
    if unassigned:
        stmt = stmt.where(Ticket.assigned_to_id.is_(None))
    if status_:
        stmt = stmt.where(Ticket.status == status_)
    if category:
        stmt = stmt.where(Ticket.category == category)
    if priority:
        stmt = stmt.where(Ticket.priority == priority)
    if q:
        # Escape LIKE wildcards so "%" or "_" are searched literally.
        like = f"%{escape_like(q)}%"
        stmt = stmt.outerjoin(Order, Order.id == Ticket.order_id).where(or_(
            *(col.ilike(like, escape=LIKE_ESCAPE) for col in (Ticket.subject, Ticket.reference,
                                                              Ticket.customer_email, Ticket.customer_name,
                                                              Order.order_number))))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    items = db.scalars(stmt.order_by(Ticket.status == "resolved", Ticket.priority, Ticket.created_at.desc())
                       .offset((page - 1) * size).limit(size)).all()
    return TicketPage(items=items, total=total, page=page, size=size)


@router.get("/{ticket_id}", response_model=TicketDetail)
def get_ticket(ticket: AccessibleTicket, db: DB):
    """Full case: order + items, photos, customer history, audit log and the latest AI suggestion."""
    return svc.staff_detail(db, ticket)


@router.post("/{ticket_id}/status", response_model=TicketDetail)
def update_status(ticket: AccessibleTicket, body: StatusUpdate, db: DB, user: Staff,
                  background: BackgroundTasks):
    t = svc.change_status(db, ticket, body.status, body.note, user)
    if body.note and body.note.strip():                 # a message to the customer was included
        _notify_customer(background, t, body.note)
    return svc.staff_detail(db, t)


@router.post("/{ticket_id}/messages", response_model=TicketDetail)
def reply_to_customer(ticket: AccessibleTicket, body: StaffReplyIn, db: DB, user: Staff,
                      background: BackgroundTasks):
    """Message the customer (visible in their 'My tickets'). `resolve=true` also resolves the ticket
    with this message as the resolution. An open ticket moves to in_progress on the first reply."""
    t = conversation.staff_reply(db, ticket, user, body.body, resolve=body.resolve)
    _notify_customer(background, t, body.body)
    return svc.staff_detail(db, t)


def _notify_customer(background: BackgroundTasks, t, text: str) -> None:
    if t.customer_email:
        background.add_task(notify.send, *notify.support_replied(t.customer_email, t.customer_name, t.reference,
                                                                 text, resolved=t.status == "resolved"))


@router.post("/{ticket_id}/comments", response_model=TicketDetail)
def add_comment(ticket: AccessibleTicket, body: CommentIn, db: DB, user: Staff):
    """INTERNAL note, visible to staff only (never shown to the customer)."""
    ticket.events.append(TicketEvent(actor=user.email, action="note", detail=body.note))
    db.commit()
    db.refresh(ticket)
    return svc.staff_detail(db, ticket)


@router.post("/{ticket_id}/assist", response_model=AISuggestionOut)
def ai_assist(ticket: AccessibleTicket, db: DB, user: Staff, regenerate: bool = False):
    """AI-suggested resolution (advisory). Stored: repeated calls return it without a new LLM request
    unless `regenerate=true`. Money rules are enforced by guard-rails, not by the model."""
    if ticket.status == "resolved" and not ticket.suggestions:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ticket is already resolved")
    return assist_svc.get_or_create_suggestion(db, ticket, user, regenerate=regenerate)


@router.get("/{ticket_id}/attachments/{attachment_id}")
def get_attachment(ticket: AccessibleTicket, attachment_id: int, db: DB):
    att = db.get(Attachment, attachment_id)
    if not att or att.ticket_id != ticket.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    return FileResponse(attachment_path(att), media_type=att.content_type,
                        headers={"Content-Disposition": "inline", "X-Content-Type-Options": "nosniff",
                                 "Cache-Control": "private, no-store"})


@router.patch("/{ticket_id}", response_model=TicketDetail)
def override_classification(ticket: AccessibleTicket, body: TicketUpdate, db: DB, admin: Admin):
    """Admin correction of the AI classification (kept in the audit log)."""
    changes = body.model_dump(exclude_none=True)
    if changes:
        before = f"{ticket.category}/P{ticket.priority}"
        for k, v in changes.items():
            setattr(ticket, k, v)
        ticket.events.append(TicketEvent(actor=admin.email, action="corrected",
                                         detail=f"{before} -> {ticket.category}/P{ticket.priority}"))
        db.commit()
        db.refresh(ticket)
    return svc.staff_detail(db, ticket)


@router.post("/{ticket_id}/reassign", response_model=TicketDetail)
def reassign(ticket: AccessibleTicket, body: Reassign, db: DB, admin: Admin):
    return svc.staff_detail(db, svc.reassign(db, ticket, body.agent_id, admin))


@router.post("/{ticket_id}/retriage", response_model=TicketDetail)
def retriage(ticket: AccessibleTicket, db: DB, admin: Admin, reclassify: bool = False):
    """Re-route to the best agent. `reclassify=true` also asks the LLM again (costs one call)."""
    return svc.staff_detail(db, svc.retriage(db, ticket, admin, reclassify=reclassify))
