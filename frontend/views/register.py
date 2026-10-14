import streamlit as st

import api_client as api
from ui import error

st.title("📝 Create your FreshCart account")

with st.form("register"):
    c1, c2 = st.columns(2)
    name = c1.text_input("Full name")
    email = c2.text_input("Email")
    p1 = c1.text_input("Password (min 8 characters)", type="password")
    p2 = c2.text_input("Confirm password", type="password")
    phone = c1.text_input("Phone (optional)")
    address = c2.text_input("Delivery address (optional)")
    ok = st.form_submit_button("Create account", type="primary")

if ok:
    if p1 != p2:
        st.error("Passwords do not match.")
    else:
        try:
            api.register(name, email, p1, phone, address)
        except api.ApiError as e:
            error(e)
        else:
            st.rerun()
