"""Ticket list + full case view. Admins see all tickets; agents see only their own queue (enforced by the API)."""
import hashlib

import pandas as pd
import streamlit as st

import api_client as api
from ui import ACTIONS, CATEGORIES, ISSUES, PRIORITY, STATUS, chat, error, inr, order_items_table, safe, when

user = api.current_user()
is_admin = user["role"] == "admin"
st.title("🎫 All tickets" if is_admin else "📥 My queue")

# ---------------------------------------------------------------- filters
f1, f2, f3, f4 = st.columns([2, 1, 1, 1])
q = f1.text_input("Search", placeholder="ticket ID, order ID, subject, customer…")
status = f2.selectbox("Status", ["", "open", "in_progress", "resolved"],
                      format_func=lambda s: STATUS.get(s, "Any"), index=0)
category = f3.selectbox("Category", [""] + CATEGORIES, format_func=lambda c: c.title() or "Any")
priority = f4.selectbox("Priority", [None, 1, 2, 3], format_func=lambda p: PRIORITY.get(p, "Any"))

agents = []
assignee_id, unassigned = None, False
if is_admin:
    try:
        agents = [u for u in api.users() if u["role"] == "agent"]
    except api.ApiError as e:
        error(e)
    a1, _ = st.columns([2, 3])
    preset = st.session_state.pop("filter_agent", None)
    names = ["Anyone", "Unassigned"] + [a["name"] for a in agents]
    pick = a1.selectbox("Assignee", names, index=names.index(preset) if preset in names else 0)
    unassigned = pick == "Unassigned"
    assignee_id = next((a["id"] for a in agents if a["name"] == pick), None)

try:
    page = api.list_tickets(q=q, status=status, category=category, priority=priority,
                            assignee_id=assignee_id, unassigned=unassigned, size=100)
except api.ApiError as e:
    error(e)
    st.stop()


if not page["items"]:
    st.info("No tickets match these filters.")
else:
    df = pd.DataFrame([{
        "id": t["id"], "💬": "⏳ reply" if t["awaiting"] == "support" else "",
        "Ticket": t["reference"], "Order": t["order_number"] or "—",
        "Priority": PRIORITY[t["priority"]], "Status": STATUS[t["status"]],
        "Issue": ISSUES.get(t["issue_type"] or "", t["category"].title()), "Customer": t["customer_name"],
        "Agent": t["assignee"]["name"] if t["assignee"] else "—", "📷": t["attachment_count"] or "",
        "AI": "🤖" if t["classified_by"] == "llm" else "📏", "Updated": when(t["updated_at"]),
    } for t in page["items"]])
    st.caption(f"{page['total']} ticket(s). Click a row to open the case.")
    # The widget key changes whenever the list order changes, so a stale row index can never point at a
    # different ticket after a save re-sorts the list. The open ticket is tracked by ID, not by row.
    order_sig = hashlib.md5(",".join(map(str, df["id"])).encode()).hexdigest()[:10]
    sel = st.dataframe(df.drop(columns="id"), hide_index=True, width="stretch",
                       on_select="rerun", selection_mode="single-row",
                       key=f"ticket_table_{order_sig}_{st.session_state.get('table_nonce', 0)}")
    if sel.selection.rows:
        st.session_state.open_ticket_id = int(df.iloc[sel.selection.rows[0]]["id"])

tid = st.session_state.get("open_ticket_id")
if not tid:
    st.stop()

# ---------------------------------------------------------------- case view
try:
    t = api.get_ticket(tid)
except api.ApiError as e:
    st.session_state.pop("open_ticket_id", None)
    error(e)
    st.stop()

st.divider()
h1, h2 = st.columns([6, 1])
h1.subheader(f"{t['reference']} · {safe(t['subject'])}")
if h2.button("✖ Close case", width="stretch"):
    st.session_state.pop("open_ticket_id", None)
    # New table key = fresh widget with no selected row (otherwise the old selection re-opens the case).
    st.session_state["table_nonce"] = st.session_state.get("table_nonce", 0) + 1
    st.rerun()
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Status", STATUS[t["status"]])
m2.metric("Priority", PRIORITY[t["priority"]])
m3.metric("Category", t["category"].title())
m4.metric("Order", t["order_number"] or "—")
m5.metric("Agent", t["assignee"]["name"] if t["assignee"] else "Unassigned")

