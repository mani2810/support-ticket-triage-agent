"""End-to-end API tests: customer portal, staff workflow, admin, access control."""
from tests.conftest import API, png_bytes, raise_ticket


def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json()["status"] == "ready"


# ---------------------------------------------------------------- customer portal
def test_register_and_login_as_customer(client):
    r = client.post(f"{API}/auth/register", json={"name": "New Shopper", "email": "New@Test.dev",
                                                   "password": "Shopper!2026"})
    assert r.status_code == 201 and r.json()["user"]["role"] == "customer"
    assert client.post(f"{API}/auth/register", json={"name": "Dup", "email": "new@test.dev",
                                                      "password": "Shopper!2026"}).status_code == 409
    # registration can never create staff
    r = client.post(f"{API}/auth/register", json={"name": "Evil", "email": "evil@test.dev",
                                                   "password": "Shopper!2026", "role": "admin"})
    assert r.json()["user"]["role"] == "customer"


def test_customer_sees_only_own_orders(client, customer_headers):
    ana = client.get(f"{API}/me/orders", headers=customer_headers()).json()
    assert {o["order_number"] for o in ana} == {"GR-100001", "GR-100002"}
    assert ana[0]["items"] and ana[0]["items"][0]["line_total"] > 0
    bob = customer_headers("bob@test.dev")
    bob_order = client.get(f"{API}/me/orders", headers=bob).json()[0]["id"]
    assert client.get(f"{API}/me/orders/{bob_order}", headers=customer_headers()).status_code == 404


def test_raise_ticket_autofills_identity_and_order(client, customer_headers):
    h = customer_headers()
    r = raise_ticket(client, h, files=[("images", ("crack.png", png_bytes(), "image/png"))])
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["order_number"] == "GR-100001" and t["affected_items"] == ["Honey Jar"]
    assert t["category"] == "damaged" and t["priority"] == 1 and t["assigned_agent"] == "Kumar"
    assert len(t["attachments"]) == 1 and "ai_reason" not in t           # no internal data for customers
    assert t["subject"].startswith("Item arrived damaged")
    # one open ticket per order
    again = raise_ticket(client, h)
    assert again.status_code == 409 and t["reference"] in again.json()["detail"]
    orders = client.get(f"{API}/me/orders", headers=h).json()
    assert next(o for o in orders if o["order_number"] == "GR-100001")["open_ticket"] == t["reference"]


def test_cannot_raise_ticket_on_someone_elses_order(client, customer_headers):
    bob = customer_headers("bob@test.dev")
    bob_order = client.get(f"{API}/me/orders", headers=bob).json()[0]["id"]
    r = client.post(f"{API}/me/tickets", headers=customer_headers(),
                    data={"order_id": str(bob_order), "issue_type": "missing_item",
                          "description": "trying to raise a ticket on another person's order"})
    assert r.status_code == 404


def test_affected_items_must_belong_to_order(client, customer_headers):
    t = raise_ticket(client, customer_headers(), items=("Honey Jar", "Gold Necklace")).json()
    assert t["affected_items"] == ["Honey Jar"]


def test_track_by_ticket_id_only_own(client, customer_headers):
    ref = raise_ticket(client, customer_headers()).json()["reference"]
    assert client.get(f"{API}/me/tickets/{ref.lower()}", headers=customer_headers()).json()["reference"] == ref
    assert client.get(f"{API}/me/tickets/{ref}", headers=customer_headers("bob@test.dev")).status_code == 404
    assert len(client.get(f"{API}/me/tickets", headers=customer_headers()).json()) == 1


def test_image_validation(client, customer_headers):
    h = customer_headers()
    fake = raise_ticket(client, h, files=[("images", ("evil.png", b"<?php echo 'pwned'; ?>", "image/png"))])
    assert fake.status_code == 422
    too_many = raise_ticket(client, h, files=[("images", (f"{i}.png", png_bytes(), "image/png")) for i in range(4)])
    assert too_many.status_code == 422
    gif = raise_ticket(client, h, files=[("images", ("a.gif", png_bytes(fmt="GIF"), "image/gif"))])
    assert gif.status_code == 422
    # nothing was created by the rejected attempts
    assert client.get(f"{API}/me/tickets", headers=h).json() == []


def test_uploaded_image_is_reencoded_and_served_only_to_authorised(client, customer_headers, admin_headers,
                                                                   agent_headers):
    jpeg = png_bytes(fmt="JPEG") + b"TRAILING-PAYLOAD"
    t = raise_ticket(client, customer_headers(), files=[("images", ("p.jpg", jpeg, "image/jpeg"))]).json()
    att = t["attachments"][0]
    mine = client.get(f"{API}/me/tickets/{t['reference']}/attachments/{att['id']}", headers=customer_headers())
    assert mine.status_code == 200 and mine.headers["content-type"] == "image/jpeg"
    assert b"TRAILING-PAYLOAD" not in mine.content                         # re-encoded
    assert client.get(f"{API}/me/tickets/{t['reference']}/attachments/{att['id']}",
                      headers=customer_headers("bob@test.dev")).status_code == 404
    tid = client.get(f"{API}/tickets", headers=admin_headers).json()["items"][0]["id"]
    assert client.get(f"{API}/tickets/{tid}/attachments/{att['id']}", headers=admin_headers).status_code == 200
    assert client.get(f"{API}/tickets/{tid}/attachments/{att['id']}",
                      headers=agent_headers("priya@test.dev")).status_code == 404   # not her ticket


