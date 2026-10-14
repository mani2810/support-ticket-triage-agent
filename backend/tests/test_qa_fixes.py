"""Regression tests for every finding in docs/QA_REPORT.md (H1-H2, M1-M4 backend parts, L1-L7)."""
import os
from datetime import timedelta
from pathlib import Path

from app.core.ratelimit import client_ip, limiter, user_or_ip
from app.core.security import login_guard
from app.db.models import Order, utcnow
from tests.conftest import API, login, png_bytes, raise_ticket


# ---------------------------------------------------------------- H1: rate-limit keys
class _Req:
    def __init__(self, peer, headers):
        self.client = type("C", (), {"host": peer})()
        self.headers = headers


def test_h1_authenticated_requests_are_limited_per_user(client, customer_headers):
    ana, bob = customer_headers(), customer_headers("bob@test.dev")
    k1 = user_or_ip(_Req("127.0.0.1", {"authorization": ana["Authorization"]}))
    k2 = user_or_ip(_Req("127.0.0.1", {"authorization": bob["Authorization"]}))
    assert k1.startswith("user:") and k2.startswith("user:") and k1 != k2      # same server IP, separate buckets


def test_h1_forwarded_ip_only_trusted_from_frontend():
    assert client_ip(_Req("127.0.0.1", {"x-forwarded-for": "203.0.113.7"})) == "203.0.113.7"     # trusted proxy
    assert client_ip(_Req("198.51.100.9", {"x-forwarded-for": "203.0.113.7"})) == "198.51.100.9"  # spoof ignored


def test_h1_one_users_burst_does_not_block_another(client, customer_headers):
    limiter.enabled = True
    try:
        ana, bob = customer_headers(), customer_headers("bob@test.dev")
        codes = [client.post(f"{API}/me/tickets/TKT-NONE00/messages", json={"body": "x"}, headers=ana).status_code
                 for _ in range(21)]
        assert 429 in codes                                                        # Ana hit HER limit
        r = client.post(f"{API}/me/tickets/TKT-NONE00/messages", json={"body": "x"}, headers=bob)
        assert r.status_code == 404                                                # Bob unaffected (404 = not his)
    finally:
        limiter.enabled = False
        limiter.reset()


# ---------------------------------------------------------------- H2: deactivation re-routes tickets
def test_h2_deactivating_agent_reroutes_open_tickets(client, customer_headers, admin_headers, db):
    raise_ticket(client, customer_headers())                                       # damaged -> Kumar
    kumar = next(u for u in client.get(f"{API}/users", headers=admin_headers).json() if u["name"] == "Kumar")
    client.patch(f"{API}/users/{kumar['id']}", json={"is_active": False}, headers=admin_headers)
    t = client.get(f"{API}/tickets", headers=admin_headers).json()["items"][0]
    assert t["assignee"]["name"] != "Kumar"
    detail = client.get(f"{API}/tickets/{t['id']}", headers=admin_headers).json()
    assert detail["events"][-1]["action"] == "rerouted" and "deactivated" in detail["events"][-1]["detail"]


# ---------------------------------------------------------------- M1: lockout per (email, IP)
def test_m1_attacker_elsewhere_cannot_lock_out_owner(client, monkeypatch):
    from app.core.config import get_settings
    # The TestClient peer is "testclient": treat it as the trusted frontend so X-Forwarded-For is honoured.
    monkeypatch.setattr(get_settings(), "trusted_proxies", ["testclient"])
    for _ in range(5):
        client.post(f"{API}/auth/login", data={"username": "ana@test.dev", "password": "attacker-guess"},
                    headers={"X-Forwarded-For": "203.0.113.66"})
    r = client.post(f"{API}/auth/login", data={"username": "ana@test.dev", "password": "CustPass!1"},
                    headers={"X-Forwarded-For": "198.51.100.10"})
    assert r.status_code == 200                                                    # owner, other IP: fine
    r = client.post(f"{API}/auth/login", data={"username": "ana@test.dev", "password": "CustPass!1"},
                    headers={"X-Forwarded-For": "203.0.113.66"})
    assert r.status_code == 429                                                    # attacker's IP stays locked
    login_guard._locked_until.clear()