# The reply box gets a new widget key after every send (versioned), so it is guaranteed to come back empty;
# an AI-assist draft is placed into the NEXT version before that widget is drawn.
ver_key = f"compose_ver_{tid}"
reply_key = f"compose_{tid}_{st.session_state.get(ver_key, 0)}"
if f"prefill_{tid}" in st.session_state:
    st.session_state[reply_key] = st.session_state.pop(f"prefill_{tid}")

conv_tab, case_tab, ai_tab, act_tab, log_tab = st.tabs(
    ["💬 Conversation", "📋 Case", "🤖 AI assist", "⚙️ Status & notes", "🕘 Activity"])

with conv_tab:
    @st.fragment(run_every=15)
    def live_thread():
        try:
            fresh = api.get_ticket(tid)
        except api.ApiError as e:
            error(e)
            return
        if fresh["awaiting"] == "support":
            st.warning("💬 The customer is waiting for a reply.")
        chat(fresh["messages"])
        st.caption(f"Live · refreshes every 15 s · last checked {when(pd.Timestamp.now(tz='UTC').isoformat())}")

    live_thread()
    with st.form(f"reply_form_{tid}", clear_on_submit=False):
        body = st.text_area("Reply to the customer (they will see this in My tickets)", key=reply_key,
                            height=140, placeholder="Write a reply… or use 🤖 AI assist to draft one")
        c1, c2 = st.columns(2)
        send = c1.form_submit_button("📨 Send reply", type="primary", width="stretch")
        send_resolve = c2.form_submit_button("✅ Send & resolve", width="stretch",
                                             disabled=t["status"] == "resolved")
    if send or send_resolve:
        if not body.strip():
            st.error("Write a message first.")
        else:
            try:
                api.reply_to_customer(tid, body, resolve=bool(send_resolve))
                st.session_state[ver_key] = st.session_state.get(ver_key, 0) + 1
                st.toast("Reply sent to the customer" + (" and ticket resolved" if send_resolve else ""))
                st.rerun()
            except api.ApiError as e:
                error(e)

with case_tab:
    left, right = st.columns([3, 2])
    with left:
        hist = t.get("customer_history") or {}
        st.markdown(f"**Customer:** {safe(t['customer_name'])} · {safe(t['customer_email'])}  \n"
                    f"{hist.get('orders_total', 0)} orders · {hist.get('tickets_total', 0)} tickets · "
                    f"{hist.get('refunds_resolved', 0)} past refund/damage claims")
        st.markdown(f"**Issue:** {ISSUES.get(t['issue_type'] or '', '—')}"
                    + (f" · **Items:** {safe(', '.join(t['affected_items']))}" if t["affected_items"] else ""))
        if t["attachments"]:
            st.markdown("**Photos from the customer**")
            cols = st.columns(min(3, len(t["attachments"])))
            for col, a in zip(cols, t["attachments"]):
                try:
                    col.image(api.attachment(tid, a["id"]), caption=f"{safe(a['original_name'])} · "
                                                                   f"{a['width']}×{a['height']}")
                except api.ApiError as e:
                    col.caption(f"Image unavailable: {safe(e)}")
        st.info(f"**🤖 Why this classification** ({t['classified_by']}): {safe(t['ai_reason'])}")
        st.info(f"**🧭 Why this agent:** {safe(t['assignment_reason'])}")
    with right:
        if t["order"]:
            o = t["order"]
            st.markdown(f"**Order {o['order_number']}** · {o['status'].replace('_', ' ')}")
            st.caption(f"Placed {when(o['placed_at'])} · promised {when(o['promised_by'])} · "
                       f"delivered {when(o['delivered_at'])}")
            order_items_table(o)
        else:
            st.caption("No order linked to this ticket.")

