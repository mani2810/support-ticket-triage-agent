"""Triage graph: classification, LLM structured output, fallback and workload-based routing."""
from langchain_core.runnables import RunnableLambda

from app.db.models import Ticket, User
from app.schemas import TicketClassification
from app.services import llm as llm_module
from app.services.llm import LLMGateway
from app.services.triage import rule_classify, run_triage


class FakeLLM:
    """Stands in for ChatGroq: returns structured output without any network call."""

    def __init__(self, result=None, fail=False):
        self.result, self.fail = result, fail

    def with_structured_output(self, schema):
        def run(_):
            if self.fail:
                raise TimeoutError("Groq API timeout")
            return self.result
        return RunnableLambda(run)


def test_rule_classifier():
    assert rule_classify("please refund my money").category == "refund"
    assert rule_classify("please refund my money").priority == 1
    assert rule_classify("just wondering how do I download an invoice").priority == 3


def gateway(fake):
    return LLMGateway(llm=fake, model="fake", sleep=lambda _: None)


def test_llm_result_is_used(db, monkeypatch):
    gw = gateway(FakeLLM(TicketClassification(category="billing", priority=1, reason="double charge")))
    monkeypatch.setattr(llm_module, "get_gateway", lambda: gw)
    out = run_triage(db, "Charged twice", "two debits for one order")
    assert (out["classified_by"], out["category"], out["priority"]) == ("llm", "billing", 1)


def test_llm_failure_falls_back_to_rules(db, monkeypatch):
    monkeypatch.setattr(llm_module, "get_gateway", lambda: gateway(FakeLLM(fail=True)))
    out = run_triage(db, "Refund", "refund please")
    assert out["classified_by"] == "rules" and out["error"] == "LLM failed: TimeoutError"


def test_preset_skips_llm(db, monkeypatch):
    fake = FakeLLM(fail=True)
    gw = gateway(fake)
    monkeypatch.setattr(llm_module, "get_gateway", lambda: gw)
    out = run_triage(db, "x", "y", preset={"category": "refund", "priority": 2, "reason": "stored",
                                           "classified_by": "llm"})
    assert out["category"] == "refund" and gw.stats["requests"] == 0


def test_high_value_refund_escalated(db):
    out = run_triage(db, "Refund status", "where is the refund for my TV of Rs 45,000")
    assert out["priority"] == 1


def test_urgent_ticket_avoids_busy_agent(db):
    kumar = db.query(User).filter_by(name="Kumar").one()
    deepak = db.query(User).filter_by(name="Deepak").one()
    # Kumar: 3 normal tickets. Deepak: 2 tickets incl. one P1 (fewer tickets, but firefighting).
    for i, (agent, prio) in enumerate([(kumar, 2), (kumar, 2), (kumar, 3), (deepak, 1), (deepak, 2)]):
        db.add(Ticket(reference=f"SEED-{i}", customer_name="x",
                      customer_email="x@x.com", subject="s", body="b", category="refund", priority=prio,
                      classified_by="rules", assigned_to_id=agent.id))
    db.commit()
    out = run_triage(db, "Refund urgently", "refund my money now")
    # Kumar score 3; Deepak score 2 + 2*1 + 5 = 9
    assert out["assigned_agent_id"] == kumar.id


def test_skill_routing(db):
    out = run_triage(db, "App error", "the app shows an error on the cart page")
    assert out["category"] == "technical"
    assert db.get(User, out["assigned_agent_id"]).name == "Priya"


def test_no_agents_leaves_unassigned(db):
    db.query(User).filter_by(role="agent").update({"is_active": False})
    db.commit()
    out = run_triage(db, "Refund", "refund please")
    assert out["assigned_agent_id"] is None
