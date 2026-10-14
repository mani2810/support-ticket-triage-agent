import streamlit as st

import api_client as api
from ui import ISSUES, STATUS, chat, error, safe, when

st.title("🎫 My tickets")

try:
    tickets = api.my_tickets()
except api.ApiError as e:
    error(e)
    st.stop()

# ---------------------------------------------------------------- pick a ticket (list or track by ID)
with st.form("track"):
    c1, c2 = st.columns([3, 1])
    ref_in = c1.text_input("Track a ticket by ID", placeholder="TKT-1A2B3C")
    if c2.form_submit_button("Track", type="primary", width="stretch") and ref_in.strip():
        wanted = ref_in.strip().upper()
        if wanted in {t["reference"] for t in tickets}:
            st.session_state.open_ref = wanted
        else:
            st.error(f"No ticket {safe(wanted)} on your account. Check the ID in your confirmation.")

if tickets:
    def label(t: dict) -> str:
        badge = " · 💬 new reply" if t["awaiting"] == "customer" else ""
        return f"{STATUS[t['status']]} · {t['reference']} · {t['order_number'] or ''} · {t['subject']}{badge}"

    refs = [t["reference"] for t in tickets]
    current = st.session_state.get("open_ref")
    pick = st.selectbox("Your tickets", tickets, format_func=label,
                        index=refs.index(current) if current in refs else 0)
    if current not in refs or pick["reference"] != current:
        st.session_state.open_ref = pick["reference"]
else:
    st.info("No tickets yet. Go to **My orders** and press **Get help** on an order.")

ref = st.session_state.get("open_ref")
if not ref:
    st.stop()

# ---------------------------------------------------------------- live conversation
st.divider()


@st.fragment(run_every=10)
def live_ticket(reference: str) -> None:
    try:
        t = api.my_ticket(reference)
    except api.ApiError as e:
        error(e)
        return
    st.subheader(f"{t['reference']} · {safe(t['subject'])}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Status", STATUS[t["status"]])
    c2.metric("Order", t["order_number"] or "—")
    c3.metric("Issue", ISSUES.get(t["issue_type"] or "", "—"))
    c4.metric("Handled by", t["assigned_agent"] or "Support queue")
    if t["status"] == "resolved":
        st.success(f"✅ Resolved: {safe(t['resolution_note'])}")
    elif t["awaiting"] == "customer":
        st.info("💬 Support has replied - see below.")
    else:
        st.caption("We're on it. New replies appear here automatically.")
    if t["affected_items"]:
        st.caption("Items: " + safe(", ".join(t["affected_items"])))
    if t["attachments"]:
        cols = st.columns(min(3, len(t["attachments"])))
        for col, a in zip(cols, t["attachments"]):
            try:
                col.image(api.my_attachment(t["reference"], a["id"]), caption=safe(a["original_name"]), width=220)
            except api.ApiError as e:
                col.caption(f"Image unavailable: {safe(e)}")
    st.markdown("#### Conversation")
    chat(t["messages"])
    with st.expander("Status history"):
        for e in t["timeline"]:
            st.markdown(f"- `{when(e['created_at'])}` {safe(e['detail'])}")


live_ticket(ref)

with st.form(f"reply_{ref}", clear_on_submit=True):
    msg = st.text_area("Write to support", height=100,
                       placeholder="Add details, answer a question, or tell us if the issue is not fixed. "
                                   "Replying to a resolved ticket re-opens it.")
    photos = st.file_uploader("Add photos (optional, up to 3 - JPG/PNG/WEBP)", type=["jpg", "jpeg", "png", "webp"],
                              accept_multiple_files=True)
    if st.form_submit_button("📨 Send", type="primary"):
        if not msg.strip() and not photos:
            st.error("Write a message or attach a photo.")
        elif photos and len(photos) > 3:
            st.error("Please attach at most 3 photos at a time.")
        else:
            try:
                if photos:
                    api.add_photos(ref, photos)
                if msg.strip():
                    api.reply_to_support(ref, msg)
                st.toast("Sent to support")
                st.rerun()
            except api.ApiError as e:
                error(e)
