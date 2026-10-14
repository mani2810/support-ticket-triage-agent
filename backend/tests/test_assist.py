"""AI assist for agents: stored once, guard-rails on money, LLM + rules paths."""
from langchain_core.runnables import RunnableLambda

from app.schemas import ResolutionSuggestion
from app.services import llm as llm_module
from app.services.llm import LLMGateway
from tests.conftest import API, login, raise_ticket


class AssistLLM:
    """Fake model: returns a fixed ResolutionSuggestion and counts calls."""

    def __init__(self, suggestion: ResolutionSuggestion):
        self.suggestion, self.calls = suggestion, 0

    def with_structured_output(self, schema):
        def run(_):
            self.calls += 1
            return self.suggestion if schema is ResolutionSuggestion else None
        return RunnableLambda(run)


def _ticket(client, customer_headers, agent_headers, **kw):
    """Raise a ticket, then act as the agent it was routed to."""
    raise_ticket(client, customer_headers(), **kw)
    admin = login(client, "admin@test.dev", "AdminPass!1")
    t = client.get(f"{API}/tickets", headers=admin).json()["items"][0]
    return agent_headers(t["assignee"]["email"]), t["id"]


def test_rules_assist_when_no_llm(client, customer_headers, agent_headers):
    h, tid = _ticket(client, customer_headers, agent_headers)
    s = client.post(f"{API}/tickets/{tid}/assist", headers=h).json()
    p = s["payload"]
    assert s["generated_by"] == "rules" and p["recommended_action"] == "refund_items"
    assert p["refund_amount"] == 320.0 and p["affected_items"] == ["Honey Jar"]
    assert "Customer Support" in p["customer_reply"]


def test_llm_assist_is_stored_and_not_called_twice(client, customer_headers, agent_headers, monkeypatch):
    fake = AssistLLM(ResolutionSuggestion(
        summary="Honey jar broke", root_cause="Glass jar packed without padding", recommended_action="refund_items",
        refund_amount=320, affected_items=["honey jar"], internal_steps=["Refund", "Flag packing"],
        customer_reply="Sorry! Refund issued.", confidence="high"))
    gw = LLMGateway(llm=fake, model="fake", sleep=lambda _: None)
    h, tid = _ticket(client, customer_headers, agent_headers)
    monkeypatch.setattr(llm_module, "get_gateway", lambda: gw)
    first = client.post(f"{API}/tickets/{tid}/assist", headers=h).json()
    assert first["generated_by"] == "llm" and first["payload"]["affected_items"] == ["Honey Jar"]  # normalised
    again = client.post(f"{API}/tickets/{tid}/assist", headers=h).json()
    assert again["id"] == first["id"] and fake.calls == 1                       # stored: 0 extra calls
    detail = client.get(f"{API}/tickets/{tid}", headers=h).json()
    assert detail["suggestion"]["id"] == first["id"]
    regen = client.post(f"{API}/tickets/{tid}/assist", params={"regenerate": "true"}, headers=h).json()
    assert regen["id"] != first["id"] and fake.calls == 2


def test_guardrails_cap_hallucinated_refunds(client, customer_headers, agent_headers, monkeypatch):
    greedy = AssistLLM(ResolutionSuggestion(
        summary="x", root_cause="y", recommended_action="refund_items", refund_amount=99999,
        affected_items=["Honey Jar", "Diamond Ring"], customer_reply="Here is a lot of money", confidence="high"))
    monkeypatch.setattr(llm_module, "get_gateway", lambda: LLMGateway(llm=greedy, model="g", sleep=lambda _: None))
    h, tid = _ticket(client, customer_headers, agent_headers)
    p = client.post(f"{API}/tickets/{tid}/assist", headers=h).json()["payload"]
    assert p["affected_items"] == ["Honey Jar"]                                 # invented item dropped
    assert p["refund_amount"] == 320.0                                          # capped to affected items
    assert any("capped" in n for n in p["policy_notes"])


def test_delivery_fee_refund_uses_real_fee_and_big_refunds_need_approval(client, customer_headers, agent_headers,
                                                                         monkeypatch):
    late = AssistLLM(ResolutionSuggestion(summary="late", root_cause="rider delay",
                                          recommended_action="refund_delivery_fee", refund_amount=500,
                                          customer_reply="Sorry for the delay."))
    monkeypatch.setattr(llm_module, "get_gateway", lambda: LLMGateway(llm=late, model="l", sleep=lambda _: None))
    h, tid = _ticket(client, customer_headers, agent_headers, issue="late_delivery", items=())
    p = client.post(f"{API}/tickets/{tid}/assist", headers=h).json()["payload"]
    assert p["refund_amount"] == 35.0 and p["needs_admin_approval"] is False

    big = AssistLLM(ResolutionSuggestion(summary="all spoiled", root_cause="cold chain",
                                         recommended_action="refund_full_order", refund_amount=3347,
                                         customer_reply="Full refund."))
    monkeypatch.setattr(llm_module, "get_gateway", lambda: LLMGateway(llm=big, model="b", sleep=lambda _: None))
    raise_ticket(client, customer_headers(), number="GR-100002", issue="quality_issue", items=("Olive Oil 1L",),
                 description="All three olive oil bottles were rancid and smelled bad.")
    admin = login(client, "admin@test.dev", "AdminPass!1")
    tid2 = next(t["id"] for t in client.get(f"{API}/tickets", headers=admin).json()["items"]
                if t["order_number"] == "GR-100002")
    p2 = client.post(f"{API}/tickets/{tid2}/assist", headers=admin).json()["payload"]
    assert p2["refund_amount"] == 3347.0 and p2["needs_admin_approval"] is True


def test_assist_context_contains_order_but_not_other_customers(client, customer_headers, agent_headers, db):
    from app.db.models import Ticket
    from app.services.context import assist_case

    raise_ticket(client, customer_headers())
    raise_ticket(client, customer_headers("bob@test.dev"), number="GR-200001", issue="missing_item",
                 items=("Bananas",), description="Bananas were missing from my bag today.")
    db.expire_all()
    ana_ticket = db.query(Ticket).filter(Ticket.customer_email == "ana@test.dev").one()
    case = assist_case(db, ana_ticket)
    assert "GR-100001" in case and "Honey Jar" in case and "LATE by 95 min" in case
    assert "bob@test.dev" not in case and "Bananas" not in case and "GR-200001" not in case
