from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .session import Base
from .types import UTCDateTime

CATEGORIES = ("billing", "refund", "damaged", "delivery", "technical", "account", "other")
STATUSES = ("open", "in_progress", "resolved")
ROLES = ("admin", "agent", "customer")
STAFF_ROLES = ("admin", "agent")
ORDER_STATUSES = ("placed", "out_for_delivery", "delivered", "cancelled")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """Every account. role: 'admin' and 'agent' are staff; 'customer' shops and raises tickets."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(10), default="agent", index=True)
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    tickets: Mapped[list["Ticket"]] = relationship(back_populates="assignee", foreign_keys="Ticket.assigned_to_id")
    orders: Mapped[list["Order"]] = relationship(back_populates="customer", order_by="Order.placed_at.desc()")


class Order(Base):
    """A grocery order. Line items are snapshotted (name/price at purchase time)."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_number: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="placed")
    placed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    promised_by: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    delivery_fee: Mapped[float] = mapped_column(Float, default=0.0)
    total: Mapped[float] = mapped_column(Float, default=0.0)
    payment_method: Mapped[str] = mapped_column(String(20), default="upi")
    address: Mapped[str] = mapped_column(String(300), default="")

    customer: Mapped[User] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan",
                                                    order_by="OrderItem.id")
    tickets: Mapped[list["Ticket"]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(40), default="grocery")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[float] = mapped_column(Float)

    order: Mapped[Order] = relationship(back_populates="items")

    @property
    def line_total(self) -> float:
        return round(self.quantity * self.unit_price, 2)


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (Index("ix_tickets_assignee_status", "assigned_to_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True, index=True)
    customer_name: Mapped[str] = mapped_column(String(100))
    customer_email: Mapped[str] = mapped_column(String(255), index=True)
    issue_type: Mapped[str | None] = mapped_column(String(30), nullable=True)   # chosen by the customer
    affected_items: Mapped[list[str]] = mapped_column(JSON, default=list)
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)

    category: Mapped[str] = mapped_column(String(20), index=True)
    priority: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)

    classified_by: Mapped[str] = mapped_column(String(10))       # llm | rules
    ai_reason: Mapped[str] = mapped_column(Text, default="")
    assignment_reason: Mapped[str] = mapped_column(Text, default="")
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    assigned_to_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    assignee: Mapped[User | None] = relationship(back_populates="tickets", foreign_keys=[assigned_to_id])
    customer: Mapped[User | None] = relationship(foreign_keys=[customer_id])
    order: Mapped[Order | None] = relationship(back_populates="tickets")

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    events: Mapped[list["TicketEvent"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", order_by="TicketEvent.id")
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", order_by="Attachment.id")
    suggestions: Mapped[list["AISuggestion"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", order_by="AISuggestion.id")

    @property
    def order_number(self) -> str | None:
        return self.order.order_number if self.order else None

    @property
    def attachment_count(self) -> int:
        return len(self.attachments)

    @property
    def awaiting(self) -> str | None:
        """'support' when the customer spoke last (incl. the original report), 'customer' after a staff reply."""
        if self.status == "resolved":
            return None
        for e in reversed(self.events):
            if e.action == "customer_message":
                return "support"
            if e.action == "reply":
                return "customer"
        return "support"


class TicketEvent(Base):
    """Audit trail: every classification, assignment and status change."""

    __tablename__ = "ticket_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    actor: Mapped[str] = mapped_column(String(100))          # "system", "customer" or a staff email
    action: Mapped[str] = mapped_column(String(30))          # created, classified, assigned, status, reassigned...
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    ticket: Mapped[Ticket] = relationship(back_populates="events")


class Attachment(Base):
    """Customer-uploaded image. Stored outside the web root under a random name, metadata stripped."""

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    stored_name: Mapped[str] = mapped_column(String(80), unique=True)
    original_name: Mapped[str] = mapped_column(String(120))
    content_type: Mapped[str] = mapped_column(String(30))
    size_bytes: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    ticket: Mapped[Ticket] = relationship(back_populates="attachments")


class AISuggestion(Base):
    """LLM 'AI assist' output for an agent. Stored so it is generated once and is auditable."""

    __tablename__ = "ai_suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    requested_by: Mapped[str] = mapped_column(String(255))
    generated_by: Mapped[str] = mapped_column(String(10))      # llm | rules
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    ticket: Mapped[Ticket] = relationship(back_populates="suggestions")


class RefreshToken(Base):
    """Long-lived login session ("remember me" across page refreshes).

    Only a SHA-256 hash is stored. Tokens rotate on every use; presenting an already-rotated
    token revokes the whole family (stolen-token detection). Logout / deactivation revoke them.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    family: Mapped[str] = mapped_column(String(32), index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(12), nullable=True)  # rotated|logout|reuse|account
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
