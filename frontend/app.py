"""FreshCart Support web UI (Streamlit).

    cd frontend && streamlit run app.py      ->  http://localhost:8501

Pages depend on who is logged in:
    guest     log in / create an account
    customer  my orders -> get help (raise a ticket with photos) -> my tickets / track by ID
    agent     my queue (order, photos, customer history, AI assist)
    admin     dashboard, all tickets, team (agents and the tickets they handle)
"""
import streamlit as st

import api_client as api
import session_cookie
from ui import safe

st.set_page_config(page_title="FreshCart Support", page_icon="🛒", layout="wide")

api.restore_session()            # after F5: log back in from the session cookie (refresh token)
user = api.current_user()
role = user["role"] if user else None

if role is None:
    pages = {"Welcome": [st.Page("views/login.py", title="Log in", icon="🔐", default=True),
                         st.Page("views/register.py", title="Create account", icon="📝")]}
elif role == "customer":
    pages = {"My account": [
        st.Page("views/my_orders.py", title="My orders", icon="🛒", default=True),
        st.Page("views/raise_ticket.py", title="Get help", icon="🆘"),
        st.Page("views/my_tickets.py", title="My tickets", icon="🎫"),
    ]}
elif role == "admin":
    pages = {"Admin": [
        st.Page("views/dashboard.py", title="Dashboard", icon="📊", default=True),
        st.Page("views/tickets.py", title="All tickets", icon="🎫"),
        st.Page("views/team.py", title="Team", icon="👥"),
    ]}
else:
    pages = {"Agent": [st.Page("views/tickets.py", title="My queue", icon="📥", default=True)]}

with st.sidebar:
    st.markdown("### 🛒 FreshCart Support")
    st.caption("Groceries delivered. Problems solved.")
    if user:
        st.write(f"**{safe(user['name'])}** · {user['role']}")
        st.caption(user["email"])
        if st.button("Log out", width="stretch"):
            api.logout()                                   # revokes the session server-side
            pending = st.session_state.get("_cookie_write")
            st.session_state.clear()                       # drop all page state of this user
            st.session_state["_cookie_write"] = pending    # ...but still delete the cookie
            st.session_state["_restore_tried"] = True      # and don't log back in from it
            st.rerun()
    if role in ("admin", "agent"):
        try:
            r = api.ready()
            label = "LLM (Groq)" if r.get("llm") == "groq" else "keyword rules"
            circuit = " · ⚠️ LLM paused (circuit open)" if r.get("llm_circuit") == "open" else ""
            st.caption(f"API ✅ · AI: **{label}**{circuit}")
        except Exception:
            st.caption("API ❌ unreachable")

session_cookie.flush()            # write/clear the session cookie if login/logout/refresh happened
st.navigation(pages).run()
