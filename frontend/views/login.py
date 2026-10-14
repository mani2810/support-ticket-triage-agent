import os

import streamlit as st

import api_client as api
from ui import error

st.title("🛒 FreshCart Support")
st.write("Log in to see your orders and get help, or to work the support queue.")

left, right = st.columns([3, 2])
with left, st.form("login"):
    email = st.text_input("Email")
    password = st.text_input("Password", type="password")
    ok = st.form_submit_button("Log in", type="primary")

if ok:
    try:
        api.login(email, password)
    except api.ApiError as e:
        error(e)
    else:
        st.rerun()

with right:
    st.info("New customer? Use **Create account** in the sidebar.")
    if os.getenv("SHOW_DEMO_ACCOUNTS", "true").lower() == "true":
        with st.expander("Demo accounts (local seed data)", expanded=True):
            st.markdown(
                "- **Customers:** `ananya@example.com`, `rahul@example.com`, `meera@example.com`  \n"
                "  password `Customer!2026`\n"
                "- **Agents:** `kumar@`, `deepak@`, `priya@`, `arjun@triagedesk.dev`  \n"
                "  password `Agent!2026`\n"
                "- **Admin:** `admin@triagedesk.dev` / `ChangeMe!2026`")
