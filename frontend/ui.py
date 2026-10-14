"""Small shared UI helpers."""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

PRIORITY = {1: "🔴 P1 Urgent", 2: "🟠 P2 Normal", 3: "🟢 P3 Low"}
STATUS = {"open": "🆕 Open", "in_progress": "🛠️ In progress", "resolved": "✅ Resolved"}
ORDER_STATUS = {"placed": "🕒 Placed", "out_for_delivery": "🛵 Out for delivery", "delivered": "📦 Delivered",
                "cancelled": "✖️ Cancelled"}
CATEGORIES = ["billing", "refund", "damaged", "delivery", "technical", "account", "other"]
ISSUES = {
    "damaged_item": "Item arrived damaged", "missing_item": "Item missing from order",
    "wrong_item": "Received wrong item", "quality_issue": "Expired / spoiled / poor quality",
    "late_delivery": "Delivery was late", "not_delivered": "Order not delivered",
    "payment_issue": "Payment / charged twice", "refund_status": "Refund not received", "other": "Something else",
}
ACTIONS = {
    "refund_items": "💸 Refund affected items", "replace_items": "🔁 Replace items",
    "refund_delivery_fee": "🛵 Refund delivery fee", "refund_full_order": "💰 Refund full order",
    "redeliver": "📦 Redeliver order", "fix_account": "🔧 Fix account", "explain_policy": "📘 Explain policy",
    "escalate_to_admin": "⬆️ Escalate to admin",
}

# Every character with a meaning in (Streamlit) markdown.
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$])")


def _viewer_tz():
    """The browser's time zone (reported by Streamlit); falls back to this machine's zone."""
    try:
        name = st.context.timezone
        if name:
            return ZoneInfo(name)
    except Exception:
        pass
    return datetime.now().astimezone().tzinfo


def when(ts: str | None) -> str:
    """API timestamps are UTC (with offset). Show them in the viewer's local time."""
    if not ts:
        return "—"
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:                          # older data without offset = UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_viewer_tz()).strftime("%d %b %Y, %H:%M")


def inr(x: float) -> str:
    return f"₹{x:,.2f}"


def error(exc: Exception) -> None:
    st.error(f"⚠️ {safe(exc)}")


def safe(text) -> str:
    """Escape markdown in untrusted text (customer input, LLM output) before st.markdown/info/etc.

    Prevents injected links, images (tracking pixels) and formatting in staff screens.
    """
    return _MD_SPECIAL.sub(r"\\\1", " ".join(str(text or "").split()))


def safe_multiline(text) -> str:
    """Like safe(), but keeps line breaks (chat messages, draft replies)."""
    lines = [_MD_SPECIAL.sub(r"\\\1", line) for line in str(text or "").splitlines()]
    return "  \n".join(lines)          # two trailing spaces = markdown line break


def order_items_table(order: dict) -> None:
    rows = [{"Item": i["name"], "Qty": i["quantity"], "Price": inr(i["unit_price"]), "Total": inr(i["line_total"])}
            for i in order["items"]]
    # Static table: always renders (also inside expanders) and order lists are short.
    st.table(pd.DataFrame(rows).set_index("Item"))
    st.caption(f"Delivery fee {inr(order['delivery_fee'])} · **Order total {inr(order['total'])}** · "
               f"paid by {order['payment_method'].upper()}")


def chat(messages: list[dict]) -> None:
    """Render a ticket conversation. role: customer | support."""
    for m in messages:
        is_customer = m["role"] == "customer"
        with st.chat_message("user" if is_customer else "assistant", avatar="🧑" if is_customer else "🛒"):
            st.caption(f"**{safe(m['author'])}** · {when(m['created_at'])}")
            st.markdown(safe_multiline(m["text"]))