# ---------------------------------------------------------------- M2: refresh-token sessions
def _refresh_token(client, email, pw="CustPass!1"):
    return client.post(f"{API}/auth/login", data={"username": email, "password": pw}).json()["refresh_token"]


def test_m2_refresh_rotation_and_reuse_detection(client, monkeypatch):
    r = client.post(f"{API}/auth/login", data={"username": "ana@test.dev", "password": "CustPass!1"}).json()
    rt1 = r["refresh_token"]
    r2 = client.post(f"{API}/auth/refresh", json={"refresh_token": rt1})
    assert r2.status_code == 200 and r2.json()["user"]["email"] == "ana@test.dev"
    rt2 = r2.json()["refresh_token"]
    assert rt2 != rt1
    # within the grace window (two tabs / double F5) a just-rotated token still works
    assert client.post(f"{API}/auth/refresh", json={"refresh_token": rt1}).status_code == 200
    # after the grace window, replaying it = theft signal -> whole family revoked (rt2 dies too)
    from app.services import sessions
    monkeypatch.setattr(sessions, "ROTATION_GRACE_S", 0)
    assert client.post(f"{API}/auth/refresh", json={"refresh_token": rt1}).status_code == 401
    assert client.post(f"{API}/auth/refresh", json={"refresh_token": rt2}).status_code == 401


def test_m2_logout_and_deactivation_revoke_sessions(client, admin_headers):
    rt = _refresh_token(client, "bob@test.dev")
    assert client.post(f"{API}/auth/logout", json={"refresh_token": rt}).status_code == 204
    assert client.post(f"{API}/auth/refresh", json={"refresh_token": rt}).status_code == 401
    rt = _refresh_token(client, "bob@test.dev")
    bob = next(u for u in client.get(f"{API}/users", params={"role": "customer"}, headers=admin_headers).json()
               if u["email"] == "bob@test.dev")
    client.patch(f"{API}/users/{bob['id']}", json={"is_active": False}, headers=admin_headers)
    assert client.post(f"{API}/auth/refresh", json={"refresh_token": rt}).status_code == 401


def test_m2_refresh_token_stored_hashed(client, db):
    from app.db.models import RefreshToken
    rt = _refresh_token(client, "ana@test.dev")
    db.expire_all()
    stored = [r.token_hash for r in db.query(RefreshToken).all()]
    assert rt not in stored and all(len(h) == 64 for h in stored)


# ---------------------------------------------------------------- M3: timezone-aware timestamps
def test_m3_api_returns_utc_offsets(client, customer_headers):
    t = raise_ticket(client, customer_headers()).json()
    assert t["created_at"].endswith(("+00:00", "Z")), t["created_at"]
    o = client.get(f"{API}/me/orders", headers=customer_headers()).json()[0]
    assert o["placed_at"].endswith(("+00:00", "Z"))


# ---------------------------------------------------------------- L1: pinned category
def test_l1_payment_issue_is_billing_even_if_model_says_refund(db):
    from app.services.triage import prioritize
    out = prioritize({"category": "refund", "priority": 1, "reason": "r", "issue_type": "payment_issue",
                      "text": "charged twice"})
    assert out["category"] == "billing"


# ---------------------------------------------------------------- L2: LIKE wildcards literal
def test_l2_percent_search_is_literal(client, customer_headers, admin_headers):
    raise_ticket(client, customer_headers())
    assert client.get(f"{API}/tickets", params={"q": "%"}, headers=admin_headers).json()["total"] == 0
    assert client.get(f"{API}/tickets", params={"q": "GR-100001"}, headers=admin_headers).json()["total"] == 1


# ---------------------------------------------------------------- L3: docs off in production
def test_l3_docs_disabled_in_production():
    from app.core.config import Settings
    s = Settings(environment="production", jwt_secret="x" * 40, bootstrap_admin_password="Strong!Pass123")
    assert s.docs_enabled is False
    assert Settings(environment="development").docs_enabled is True


