import pandas as pd
import streamlit as st

import api_client as api
from ui import CATEGORIES, PRIORITY, STATUS, error, when

st.title("👥 Team")

try:
    stats = api.stats()
    staff = api.users()
except api.ApiError as e:
    error(e)
    st.stop()

agents_tab, manage_tab, customers_tab = st.tabs(["🧑‍💼 Agents at work", "⚙️ Manage staff", "🛒 Customers"])

with agents_tab:
    agents = stats["agents"]
    if not agents:
        st.info("No agents yet - add them in **Manage staff**.")
    else:
        st.dataframe(pd.DataFrame([{
            "Agent": a["name"], "Status": "🟢 active" if a["is_active"] else "⚪ inactive",
            "Skills": ", ".join(a["skills"]), "Open now": a["open_tickets"], "P1 open": a["high_priority_open"],
            "Resolved": a["resolved_total"]} for a in agents]), hide_index=True, width="stretch")

        st.subheader("Tickets handled by an agent")
        a = st.selectbox("Agent", agents, format_func=lambda x: x["name"])
        which = st.radio("Show", ["Open", "Resolved", "All"], horizontal=True)
        status = {"Open": None, "Resolved": "resolved", "All": None}[which]
        try:
            page = api.list_tickets(assignee_id=a["id"], status=status, size=100)
        except api.ApiError as e:
            error(e)
            st.stop()
        items = page["items"] if which != "Open" else [t for t in page["items"] if t["status"] != "resolved"]
        if items:
            st.dataframe(pd.DataFrame([{
                "Ticket": t["reference"], "Order": t["order_number"] or "—", "Priority": PRIORITY[t["priority"]],
                "Status": STATUS[t["status"]], "Customer": t["customer_name"], "Subject": t["subject"],
                "Created": when(t["created_at"]), "Resolved": when(t["resolved_at"])} for t in items]),
                hide_index=True, width="stretch")
            if st.button(f"Open {a['name']}'s tickets in All tickets"):
                st.session_state.filter_agent = a["name"]
                st.switch_page("views/tickets.py")
        else:
            st.caption("No tickets in this view.")

with manage_tab:
    st.dataframe(pd.DataFrame(staff)[["name", "email", "role", "skills", "is_active"]],
                 hide_index=True, width="stretch")
    c1, c2 = st.columns(2)
    with c1, st.form("new_user", clear_on_submit=True):
        st.subheader("Add staff member")
        name = st.text_input("Name")
        email = st.text_input("Email")
        role = st.selectbox("Role", ["agent", "admin"])
        skills = st.multiselect("Skills (categories this person handles)", CATEGORIES)
        password = st.text_input("Temporary password (min 8 chars)", type="password")
        if st.form_submit_button("Create", type="primary"):
            try:
                api.create_user({"name": name, "email": email, "role": role, "skills": skills, "password": password})
                st.toast(f"Created {name}")
                st.rerun()
            except api.ApiError as e:
                error(e)
    with c2:
        st.subheader("Edit staff member")
        u = st.selectbox("Person", staff, format_func=lambda x: f"{x['name']} ({x['role']})")
        if u:
            with st.form(f"edit_{u['id']}"):
                skills = st.multiselect("Skills", CATEGORIES, default=[s for s in u["skills"] if s in CATEGORIES])
                active = st.checkbox("Active (receives new tickets)", value=u["is_active"])
                pw = st.text_input("Reset password (optional)", type="password")
                if st.form_submit_button("Save"):
                    changes = {"skills": skills, "is_active": active}
                    if pw:
                        changes["password"] = pw
                    try:
                        api.update_user(u["id"], **changes)
                        st.toast("Saved")
                        st.rerun()
                    except api.ApiError as e:
                        error(e)

with customers_tab:
    try:
        customers = api.users(role="customer")
    except api.ApiError as e:
        error(e)
        st.stop()
    st.caption(f"{len(customers)} registered customers · {stats['orders']} orders")
    if customers:
        st.dataframe(pd.DataFrame(customers)[["name", "email", "is_active"]], hide_index=True,
                     width="stretch")
