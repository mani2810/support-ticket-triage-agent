"""Single-agent LangGraph workflow with sequential nodes:

    read_ticket -> classify -> prioritize -> check_availability -> choose_agent -> END

The graph is pure decision logic: it reads agent workload from the database but does
not write. `services.tickets` persists the decision, so the same graph is used for new
tickets and for admin "re-route" / "re-classify".

LLM economy: `classify` makes at most ONE gateway request per ticket (the gateway adds
caching, a circuit breaker and a call budget). Re-routing an existing ticket passes its
stored classification in `preset` and skips the LLM entirely.
"""
import logging
import re
from typing import Optional, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..db.models import Ticket, User
from ..schemas import TicketClassification
from . import llm as llm_module

log = logging.getLogger(__name__)
ACTIVE_STATUSES = ("open", "in_progress")


class TriageState(TypedDict, total=False):
    subject: str
    body: str
    text: str
    category: str
    priority: int
    reason: str
    classified_by: str
    candidates: list[dict]
    assigned_agent_id: Optional[int]
    assignment_reason: str
    error: Optional[str]
    preset: Optional[dict]          # existing classification -> skip the LLM
    use_cache: bool
    context: str                    # order summary given to the LLM (items, amounts, delivery)
    issue_type: Optional[str]       # chosen by the customer in the form
    order_total: float



# ---------------------------------------------------------------- rule-based fallback
KEYWORDS = {
    "refund": ["refund", "money back", "return", "reimburse"],
    "damaged": ["damaged", "broken", "cracked", "defective", "torn", "spoiled"],
    "billing": ["charged", "invoice", "bill", "payment", "overcharged", "double", "twice"],
    "delivery": ["delivery", "late", "not delivered", "tracking", "courier", "shipped"],
    "technical": ["error", "crash", "bug", "app", "login issue", "not working", "website"],
    "account": ["password", "account", "email change", "sign in", "locked"],
}
URGENT = ["urgent", "asap", "immediately", "fraud", "charged twice", "double charged", "damaged",
          "broken", "legal", "angry", "unacceptable", "refund"]
LOW = ["question", "feedback", "suggestion", "just wondering", "how do i"]


# The customer's own choice of issue type is a strong signal for the rule fallback.
ISSUE_TO_CATEGORY = {
    "damaged_item": ("damaged", 1), "quality_issue": ("damaged", 1), "missing_item": ("refund", 2),
    "wrong_item": ("refund", 2), "late_delivery": ("delivery", 2), "not_delivered": ("delivery", 1),
    "payment_issue": ("billing", 1), "refund_status": ("refund", 2),
}


def rule_classify(text: str, issue_type: str | None = None) -> TicketClassification:
    t = text.lower()
    if issue_type in ISSUE_TO_CATEGORY:
        category, priority = ISSUE_TO_CATEGORY[issue_type]
        if any(k in t for k in URGENT):
            priority = 1
        return TicketClassification(category=category, priority=priority,
                                    reason=f"Rules: customer selected '{issue_type.replace('_', ' ')}'.")
    scores = {cat: sum(t.count(k) for k in kws) for cat, kws in KEYWORDS.items()}
    category = max(scores, key=scores.get) if max(scores.values()) > 0 else "other"
    priority = 1 if any(k in t for k in URGENT) else 3 if any(k in t for k in LOW) else 2
    matched = {c: n for c, n in scores.items() if n}
    return TicketClassification(category=category, priority=priority,
                                reason=f"Keyword rules matched {matched or 'nothing'}.")


def agent_workloads(db: Session) -> list[dict]:
    """Active agents with their open-ticket and high-priority counts (one query)."""
    active = Ticket.status.in_(ACTIVE_STATUSES)
    rows = db.execute(
        select(
            User.id, User.name, User.skills,
            func.count(Ticket.id).filter(active).label("open_tickets"),
            func.coalesce(func.sum(case((active & (Ticket.priority == 1), 1), else_=0)), 0).label("high"),
        )
        .outerjoin(Ticket, Ticket.assigned_to_id == User.id)
        .where(User.role == "agent", User.is_active.is_(True))
        .group_by(User.id)
        .order_by(User.id)
    ).all()
    return [{"id": r.id, "name": r.name, "skills": r.skills or [], "open_tickets": int(r.open_tickets),
             "high_priority_open": int(r.high)} for r in rows]


# ---------------------------------------------------------------- nodes
def read_ticket(state: TriageState) -> TriageState:
    return {"text": f"{state['subject'].strip()}\n{state['body'].strip()}"}