# ---------------------------------------------------------------- L4: stale AI suggestion
def test_l4_suggestion_marked_stale_after_new_messages(client, customer_headers):
    ref = raise_ticket(client, customer_headers()).json()["reference"]
    admin = login(client, "admin@test.dev", "AdminPass!1")
    tid = client.get(f"{API}/tickets", headers=admin).json()["items"][0]["id"]
    client.post(f"{API}/tickets/{tid}/assist", headers=admin)
    assert client.get(f"{API}/tickets/{tid}", headers=admin).json()["suggestion"]["stale"] is False
    client.post(f"{API}/me/tickets/{ref}/messages", json={"body": "Also the milk was sour"}, headers=customer_headers())
    assert client.get(f"{API}/tickets/{tid}", headers=admin).json()["suggestion"]["stale"] is True


# ---------------------------------------------------------------- L5: eligibility
def test_l5_eligibility_rules(client, customer_headers, db):
    h = customer_headers()
    orders = {o["order_number"]: o for o in client.get(f"{API}/me/orders", headers=h).json()}
    late, on_time = orders["GR-100001"], orders["GR-100002"]
    assert "late_delivery" in late["allowed_issues"] and "late_delivery" not in on_time["allowed_issues"]
    assert "not_delivered" in on_time["blocked_issues"]
    r = raise_ticket(client, h, number="GR-100002", issue="late_delivery", items=(),
                     description="My order arrived late yesterday evening.")
    assert r.status_code == 422 and "delivered on time" in r.json()["detail"]
    # old delivery: item issues expire after TICKET_WINDOW_DAYS
    o = db.query(Order).filter_by(order_number="GR-100002").one()
    o.delivered_at = utcnow() - timedelta(days=45)
    db.commit()
    r = raise_ticket(client, h, number="GR-100002", issue="damaged_item", items=(),
                     description="Bottle was broken when it arrived.")
    assert r.status_code == 422 and "within 30 days" in r.json()["detail"]


# ---------------------------------------------------------------- L6: photos on follow-ups
def test_l6_customer_adds_photos_later(client, customer_headers):
    h = customer_headers()
    ref = raise_ticket(client, h).json()["reference"]
    r = client.post(f"{API}/me/tickets/{ref}/photos", headers=h,
                    files=[("images", ("later.png", png_bytes(), "image/png"))])
    assert r.status_code == 200 and len(r.json()["attachments"]) == 1
    assert r.json()["messages"][-1]["text"].startswith("📎 Added 1 photo")
    bad = client.post(f"{API}/me/tickets/{ref}/photos", headers=h,
                      files=[("images", ("x.png", b"not an image", "image/png"))])
    assert bad.status_code == 422
    other = client.post(f"{API}/me/tickets/{ref}/photos", headers=customer_headers("bob@test.dev"),
                        files=[("images", ("x.png", png_bytes(), "image/png"))])
    assert other.status_code == 404


# ---------------------------------------------------------------- L7: notifications
def test_l7_notifications_written_to_outbox(client, customer_headers, agent_headers):
    outbox = Path(os.environ["OUTBOX_DIR"])
    before = set(outbox.glob("*.eml")) if outbox.exists() else set()
    ref = raise_ticket(client, customer_headers()).json()["reference"]
    kumar = agent_headers("kumar@test.dev")
    tid = client.get(f"{API}/tickets", headers=kumar).json()["items"][0]["id"]
    client.post(f"{API}/tickets/{tid}/messages", json={"body": "Refund done!"}, headers=kumar)
    client.post(f"{API}/me/tickets/{ref}/messages", json={"body": "Thanks"}, headers=customer_headers())
    new = {p.name: p.read_text(encoding="utf-8") for p in set(outbox.glob("*.eml")) - before}
    assert any("ana@test.dev" in n and "We received your request" in t for n, t in new.items())
    assert any("ana@test.dev" in n and "new reply" in t and "Refund done!" in t for n, t in new.items())
    assert any("kumar@test.dev" in n and "replied" in t for n, t in new.items())
    assert not any("@test.dev" in t.split("\n\n", 1)[1] and "kumar@" in t and "ana@" in n for n, t in new.items())
