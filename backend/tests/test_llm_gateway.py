"""LLM gateway: one call per ticket, cache, retry policy, circuit breaker, call budget."""
import groq
import httpx
from langchain_core.runnables import RunnableLambda

from app.schemas import TicketClassification
from app.services.llm import CallBudget, CircuitBreaker, LLMGateway, TTLCache

OK = TicketClassification(category="refund", priority=1, reason="refund requested")
REQ = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


def rate_limited(retry_after="1"):
    return groq.RateLimitError("429", response=httpx.Response(429, headers={"retry-after": retry_after}, request=REQ),
                               body=None)


def auth_error():
    return groq.AuthenticationError("401", response=httpx.Response(401, request=REQ), body=None)


class ScriptedLLM:
    """Raises/returns the scripted items in order and counts real calls."""

    def __init__(self, *script):
        self.script, self.calls = list(script), 0

    def with_structured_output(self, schema):
        def run(_):
            self.calls += 1
            item = self.script.pop(0) if self.script else OK
            if isinstance(item, Exception):
                raise item
            return item
        return RunnableLambda(run)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(llm, clock=None, **kw):
    clock = clock or Clock()
    return LLMGateway(llm=llm, model="m", sleep=lambda _: None,
                      breaker=kw.pop("breaker", CircuitBreaker(3, 60, clock)),
                      budget=kw.pop("budget", CallBudget(25, clock)),
                      cache=TTLCache(3600, clock=clock), **kw), clock


def test_success_is_one_call_and_cached():
    llm = ScriptedLLM(OK)
    gw, _ = make(llm)
    assert gw.classify("Refund", "money back please").outcome == "llm"
    again = gw.classify("  refund ", "Money back please")  # same text, different whitespace/case
    assert again.outcome == "cache" and again.classification == OK
    assert llm.calls == 1 and gw.stats["cache_hits"] == 1


def test_bypass_cache_when_asked():
    llm = ScriptedLLM(OK, OK)
    gw, _ = make(llm)
    gw.classify("a", "b")
    assert gw.classify("a", "b", use_cache=False).outcome == "llm" and llm.calls == 2


def test_transient_error_retried_once_then_succeeds():
    llm = ScriptedLLM(groq.APITimeoutError(request=REQ), OK)
    gw, _ = make(llm)
    res = gw.classify("a", "b")
    assert res.outcome == "llm" and res.attempts == 2 and llm.calls == 2 and gw.stats["retries"] == 1


def test_retries_are_bounded():
    llm = ScriptedLLM(groq.APITimeoutError(request=REQ), groq.APITimeoutError(request=REQ), OK)
    gw, _ = make(llm, max_attempts=2)
    res = gw.classify("a", "b")
    assert res.outcome == "failed" and llm.calls == 2


def test_long_retry_after_is_not_waited_for():
    llm = ScriptedLLM(rate_limited("30"), OK)
    gw, _ = make(llm)
    res = gw.classify("a", "b")
    assert res.outcome == "failed" and llm.calls == 1          # no blocking 30s sleep
    assert gw.breaker.state == "open"                         # honour Retry-After via the breaker


def test_auth_error_never_retried_and_opens_circuit():
    llm = ScriptedLLM(auth_error(), OK)
    gw, _ = make(llm)
    assert gw.classify("a", "b").outcome == "failed" and llm.calls == 1
    assert gw.classify("c", "d").outcome == "circuit_open" and llm.calls == 1


def test_invalid_output_falls_back_without_tripping_breaker():
    llm = ScriptedLLM(ValueError("bad json"), ValueError("bad json"), ValueError("bad json"))
    gw, _ = make(llm)
    for i in range(3):
        assert gw.classify(f"s{i}", "b").outcome == "failed"
    assert llm.calls == 3 and gw.breaker.state == "closed"    # model output issue, not an outage


def test_circuit_opens_after_consecutive_failures_and_recovers():
    err = groq.InternalServerError("500", response=httpx.Response(500, request=REQ), body=None)
    llm = ScriptedLLM(*([err] * 6), OK)
    gw, clock = make(llm, max_attempts=1)
    for i in range(3):
        gw.classify(f"s{i}", "b")
    assert gw.breaker.state == "open"
    calls = llm.calls
    assert gw.classify("x", "y").outcome == "circuit_open" and llm.calls == calls   # no call while open
    clock.t += 61                                                                   # cool-down passes
    llm.script = [OK]
    assert gw.classify("x", "y").outcome == "llm" and gw.breaker.state == "closed"  # probe succeeded


def test_half_open_failure_reopens():
    clock = Clock()
    breaker = CircuitBreaker(1, 60, clock)
    breaker.record_failure()
    clock.t += 61
    assert breaker.state == "half_open" and breaker.allow()
    assert not breaker.allow()                     # only one probe at a time
    breaker.record_failure()
    assert breaker.state == "open"


def test_budget_caps_calls_per_minute():
    llm = ScriptedLLM(*([OK] * 5))
    clock = Clock()
    gw, _ = make(llm, clock=clock, budget=CallBudget(2, clock))
    outcomes = [gw.classify(f"s{i}", "b").outcome for i in range(3)]
    assert outcomes == ["llm", "llm", "budget_exhausted"] and llm.calls == 2
    clock.t += 60
    assert gw.classify("s9", "b").outcome == "llm"


def test_reason_is_sanitised():
    c = TicketClassification(category="other", priority=3, reason="line1\n\n line2 " + "x" * 400)
    assert "\n" not in c.reason and len(c.reason) == 300
