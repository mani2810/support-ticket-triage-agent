"""Pydantic request / response models (the API contract)."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

Category = Literal["billing", "refund", "damaged", "delivery", "technical", "account", "other"]
Status = Literal["open", "in_progress", "resolved"]
Role = Literal["admin", "agent", "customer"]
StaffRole = Literal["admin", "agent"]
IssueType = Literal["damaged_item", "missing_item", "wrong_item", "quality_issue", "late_delivery",
                    "not_delivered", "payment_issue", "refund_status", "other"]

ISSUE_LABELS: dict[str, str] = {
    "damaged_item": "Item arrived damaged", "missing_item": "Item missing from order",
    "wrong_item": "Received wrong item", "quality_issue": "Expired / spoiled / poor quality",
    "late_delivery": "Delivery was late", "not_delivered": "Order not delivered",
    "payment_issue": "Payment / charged twice", "refund_status": "Refund not received", "other": "Something else",
}


def _one_line(v: str, limit: int) -> str:
    v = " ".join(str(v).split())
    return v[: limit - 3] + "..." if len(v) > limit else v


# ---------------------------------------------------------------- LLM output
class TicketClassification(BaseModel):
    """Structured output the LLM must return when routing a ticket."""

    category: Category
    priority: int = Field(..., ge=1, le=3, description="1 = urgent, 2 = normal, 3 = low")
    reason: str = Field(..., description="One-sentence justification")

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, v: str) -> str:
        # Untrusted model output: single line, bounded length (it is shown in the admin UI).
        return _one_line(v, 300)


ResolutionAction = Literal["refund_items", "replace_items", "refund_delivery_fee", "refund_full_order",
                           "redeliver", "fix_account", "explain_policy", "escalate_to_admin"]


class ResolutionSuggestion(BaseModel):
    """Structured output of 'AI assist' for agents. Advisory only - a human decides."""

    summary: str = Field(..., description="2-3 sentence summary of the customer's problem")
    root_cause: str = Field(..., description="Most likely cause, based only on the provided data")
    recommended_action: ResolutionAction
    refund_amount: float = Field(0.0, ge=0, description="Suggested refund in INR (0 if none)")
    affected_items: list[str] = Field(default_factory=list, description="Item names from the order that are affected")
    internal_steps: list[str] = Field(default_factory=list, description="Checklist for the agent")
    customer_reply: str = Field(..., description="Polite reply to send to the customer")
    confidence: Literal["low", "medium", "high"] = "medium"
    needs_admin_approval: bool = False
    policy_notes: list[str] = Field(default_factory=list, description="Guard-rail notes added by the system")

    @field_validator("summary", "root_cause")
    @classmethod
    def _short(cls, v: str) -> str:
        return _one_line(v, 600)

    @field_validator("customer_reply")
    @classmethod
    def _reply(cls, v: str) -> str:
        v = v.strip()
        return v[:1500]

    @field_validator("internal_steps", "affected_items")
    @classmethod
    def _list(cls, v: list[str]) -> list[str]:
        return [_one_line(x, 200) for x in v[:8]]


# ---------------------------------------------------------------- auth & users
class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: EmailStr
    name: str
    role: Role
    skills: list[str] = []
    is_active: bool


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
    refresh_token: str | None = None


class RegisterIn(BaseModel):
    """Customer self-registration."""

    name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=72)
    phone: str | None = Field(None, pattern=r"^\+?[0-9 ]{8,15}$")
    address: str | None = Field(None, max_length=300)


class UserCreate(BaseModel):
    """Admin creates staff accounts (customers register themselves)."""

    email: EmailStr
    name: str = Field(..., min_length=2, max_length=100)
    role: StaffRole = "agent"
    skills: list[Category] = []
    password: str = Field(..., min_length=8, max_length=72)


class UserUpdate(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=100)
    skills: list[Category] | None = None
    is_active: bool | None = None
    password: str | None = Field(None, min_length=8, max_length=72)


class AgentWorkload(BaseModel):
    id: int
    name: str
    email: str
    skills: list[str]
    is_active: bool
    open_tickets: int
    high_priority_open: int
    resolved_total: int


# ---------------------------------------------------------------- orders
class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    name: str
    category: str
    quantity: int
    unit_price: float
    line_total: float


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_number: str
    status: str
    placed_at: datetime
    promised_by: datetime | None
    delivered_at: datetime | None
    delivery_fee: float
    total: float
    payment_method: str
    address: str
    items: list[OrderItemOut]
    open_ticket: str | None = None          # reference of an open ticket for this order, if any
    allowed_issues: list[str] = []          # issue types the customer may raise for this order now
    blocked_issues: dict[str, str] = {}     # issue type -> why it is not available (shown in the UI)


# ---------------------------------------------------------------- tickets
class TicketCreate(BaseModel):
    """Internal use-case input (the customer API builds it from the logged-in user + multipart form)."""

    customer_name: str = Field(..., min_length=2, max_length=100)
    customer_email: EmailStr
    subject: str = Field(..., min_length=3, max_length=200)
    body: str = Field(..., min_length=10, max_length=5000)
    customer_id: int | None = None
    order_id: int | None = None
    issue_type: IssueType | None = None
    affected_items: list[str] = []


class TicketEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    actor: str
    action: str
    detail: str
    created_at: datetime


class AssigneeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    original_name: str
    content_type: str
    size_bytes: int
    width: int
    height: int


class MessageOut(BaseModel):
    role: Literal["customer", "support"]
    author: str
    text: str
    created_at: datetime


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    reference: str
    customer_id: int | None
    customer_name: str
    customer_email: str
    order_number: str | None = None
    issue_type: str | None
    affected_items: list[str] = []
    subject: str
    body: str
    category: Category
    priority: int
    status: Status
    classified_by: str
    ai_reason: str
    assignment_reason: str
    resolution_note: str | None
    assignee: AssigneeOut | None
    attachment_count: int = 0
    awaiting: Literal["support", "customer"] | None = None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None


class AISuggestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    requested_by: str
    generated_by: str
    payload: ResolutionSuggestion
    created_at: datetime
    stale: bool = False             # new messages/notes arrived after this suggestion -> regenerate


class CustomerHistory(BaseModel):
    orders_total: int
    tickets_total: int
    refunds_resolved: int
    member_since: datetime | None


class TicketDetail(TicketOut):
    events: list[TicketEventOut] = []
    messages: list[MessageOut] = []
    attachments: list[AttachmentOut] = []
    order: OrderOut | None = None
    suggestion: AISuggestionOut | None = None
    customer_history: CustomerHistory | None = None


class TicketPage(BaseModel):
    items: list[TicketOut]
    total: int
    page: int
    size: int


class MessageIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=2000)


class StaffReplyIn(MessageIn):
    resolve: bool = False          # send the reply AND mark the ticket resolved (reply = resolution note)


class CustomerTicketView(BaseModel):
    """What a customer may see: no internal reasoning, no staff emails, no internal notes."""

    reference: str
    subject: str
    body: str
    status: Status
    priority: int
    category: Category
    issue_type: str | None
    order_number: str | None
    affected_items: list[str]
    assigned_agent: str | None
    created_at: datetime
    updated_at: datetime
    resolution_note: str | None
    attachments: list[AttachmentOut]
    timeline: list[TicketEventOut]
    messages: list[MessageOut] = []
    awaiting: Literal["support", "customer"] | None = None


class StatusUpdate(BaseModel):
    status: Status
    note: str | None = Field(None, max_length=2000)


class CommentIn(BaseModel):
    note: str = Field(..., min_length=1, max_length=2000)


class Reassign(BaseModel):
    agent_id: int


class TicketUpdate(BaseModel):
    category: Category | None = None
    priority: int | None = Field(None, ge=1, le=3)


# ---------------------------------------------------------------- dashboard
class Stats(BaseModel):
    total: int
    by_status: dict[str, int]
    by_category: dict[str, int]
    by_priority: dict[str, int]
    unassigned: int
    llm_share: float
    avg_resolution_hours: float | None
    agents: list[AgentWorkload]
    daily_created: list[dict]
    customers: int = 0
    orders: int = 0
    llm: dict = {}                   # gateway health: mode, breaker, calls, cache hits, fallbacks