# ---------------------------------------------------------------- role separation
def test_customers_cannot_use_staff_endpoints(client, customer_headers):
    h = customer_headers()
    raise_ticket(client, h)
    assert client.get(f"{API}/tickets", headers=h).status_code == 403
    assert client.get(f"{API}/tickets/1", headers=h).status_code == 403
    assert client.post(f"{API}/tickets/1/assist", headers=h).status_code == 403
    assert client.get(f"{API}/stats", headers=h).status_code == 403
    assert client.get(f"{API}/users", headers=h).status_code == 403


def test_staff_cannot_use_customer_endpoints(client, agent_headers):
    assert client.get(f"{API}/me/orders", headers=agent_headers("kumar@test.dev")).status_code == 403


def test_login_failures(client):
    assert client.post(f"{API}/auth/login", data={"username": "admin@test.dev", "password": "nope"}).status_code == 401
    assert client.get(f"{API}/tickets").status_code == 401
    assert client.get(f"{API}/tickets", headers={"Authorization": "Bearer garbage"}).status_code == 401


# ---------------------------------------------------------------- staff workflow
def test_agent_sees_only_own_queue_with_full_case(client, customer_headers, agent_headers):
    raise_ticket(client, customer_headers())                                       # -> Kumar
    kumar = agent_headers("kumar@test.dev")
    page = client.get(f"{API}/tickets", headers=kumar).json()
    assert page["total"] == 1 and page["items"][0]["order_number"] == "GR-100001"
    detail = client.get(f"{API}/tickets/{page['items'][0]['id']}", headers=kumar).json()
    assert detail["order"]["order_number"] == "GR-100001" and len(detail["order"]["items"]) == 2
    assert detail["customer_history"]["orders_total"] == 2
    assert client.get(f"{API}/tickets", headers=agent_headers("priya@test.dev")).json()["total"] == 0
    assert client.get(f"{API}/tickets/{page['items'][0]['id']}",
                      headers=agent_headers("priya@test.dev")).status_code == 404


def test_status_workflow_and_customer_timeline(client, customer_headers, agent_headers, admin_headers):
    ref = raise_ticket(client, customer_headers()).json()["reference"]
    h = agent_headers("kumar@test.dev")
    tid = client.get(f"{API}/tickets", headers=h).json()["items"][0]["id"]
    client.post(f"{API}/tickets/{tid}/comments", json={"note": "internal: customer is a VIP"}, headers=h)
    assert client.post(f"{API}/tickets/{tid}/status", json={"status": "resolved"}, headers=h).status_code == 422
    r = client.post(f"{API}/tickets/{tid}/status", json={"status": "resolved", "note": "Refunded INR 320"}, headers=h)
    assert r.json()["status"] == "resolved"
    assert client.post(f"{API}/tickets/{tid}/status", json={"status": "open"}, headers=h).status_code == 409
    view = client.get(f"{API}/me/tickets/{ref}", headers=customer_headers()).json()
    assert view["status"] == "resolved" and view["resolution_note"] == "Refunded INR 320"
    assert all("VIP" not in e["detail"] for e in view["timeline"])                 # internal notes hidden
    assert client.post(f"{API}/tickets/{tid}/status", json={"status": "open"},
                       headers=admin_headers).status_code == 200


def test_admin_overview(client, customer_headers, admin_headers):
    raise_ticket(client, customer_headers())
    s = client.get(f"{API}/stats", headers=admin_headers).json()
    assert s["total"] == 1 and s["customers"] == 2 and s["orders"] == 3 and len(s["agents"]) == 3
    kumar = next(a for a in s["agents"] if a["name"] == "Kumar")
    assert kumar["open_tickets"] == 1
    page = client.get(f"{API}/tickets", params={"assignee_id": kumar["id"]}, headers=admin_headers).json()
    assert page["total"] == 1
    assert client.get(f"{API}/tickets", params={"q": "GR-100001"}, headers=admin_headers).json()["total"] == 1
    customers = client.get(f"{API}/users", params={"role": "customer"}, headers=admin_headers).json()
    assert {c["email"] for c in customers} == {"ana@test.dev", "bob@test.dev"}
    staff = client.get(f"{API}/users", headers=admin_headers).json()
    assert all(u["role"] in ("admin", "agent") for u in staff)


def test_admin_user_management(client, admin_headers):
    r = client.post(f"{API}/users", headers=admin_headers, json={"email": "new@test.dev", "name": "Neha",
                                                                 "password": "Password!1", "skills": ["delivery"]})
    assert r.status_code == 201
    uid = r.json()["id"]
    r = client.patch(f"{API}/users/{uid}", json={"is_active": False}, headers=admin_headers)
    assert r.json()["is_active"] is False
    r = client.post(f"{API}/auth/login", data={"username": "new@test.dev", "password": "Password!1"})
    assert r.status_code == 403