def classify(state: TriageState) -> TriageState:
    if preset := state.get("preset"):
        return {**preset, "classified_by": preset.get("classified_by", "rules")}
    gateway = llm_module.get_gateway()
    error = None
    if gateway is not None:
        res = gateway.classify(state["subject"], state["body"], context=state.get("context", ""),
                               use_cache=state.get("use_cache", True))
        if res.classification is not None:
            c = res.classification
            return {"category": c.category, "priority": c.priority, "reason": c.reason, "classified_by": "llm"}
        error = res.error
    r = rule_classify(state["text"], state.get("issue_type"))
    out = {"category": r.category, "priority": r.priority, "reason": r.reason, "classified_by": "rules"}
    return {**out, "error": error} if error else out


# When the customer explicitly picked one of these, the category is not open to interpretation.
PINNED_CATEGORY = {"payment_issue": "billing", "not_delivered": "delivery", "late_delivery": "delivery"}


def prioritize(state: TriageState) -> TriageState:
    """Business rules applied AFTER the model; they must hold whatever the LLM says.

    1. pin the category for unambiguous customer-selected issues (e.g. payment issue -> billing)
    2. escalate to P1: order not delivered, possible double charge, high-value refund/damage claim
    """
    issue = state.get("issue_type")
    category, priority, reason = state["category"], state["priority"], state["reason"]
    pinned = PINNED_CATEGORY.get(issue or "")
    if pinned and category != pinned and not state.get("preset"):
        category = pinned
        reason += f" Category set to '{pinned}' from the customer's chosen issue."
    if priority > 1 and issue == "not_delivered":
        priority, reason = 1, reason + " Escalated to P1: order not delivered."
    elif priority > 1 and issue == "payment_issue" and "twice" in state["text"].lower():
        priority, reason = 1, reason + " Escalated to P1: possible double charge."
    elif priority > 1 and category in ("refund", "damaged") and (
            state.get("order_total", 0) >= 2000
            or re.search(r"(rs\.?|inr|₹|\$)\s?\d[\d,]{3,}", state["text"].lower())):
        priority, reason = 1, reason + " Escalated to P1: high-value claim."
    changed = {k: v for k, v in (("category", category), ("priority", priority), ("reason", reason))
               if v != state[k]}
    return changed


def check_availability(state: TriageState, config: RunnableConfig) -> TriageState:
    db: Session = config["configurable"]["db"]
    exclude = config["configurable"].get("exclude_agent_id")
    agents = [a for a in agent_workloads(db) if a["id"] != exclude]
    skilled = [a for a in agents if state["category"] in a["skills"]] or agents
    for a in skilled:
        # Every open ticket counts 1; a high-priority one counts 3 in total.
        a["score"] = a["open_tickets"] + 2 * a["high_priority_open"]
        # An urgent ticket should go to someone who is not already firefighting.
        if state["priority"] == 1 and a["high_priority_open"] > 0:
            a["score"] += 5
    skilled.sort(key=lambda a: (a["score"], a["open_tickets"], a["id"]))
    return {"candidates": skilled}


def choose_agent(state: TriageState) -> TriageState:
    if not state["candidates"]:
        return {"assigned_agent_id": None, "assignment_reason": "No active agents available; left unassigned."}
    best, others = state["candidates"][0], state["candidates"][1:]
    why = (f"{best['name']} has {best['open_tickets']} open ticket(s), {best['high_priority_open']} high priority, "
           f"and handles '{state['category']}' (load score {best['score']}).")
    if others:
        why += " Other candidates: " + ", ".join(f"{a['name']} (score {a['score']})" for a in others) + "."
    return {"assigned_agent_id": best["id"], "assignment_reason": why}


def build_graph():
    g = StateGraph(TriageState)
    for name, fn in [("read_ticket", read_ticket), ("classify", classify), ("prioritize", prioritize),
                     ("check_availability", check_availability), ("choose_agent", choose_agent)]:
        g.add_node(name, fn)
    g.set_entry_point("read_ticket")
    g.add_edge("read_ticket", "classify")
    g.add_edge("classify", "prioritize")
    g.add_edge("prioritize", "check_availability")
    g.add_edge("check_availability", "choose_agent")
    g.add_edge("choose_agent", END)
    return g.compile()


triage_graph = build_graph()


def run_triage(db: Session, subject: str, body: str, *, preset: dict | None = None,
               use_cache: bool = True, exclude_agent_id: int | None = None, context: str = "",
               issue_type: str | None = None, order_total: float = 0.0) -> TriageState:
    """Classify (unless `preset` is given) and pick an agent. Makes at most one LLM request."""
    state: TriageState = {"subject": subject, "body": body, "use_cache": use_cache, "context": context,
                          "issue_type": issue_type, "order_total": order_total}
    if preset:
        state["preset"] = preset
    return triage_graph.invoke(state, {"configurable": {"db": db, "exclude_agent_id": exclude_agent_id}})

