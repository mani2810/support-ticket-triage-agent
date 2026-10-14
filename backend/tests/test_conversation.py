"""Customer <-> support conversation: what each side sees, re-opening, and the bugs this fixed."""
from tests.conftest import API, login, raise_ticket


def _setup(client, customer_headers):
    ref = raise_ticket(client, customer_headers()).json()["reference"]
    admin = login(client, "admin@test.dev", "AdminPass!1")
    t = client.get(f"{API}/tickets", headers=admin).json()["items"][0]
    agent = login(client, t["assignee"]["email"], "AgentPass!1")
    return ref, t["id"], agent


def _me(client, customer_headers, ref):
    return client.get(f"{API}/me/tickets/{ref}", headers=customer_headers()).json()


def test_agent_reply_reaches_customer_and_moves_to_in_progress(client, customer_headers):
    ref, tid, agent = _setup(client, customer_headers)
    r = client.post(f"{API}/tickets/{tid}/messages", json={"body": "Sorry! We are refunding the honey jar."},
                    headers=agent)
    assert r.status_code == 200 and r.json()["status"] == "in_progress"
    view = _me(client, customer_headers, ref)
    assert [m["role"] for m in view["messages"]] == ["customer", "support"]
    assert view["messages"][1]["text"] == "Sorry! We are refunding the honey jar."
    assert view["messages"][1]["author"].endswith("(FreshCart Support)")            # no staff email
    assert "@" not in view["messages"][1]["author"]
    assert view["awaiting"] == "customer"


def test_internal_notes_never_reach_customer(client, customer_headers):
    ref, tid, agent = _setup(client, customer_headers)
    client.post(f"{API}/tickets/{tid}/comments", json={"note": "INTERNAL: customer filed 3 claims this month"},
                headers=agent)
    view = _me(client, customer_headers, ref)
    assert all("INTERNAL" not in m["text"] for m in view["messages"])
    assert all("INTERNAL" not in e["detail"] for e in view["timeline"])


def test_message_with_status_change_is_delivered(client, customer_headers):
    """Regression: a note typed with 'in_progress' used to be visible only in the staff audit log."""
    ref, tid, agent = _setup(client, customer_headers)
    client.post(f"{API}/tickets/{tid}/status", json={"status": "in_progress", "note": "Checking with the store"},
                headers=agent)
    assert _me(client, customer_headers, ref)["messages"][-1]["text"] == "Checking with the store"


def test_message_without_status_change_is_not_dropped(client, customer_headers):
    """Regression: saving the SAME status with a note silently discarded the note."""
    ref, tid, agent = _setup(client, customer_headers)
    client.post(f"{API}/tickets/{tid}/status", json={"status": "in_progress"}, headers=agent)
    client.post(f"{API}/tickets/{tid}/status", json={"status": "in_progress", "note": "Refund approved today"},
                headers=agent)
    assert _me(client, customer_headers, ref)["messages"][-1]["text"] == "Refund approved today"


def test_reply_and_resolve(client, customer_headers):
    ref, tid, agent = _setup(client, customer_headers)
    r = client.post(f"{API}/tickets/{tid}/messages", json={"body": "Refund of INR 320 issued.", "resolve": True},
                    headers=agent).json()
    assert r["status"] == "resolved" and r["resolution_note"] == "Refund of INR 320 issued."
    view = _me(client, customer_headers, ref)
    assert view["status"] == "resolved" and view["messages"][-1]["text"] == "Refund of INR 320 issued."
    assert sum(m["text"] == "Refund of INR 320 issued." for m in view["messages"]) == 1   # not duplicated
    assert view["awaiting"] is None


def test_customer_reply_reaches_agent_and_reopens_resolved_ticket(client, customer_headers):
    ref, tid, agent = _setup(client, customer_headers)
    client.post(f"{API}/tickets/{tid}/messages", json={"body": "Refund issued.", "resolve": True}, headers=agent)
    r = client.post(f"{API}/me/tickets/{ref}/messages", json={"body": "I have not received the refund yet."},
                    headers=customer_headers())
    assert r.status_code == 200 and r.json()["status"] == "open" and r.json()["awaiting"] == "support"
    staff = client.get(f"{API}/tickets/{tid}", headers=agent).json()
    assert staff["messages"][-1] == {**staff["messages"][-1], "role": "customer",
                                     "text": "I have not received the refund yet."}
    assert staff["awaiting"] == "support" and staff["resolved_at"] is None


def test_customer_cannot_message_someone_elses_ticket(client, customer_headers):
    ref, _, _ = _setup(client, customer_headers)
    r = client.post(f"{API}/me/tickets/{ref}/messages", json={"body": "hi"}, headers=customer_headers("bob@test.dev"))
    assert r.status_code == 404


def test_message_validation(client, customer_headers):
    ref, tid, agent = _setup(client, customer_headers)
    assert client.post(f"{API}/me/tickets/{ref}/messages", json={"body": ""},
                       headers=customer_headers()).status_code == 422
    assert client.post(f"{API}/tickets/{tid}/messages", json={"body": "x" * 2001}, headers=agent).status_code == 422
