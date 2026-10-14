"""'AI assist' for agents: a suggested resolution for a ticket.

    case text (order + ticket + history) -> LLM via gateway (or rules) -> policy guard-rails -> stored

Design:
* On demand only (agent clicks the button) and stored in the DB: opening the ticket again
  costs 0 LLM calls. "Regenerate" is an explicit, separate action.
* Advisory only. Deterministic guard-rails enforce the money rules whatever the model says:
  refunds can't exceed what was paid, item names must exist in the order, large refunds
  need admin approval.
"""
import logging

from sqlalchemy.orm import Session

from ..db.models import AISuggestion, Ticket, TicketEvent, User
from ..schemas import ResolutionSuggestion
from . import llm as llm_module
from .context import assist_case, delivery_delay_minutes

log = logging.getLogger(__name__)
ADMIN_APPROVAL_ABOVE = 2000.0


# ---------------------------------------------------------------- rule-based fallback
def rules_suggestion(ticket: Ticket) -> ResolutionSuggestion:
    order = ticket.order
    items = _matched_items(ticket.affected_items or [], ticket)
    item_total = sum(i.line_total for i in order.items if i.name in items) if order else 0.0
    issue = ticket.issue_type or ""
    if issue in ("damaged_item", "quality_issue", "wrong_item", "missing_item") and items:
        action, amount = "refund_items", item_total
        cause = "Item(s) reported as " + issue.replace("_", " ") + " by the customer."
    elif issue == "late_delivery" and order:
        action, amount, cause = "refund_delivery_fee", order.delivery_fee, "Delivery arrived after the promised time."
    elif issue == "not_delivered" and order:
        action, amount, cause = "refund_full_order", order.total, "Order marked as not received by the customer."
    elif issue == "payment_issue":
        action, amount, cause = "escalate_to_admin", 0.0, "Possible duplicate charge; needs payment verification."
    else:
        action, amount, cause = "explain_policy", 0.0, "Not enough structured information for an automatic remedy."
    name = ticket.customer_name.split()[0] if ticket.customer_name else "there"
    reply = (f"Hi {name},\n\nThank you for reaching out about order {ticket.order_number or ''} and sorry for the "
             f"trouble. We've reviewed your request" +
             (f" and processed a refund of INR {amount:.2f}" if amount else " and our team is looking into it") +
             ". Please let us know if there is anything else we can do.\n\nCustomer Support")
    return ResolutionSuggestion(
        summary=f"{ticket.subject}: {ticket.body[:200]}", root_cause=cause, recommended_action=action,
        refund_amount=round(amount, 2), affected_items=items,
        internal_steps=["Verify the order details and photos", "Apply the recommended action",
                        "Send the reply and resolve the ticket"],
        customer_reply=reply, confidence="low")


# ---------------------------------------------------------------- guard-rails
def _matched_items(names: list[str], ticket: Ticket) -> list[str]:
    if not ticket.order:
        return []
    by_lower = {i.name.lower(): i.name for i in ticket.order.items}
    out = []
    for n in names:
        key = n.strip().lower()
        match = by_lower.get(key) or next((v for k, v in by_lower.items() if key and (key in k or k in key)), None)
        if match and match not in out:
            out.append(match)
    return out


def apply_guardrails(s: ResolutionSuggestion, ticket: Ticket) -> ResolutionSuggestion:
    notes = list(s.policy_notes)
    order = ticket.order
    items = _matched_items(s.affected_items, ticket)
    if len(items) < len(s.affected_items):
        notes.append("Removed item names that are not in this order.")
    amount = s.refund_amount
    if order is None:
        if amount:
            notes.append("No order linked: refund removed.")
        amount = 0.0
    else:
        if s.recommended_action == "refund_delivery_fee":
            if amount != order.delivery_fee:
                notes.append(f"Delivery-fee refund set to the actual fee INR {order.delivery_fee:.2f}.")
            amount = order.delivery_fee
            delay = delivery_delay_minutes(order)
            if delay is not None and delay <= 0:
                notes.append("Order records show an on-time delivery - verify before refunding the fee.")
        elif s.recommended_action == "refund_items" and items:
            item_total = round(sum(i.line_total for i in order.items if i.name in items), 2)
            if amount > item_total:
                notes.append(f"Refund capped to the affected items' total INR {item_total:.2f}.")
                amount = item_total
        if amount > order.total:
            notes.append(f"Refund capped to the order total INR {order.total:.2f}.")
            amount = order.total
    needs_approval = s.needs_admin_approval or amount > ADMIN_APPROVAL_ABOVE \
        or s.recommended_action == "escalate_to_admin"
    if amount > ADMIN_APPROVAL_ABOVE:
        notes.append(f"Refund above INR {ADMIN_APPROVAL_ABOVE:.0f} requires admin approval.")
    return s.model_copy(update={"affected_items": items, "refund_amount": round(max(amount, 0.0), 2),
                                "needs_admin_approval": needs_approval, "policy_notes": notes})


# ---------------------------------------------------------------- use-case
def get_or_create_suggestion(db: Session, ticket: Ticket, user: User, regenerate: bool = False) -> AISuggestion:
    if ticket.suggestions and not regenerate:
        return ticket.suggestions[-1]                       # stored: 0 LLM calls

    gateway = llm_module.get_gateway()
    generated_by, error = "rules", None
    suggestion = None
    if gateway is not None:
        res = gateway.suggest(assist_case(db, ticket), use_cache=not regenerate)
        if res.value is not None:
            suggestion, generated_by = res.value, "llm"
        else:
            error = res.error
    if suggestion is None:
        suggestion = rules_suggestion(ticket)
        if error:
            suggestion = suggestion.model_copy(update={"policy_notes": [f"{error}; rule-based draft shown."]})
    suggestion = apply_guardrails(suggestion, ticket)

    row = AISuggestion(requested_by=user.email, generated_by=generated_by, payload=suggestion.model_dump())
    ticket.suggestions.append(row)
    ticket.events.append(TicketEvent(actor=user.email, action="ai_assist",
                                     detail=f"{generated_by}: {suggestion.recommended_action}, "
                                            f"refund INR {suggestion.refund_amount:.2f}"))
    db.commit()
    db.refresh(row)
    log.info("ai assist generated", extra={"ticket_id": ticket.id, "by": generated_by,
                                           "action": suggestion.recommended_action})
    return row