with ai_tab:
    st.caption("AI suggests a resolution from the order, the conversation and the customer's history, applying "
               "the FreshCart refund policy. It is advisory: you decide. Money rules are enforced by the system "
               "(refunds can't exceed what was paid; above ₹2000 needs admin approval).")
    s = t.get("suggestion")
    b1, b2 = st.columns(2)
    if b1.button("🤖 Get AI suggestion" if not s else "✅ Suggestion ready", type="primary",
                 width="stretch", disabled=bool(s), key=f"ai_{tid}"):
        try:
            with st.spinner("Analysing the case…"):
                api.assist(tid)
            st.rerun()
        except api.ApiError as e:
            error(e)
    if s and b2.button("🔄 Regenerate (new AI call)", width="stretch", key=f"regen_{tid}"):
        try:
            with st.spinner("Re-analysing…"):
                api.assist(tid, regenerate=True)
            st.rerun()
        except api.ApiError as e:
            error(e)
    if s:
        if s.get("stale"):
            st.warning("🕘 New messages arrived after this suggestion was generated - it may be outdated. "
                       "Press **Regenerate** to include them.")
        p = s["payload"]
        st.markdown(f"**Recommended:** {ACTIONS.get(p['recommended_action'], p['recommended_action'])}"
                    + (f" · **{inr(p['refund_amount'])}**" if p["refund_amount"] else "")
                    + f" · confidence **{p['confidence']}** · by {'LLM' if s['generated_by'] == 'llm' else 'rules'}")
        if p["needs_admin_approval"]:
            st.warning("⬆️ Needs admin approval before the refund is issued.")
        st.markdown(f"**Summary:** {safe(p['summary'])}")
        st.markdown(f"**Likely root cause:** {safe(p['root_cause'])}")
        if p["affected_items"]:
            st.markdown("**Affected items:** " + safe(", ".join(p["affected_items"])))
        if p["internal_steps"]:
            st.markdown("**Steps:**\n" + "\n".join(f"{i}. {safe(x)}" for i, x in enumerate(p["internal_steps"], 1)))
        for n in p["policy_notes"]:
            st.caption(f"🛡️ {safe(n)}")
        draft = st.text_area("Draft reply (edit before using)", p["customer_reply"], height=170,
                             key=f"draft_{tid}")
        if st.button("📨 Use this draft as my reply", type="primary", key=f"use_{tid}"):
            st.session_state[f"prefill_{tid}"] = draft
            st.toast("Draft copied to the 💬 Conversation tab - review it and press Send")
            st.rerun()
        st.caption(f"Generated {when(s['created_at'])} for {safe(s['requested_by'])}")

with act_tab:
    options = ["open", "in_progress", "resolved"]
    with st.form(f"status_form_{tid}"):
        new_status = st.radio("Status", options, index=options.index(t["status"]), format_func=STATUS.get,
                              horizontal=True)
        msg = st.text_area("Message to the customer (optional; required when resolving)", height=110)
        if st.form_submit_button("Save status", type="primary"):
            try:
                api.set_status(tid, new_status, msg)
                st.toast("Saved" + (" - message sent to the customer" if msg.strip() else ""))
                st.rerun()
            except api.ApiError as e:
                error(e)
    with st.form(f"note_form_{tid}", clear_on_submit=True):
        note = st.text_area("🔒 Internal note (staff only - the customer never sees this)", height=90)
        if st.form_submit_button("Add internal note") and note.strip():
            try:
                api.add_note(tid, note)
                st.toast("Internal note added")
                st.rerun()
            except api.ApiError as e:
                error(e)
    if t["resolution_note"]:
        st.success(f"**Current resolution:** {safe(t['resolution_note'])}")

    if is_admin:
        st.divider()
        st.markdown("**Admin actions**")
        active = [a for a in agents if a["is_active"]]
        target = st.selectbox("Reassign to", active, format_func=lambda a: a["name"], key=f"re_{tid}")
        b1, b2, b3 = st.columns(3)
        if b1.button("Reassign", width="stretch", key=f"reb_{tid}", disabled=not active):
            try:
                api.reassign(tid, target["id"])
                st.rerun()
            except api.ApiError as e:
                error(e)
        if b2.button("🧭 Re-route (no AI call)", width="stretch", key=f"rt_{tid}"):
            try:
                api.retriage(tid)
                st.rerun()
            except api.ApiError as e:
                error(e)
        if b3.button("🤖 Re-classify with AI", width="stretch", key=f"rc_{tid}"):
            try:
                api.retriage(tid, reclassify=True)
                st.rerun()
            except api.ApiError as e:
                error(e)
        with st.popover("Correct AI classification", width="stretch"):
            cat = st.selectbox("Category", CATEGORIES, index=CATEGORIES.index(t["category"]), key=f"cc_{tid}")
            pr = st.selectbox("Priority", [1, 2, 3], index=t["priority"] - 1, format_func=PRIORITY.get,
                              key=f"cp_{tid}")
            if st.button("Save correction", key=f"cs_{tid}"):
                try:
                    api.correct(tid, category=cat, priority=pr)
                    st.rerun()
                except api.ApiError as e:
                    error(e)

with log_tab:
    for e in reversed(t["events"]):
        icon = {"note": "🔒", "reply": "📨", "customer_message": "🧑"}.get(e["action"], "•")
        st.markdown(f"- `{when(e['created_at'])}` {icon} **{e['action']}** by _{safe(e['actor'])}_: "
                    f"{safe(e['detail'])}")
