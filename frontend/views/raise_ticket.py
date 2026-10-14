import streamlit as st

import api_client as api
from ui import ISSUES, PRIORITY, error, inr, order_items_table, when

user = api.current_user()
st.title("🆘 Get help with an order")

try:
    orders = api.my_orders()
except api.ApiError as e:
    error(e)
    st.stop()
if not orders:
    st.info("You have no orders yet.")
    st.stop()

ids = [o["id"] for o in orders]
preselect = st.session_state.get("help_order_id")
order = st.selectbox(
    "Which order?", orders, index=ids.index(preselect) if preselect in ids else 0,
    format_func=lambda o: f"{o['order_number']} · {when(o['placed_at'])} · {inr(o['total'])}"
                          + ("  (ticket open)" if o["open_ticket"] else ""))

if order["open_ticket"]:
    st.warning(f"Ticket **{order['open_ticket']}** is already open for this order - we're on it.")
    if st.button("Track that ticket"):
        st.session_state.open_ref = order["open_ticket"]
        st.switch_page("views/my_tickets.py")
    st.stop()

with st.expander("Order details", expanded=False):
    order_items_table(order)

# Auto-captured from the logged-in account and the selected order (the API also enforces this).
c1, c2 = st.columns(2)
c1.text_input("Email (from your account)", user["email"], disabled=True)
c2.text_input("Order ID", order["order_number"], disabled=True)

allowed = [k for k in ISSUES if k in order.get("allowed_issues", list(ISSUES))]
if not allowed:
    st.info("There's nothing that can be reported for this order right now.")
    st.stop()
blocked = order.get("blocked_issues", {})
if blocked:
    with st.expander("Why can't I pick some options?"):
        for k, why in blocked.items():
            st.caption(f"**{ISSUES.get(k, k)}:** {why}")

with st.form("ticket", clear_on_submit=False):
    issue = st.selectbox("What went wrong?", allowed, format_func=ISSUES.get)
    items = st.multiselect("Which items? (optional)", [i["name"] for i in order["items"]])
    description = st.text_area("Describe the problem", height=140,
                               placeholder="e.g. The honey jar was cracked and leaked onto the other items.")
    photos = st.file_uploader("Photos (optional, up to 3 - JPG/PNG/WEBP, 5 MB each)",
                              type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True)
    send = st.form_submit_button("Submit ticket", type="primary")

if send:
    if len(description.strip()) < 10:
        st.error("Please describe the problem in at least 10 characters.")
    elif photos and len(photos) > 3:
        st.error("Please attach at most 3 photos.")
    else:
        try:
            with st.spinner("Submitting and routing your ticket to the right specialist…"):
                t = api.raise_ticket(order["id"], issue, description, items, None, photos or [])
        except api.ApiError as e:
            error(e)
        else:
            st.session_state.pop("help_order_id", None)
            st.success(f"Ticket **{t['reference']}** created. Keep this ID to track your request.")
            m1, m2, m3 = st.columns(3)
            m1.metric("Ticket ID", t["reference"])
            m2.metric("Priority", PRIORITY[t["priority"]])
            m3.metric("Assigned to", t["assigned_agent"] or "Support queue")
            st.session_state.open_ref = t["reference"]
            st.page_link("views/my_tickets.py", label="Open the conversation for this ticket", icon="💬")
