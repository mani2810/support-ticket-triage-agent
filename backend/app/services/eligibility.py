"""Which problems can a customer report for an order? (single source of truth for API and UI)

    item problems (damaged / missing / wrong / quality)  -> delivered, within TICKET_WINDOW_DAYS of delivery
    late_delivery                                        -> delivered AFTER the promised time
    not_delivered                                        -> not delivered yet AND the promised time has passed
    payment_issue / refund_status / other                -> any order from the last 90 days
"""
from datetime import timedelta

from ..core.config import get_settings
from ..db.models import Order, utcnow

ITEM_ISSUES = ("damaged_item", "missing_item", "wrong_item", "quality_issue")
ANYTIME_ISSUES = ("payment_issue", "refund_status", "other")
ANYTIME_DAYS = 90


def allowed_issues(order: Order) -> dict[str, str | None]:
    """issue_type -> None if allowed, else a customer-friendly reason why not."""
    now = utcnow()
    window = get_settings().ticket_window_days
    delivered = order.status == "delivered" and order.delivered_at is not None
    out: dict[str, str | None] = {}

    for issue in ITEM_ISSUES:
        if not delivered:
            out[issue] = "This order hasn't been delivered yet."
        elif now - order.delivered_at > timedelta(days=window):
            out[issue] = f"Item problems must be reported within {window} days of delivery."
        else:
            out[issue] = None

    late = delivered and order.promised_by is not None and order.delivered_at > order.promised_by
    out["late_delivery"] = None if late else (
        "This order hasn't been delivered yet." if not delivered else "This order was delivered on time.")

    if delivered:
        out["not_delivered"] = "Our records show this order was delivered."
    elif order.status == "cancelled":
        out["not_delivered"] = "This order was cancelled."
    elif order.promised_by is not None and now < order.promised_by:
        out["not_delivered"] = "Your order is still within its promised delivery time."
    else:
        out["not_delivered"] = None

    too_old = now - order.placed_at > timedelta(days=ANYTIME_DAYS)
    for issue in ANYTIME_ISSUES:
        out[issue] = f"Orders older than {ANYTIME_DAYS} days can't be raised online." if too_old else None
    return out


def check(order: Order, issue_type: str) -> str | None:
    """None if the issue can be raised for this order, else the reason."""
    return allowed_issues(order).get(issue_type, "Unknown issue type.")
