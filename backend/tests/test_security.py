"""Security behaviour: lockout, enumeration, headers, log hygiene, token checks, LLM economy."""
import logging

import jwt

from app.core.config import get_settings
from app.core.logging import redact
from app.core.security import login_guard
from tests.conftest import raise_ticket

API = "/api/v1"


def _login(client, email, password):
    return client.post(f"{API}/auth/login", data={"username": email, "password": password})


def test_account_locks_after_repeated_failures(client):
    login_guard._failures.clear()
    login_guard._locked_until.clear()
    for _ in range(5):
        assert _login(client, "kumar@test.dev", "wrong-password").status_code == 401
    r = _login(client, "kumar@test.dev", "AgentPass!1")          # correct password, but locked
    assert r.status_code == 429 and "Retry-After" in r.headers
    login_guard._locked_until.clear()
    assert _login(client, "kumar@test.dev", "AgentPass!1").status_code == 200


def test_unknown_and_wrong_password_look_identical(client):
    a = _login(client, "nobody@test.dev", "whatever-123")
    b = _login(client, "priya@test.dev", "whatever-123")
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_security_headers_and_request_id(client):
    r = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Request-ID"] == "abc-123"
    evil = client.get("/health", headers={"X-Request-ID": "x\nFAKE LOG LINE"})
    assert "\n" not in evil.headers["X-Request-ID"] and evil.headers["X-Request-ID"] != "x"


def test_forged_and_foreign_tokens_rejected(client):
    s = get_settings()
    forged = jwt.encode({"sub": "1", "role": "admin", "iss": s.jwt_issuer, "iat": 0, "exp": 9999999999},
                        "change-me-in-production-please-32+chars", algorithm="HS256")
    assert client.get(f"{API}/tickets", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    wrong_iss = jwt.encode({"sub": "1", "role": "admin", "iss": "other", "iat": 0, "exp": 9999999999},
                           s.jwt_secret, algorithm="HS256")
    assert client.get(f"{API}/tickets", headers={"Authorization": f"Bearer {wrong_iss}"}).status_code == 401


def test_jwt_secret_is_never_the_public_default():
    s = get_settings()
    assert len(s.jwt_secret) >= 32 and not s.jwt_secret.startswith("change-me")


def test_long_password_is_validated_not_500(client, admin_headers):
    r = client.post(f"{API}/users", headers=admin_headers,
                    json={"email": "long@test.dev", "name": "Long", "password": "x" * 100})
    assert r.status_code == 422


def test_secrets_are_redacted_from_logs():
    text = ('calling groq with api_key=gsk_ABCDEFGHIJKLMNOPQRSTUVWX and '
            'Authorization: Bearer eyJabc.def.ghi password="hunter2"')
    out = redact(text)
    assert "gsk_ABCDEF" not in out and "hunter2" not in out and "eyJabc" not in out


def test_reroute_makes_no_llm_call(client, admin_headers, customer_headers, monkeypatch):
    from app.services import llm as llm_module

    calls = []

    class Spy:
        def classify(self, *a, **k):
            calls.append(1)
            raise AssertionError("LLM must not be called on re-route")

    raise_ticket(client, customer_headers())
    tid = client.get(f"{API}/tickets", headers=admin_headers).json()["items"][0]["id"]
    monkeypatch.setattr(llm_module, "get_gateway", lambda: Spy())
    r = client.post(f"{API}/tickets/{tid}/retriage", headers=admin_headers)
    assert r.status_code == 200 and r.json()["events"][-1]["action"] == "rerouted" and not calls


def test_audit_log_records_login_failure(client, caplog):
    with caplog.at_level(logging.WARNING, logger="audit"):
        _login(client, "deepak@test.dev", "nope-nope")
    assert any(r.getMessage() == "login failed" for r in caplog.records)
    login_guard._failures.clear()
