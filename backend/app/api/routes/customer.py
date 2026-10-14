"""Customer endpoints (/me). Every query is scoped to the logged-in customer's own data."""
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from ...core.config import get_settings
from ...core.ratelimit import limiter
from ...db.models import Attachment, Order, Ticket, TicketEvent
from ...schemas import ISSUE_LABELS, CustomerTicketView, IssueType, MessageIn, OrderOut, TicketCreate
from ...services import conversation, eligibility, notify
from ...services import tickets as svc
from ...services.attachments import attachment_path, prepare_images, store_images
from ..deps import DB, Customer

router = APIRouter(prefix="/me", tags=["customer"])


def _own_order(db, user, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if not order or order.customer_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    return order


def _own_ticket(db, user, reference: str) -> Ticket:
    t = db.scalar(select(Ticket).where(Ticket.reference == reference.strip().upper()))
    if not t or t.customer_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No ticket with this ID on your account")
    return t


def _open_ticket_refs(db, user) -> dict[int, str]:
    rows = db.execute(select(Ticket.order_id, Ticket.reference).where(
        Ticket.customer_id == user.id, Ticket.status != "resolved", Ticket.order_id.is_not(None))).all()
    return {oid: ref for oid, ref in rows}


@router.get("/orders", response_model=list[OrderOut])
def my_orders(db: DB, user: Customer):
    """Purchase history, newest first, with the reference of any open ticket per order."""
    open_refs = _open_ticket_refs(db, user)
    orders = db.scalars(select(Order).where(Order.customer_id == user.id).order_by(Order.placed_at.desc())).all()
    return [_order_out(o, open_refs) for o in orders]


def _order_out(o: Order, open_refs: dict[int, str]) -> OrderOut:
    item = OrderOut.model_validate(o)
    item.open_ticket = open_refs.get(o.id)
    rules = eligibility.allowed_issues(o)
    item.allowed_issues = [k for k, why in rules.items() if why is None]
    item.blocked_issues = {k: why for k, why in rules.items() if why}
    return item


@router.get("/orders/{order_id}", response_model=OrderOut)
def my_order(order_id: int, db: DB, user: Customer):
    return _order_out(_own_order(db, user, order_id), _open_ticket_refs(db, user))


@router.post("/tickets", response_model=CustomerTicketView, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
def raise_ticket(
    request: Request, db: DB, user: Customer, background: BackgroundTasks,
    order_id: Annotated[int, Form()],
    issue_type: Annotated[IssueType, Form()],
    description: Annotated[str, Form(min_length=10, max_length=5000)],
    subject: Annotated[str | None, Form(max_length=200)] = None,
    affected_items: Annotated[list[str] | None, Form()] = None,
    images: Annotated[list[UploadFile] | None, File(description="Up to 3 JPEG/PNG/WEBP photos")] = None,
):
    """Raise a ticket for one of *your* orders. Name, email and order are taken from your account,
    never from the form. Images are validated before any AI call is made."""
    order = _own_order(db, user, order_id)
    if ref := _open_ticket_refs(db, user).get(order.id):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"Ticket {ref} is already open for this order. Track it under My tickets.")
    if why := eligibility.check(order, issue_type):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"'{ISSUE_LABELS[issue_type]}' can't be raised for this order: {why}")
    names = {i.name for i in order.items}
    items = [i for i in dict.fromkeys(affected_items or []) if i in names]   # only items from this order
    prepared = prepare_images(images or [])
    data = TicketCreate(
        customer_name=user.name, customer_email=user.email, customer_id=user.id, order_id=order.id,
        issue_type=issue_type, affected_items=items,
        subject=(subject or "").strip() or f"{ISSUE_LABELS[issue_type]} - order {order.order_number}",
        body=description,
    )
    ticket = svc.create_ticket(db, data, order=order, images=prepared)
    background.add_task(notify.send, *notify.ticket_created(user.email, user.name, ticket.reference,
                                                            order.order_number))
    return svc.customer_view(db, ticket)


@router.get("/tickets", response_model=list[CustomerTicketView])
def my_tickets(db: DB, user: Customer):
    tickets = db.scalars(select(Ticket).where(Ticket.customer_id == user.id)
                         .order_by(Ticket.created_at.desc())).all()
    return [svc.customer_view(db, t) for t in tickets]


@router.get("/tickets/{reference}", response_model=CustomerTicketView)
def track_ticket(reference: str, db: DB, user: Customer):
    """Track by ticket ID (e.g. TKT-1A2B3C). Only tickets on your own account are visible."""
    return svc.customer_view(db, _own_ticket(db, user, reference))


@router.post("/tickets/{reference}/messages", response_model=CustomerTicketView)
@limiter.limit("20/minute")
def reply_to_support(request: Request, reference: str, body: MessageIn, db: DB, user: Customer,
                     background: BackgroundTasks):
    """Write to support on your ticket. Replying to a resolved ticket re-opens it."""
    t = conversation.customer_message(db, _own_ticket(db, user, reference), user, body.body)
    if t.assignee:
        background.add_task(notify.send, *notify.customer_replied(t.assignee.email, t.assignee.name, t.reference,
                                                                  user.name, body.body))
    return svc.customer_view(db, t)


@router.post("/tickets/{reference}/photos", response_model=CustomerTicketView)
@limiter.limit("10/minute")
def add_photos(request: Request, reference: str, db: DB, user: Customer,
               images: Annotated[list[UploadFile], File(description="Up to 3 JPEG/PNG/WEBP photos")]):
    """Add photos to an existing ticket (e.g. when support asks for a picture)."""
    t = _own_ticket(db, user, reference)
    prepared = prepare_images(images)
    if not prepared:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Attach at least one photo")
    limit = get_settings().max_images_total
    if len(t.attachments) + len(prepared) > limit:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"A ticket can have at most {limit} photos ({len(t.attachments)} already attached)")
    written = store_images(t, prepared)
    t.events.append(TicketEvent(actor="customer", action="attached", detail=f"{len(written)} photo(s)"))
    t.events.append(TicketEvent(actor="customer", action="customer_message",
                                detail=f"📎 Added {len(written)} photo(s)."))
    try:
        db.commit()
    except Exception:
        db.rollback()
        for path in written:
            path.unlink(missing_ok=True)
        raise
    db.refresh(t)
    return svc.customer_view(db, t)


@router.get("/tickets/{reference}/attachments/{attachment_id}")
def my_attachment(reference: str, attachment_id: int, db: DB, user: Customer):
    t = _own_ticket(db, user, reference)
    att = db.get(Attachment, attachment_id)
    if not att or att.ticket_id != t.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    return FileResponse(attachment_path(att), media_type=att.content_type,
                        headers={"Content-Disposition": "inline", "Cache-Control": "private, no-store"})
