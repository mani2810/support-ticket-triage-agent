import pandas as pd
import streamlit as st

import api_client as api
from ui import error

st.title("📊 Support dashboard")
st.caption("FreshCart customer support - live overview")

try:
    s = api.stats()
except api.ApiError as e:
    error(e)
    st.stop()

open_n = s["by_status"].get("open", 0) + s["by_status"].get("in_progress", 0)
k1, k2, k3 = st.columns(3)
k1.metric("Customers", s.get("customers", 0))
k2.metric("Orders", s.get("orders", 0))
k3.metric("Active agents", sum(1 for a in s["agents"] if a["is_active"]))
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total tickets", s["total"])
c2.metric("Open / in progress", open_n)
c3.metric("Urgent (P1)", s["by_priority"].get("P1", 0))
c4.metric("Classified by LLM", f"{s['llm_share']:.0%}")
c5.metric("Avg resolution", f"{s['avg_resolution_hours']} h" if s["avg_resolution_hours"] is not None else "—")
if s["unassigned"]:
    st.warning(f"{s['unassigned']} open ticket(s) are unassigned. Add or activate agents on the Team page.")

st.subheader("Team workload")
agents = pd.DataFrame(s["agents"])
if agents.empty:
    st.info("No agents yet. Create them on the Team page.")
else:
    left, right = st.columns([3, 2])
    chart = agents[agents["is_active"]].set_index("name")[["open_tickets", "high_priority_open"]]
    left.bar_chart(chart.rename(columns={"open_tickets": "Open", "high_priority_open": "High priority"}),
                   stack=False, height=280)
    right.dataframe(
        agents[["name", "open_tickets", "high_priority_open", "resolved_total", "is_active"]].rename(columns={
            "name": "Agent", "open_tickets": "Open", "high_priority_open": "P1 open",
            "resolved_total": "Resolved", "is_active": "Active"}),
        hide_index=True, width="stretch")

c1, c2 = st.columns(2)
with c1:
    st.subheader("By category")
    if s["by_category"]:
        st.bar_chart(pd.Series(s["by_category"], name="Tickets"), horizontal=True, height=260)
with c2:
    st.subheader("Last 14 days")
    st.line_chart(pd.DataFrame(s["daily_created"]).set_index("date")["count"].rename("Created"), height=260)

st.subheader("🤖 LLM gateway (since last restart)")
llm = s.get("llm", {})
if llm.get("mode") != "groq":
    st.info("Running on the keyword-rules classifier (no GROQ_API_KEY set or LLM_MODE=rules).")
else:
    g1, g2, g3, g4, g5 = st.columns(5)
    state = {"closed": "🟢 healthy", "half_open": "🟡 probing", "open": "🔴 paused"}.get(llm["breaker"], llm["breaker"])
    g1.metric("Circuit", state)
    g2.metric("Tickets classified", llm["requests"])
    g3.metric("Groq calls made", llm["llm_calls"])
    g4.metric("Saved by cache", llm["cache_hits"])
    g5.metric("Rule fallbacks", llm["fallbacks"])
    st.caption(f"Model `{llm['model']}` · AI-assist requests: {llm.get('assists', 0)} · "
               f"retries: {llm['retries']}. One ticket = at most one classification request; identical input "
               "is answered from cache; when Groq fails repeatedly the circuit pauses calls and rules take over.")
