import streamlit as st

import api_client as api
from ui import ORDER_STATUS, error, inr, order_items_table, when

st.title("🛒 My orders")
st.caption("Something wrong with an order? Open it and press **Get help** - we'll fill in the details for you.")

try:
    orders = api.my_orders()
except api.ApiError as e:
    error(e)
    st.stop()

if not orders:
    st.info("You have no orders yet.")
    st.stop()

for o in orders:
    title = (f"**{o['order_number']}** · {when(o['placed_at'])} · {ORDER_STATUS.get(o['status'], o['status'])} · "
             f"{len(o['items'])} items · {inr(o['total'])}")
    with st.expander(title, expanded=o is orders[0]):
        order_items_table(o)
        c1, c2 = st.columns([3, 2])
        if o["delivered_at"]:
            c1.caption(f"Delivered {when(o['delivered_at'])} (promised by {when(o['promised_by'])})")
        elif o["promised_by"]:
            c1.caption(f"Promised by {when(o['promised_by'])}")
        if o["open_ticket"]:
            c2.info(f"Ticket **{o['open_ticket']}** is open for this order.")
            if c2.button("Track it", key=f"track_{o['id']}"):
                st.session_state.open_ref = o["open_ticket"]
                st.switch_page("views/my_tickets.py")
        elif c2.button("🆘 Get help with this order", key=f"help_{o['id']}", type="primary",
                       width="stretch"):
            st.session_state.help_order_id = o["id"]
            st.switch_page("views/raise_ticket.py")
