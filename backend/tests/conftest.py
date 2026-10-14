import io
import os
import tempfile

# Configure BEFORE the app is imported: isolated SQLite file + upload dir, offline LLM, test env.
_tmp = tempfile.mkdtemp()
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_tmp}/test.db",
    "UPLOAD_DIR": f"{_tmp}/uploads",
    "OUTBOX_DIR": f"{_tmp}/outbox",
    "LLM_MODE": "rules",
    "ENVIRONMENT": "test",
    "GROQ_API_KEY": "",
    "BOOTSTRAP_ADMIN_EMAIL": "admin@test.dev",
    "BOOTSTRAP_ADMIN_PASSWORD": "AdminPass!1",
    "BCRYPT_ROUNDS": "4",
    "LOG_FILE": "",
})

from datetime import timedelta  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.core.ratelimit import limiter  # noqa: E402
from app.core.security import hash_password, login_guard  # noqa: E402
from app.db.models import Order, OrderItem, User, utcnow  # noqa: E402
from app.db.session import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402

limiter.enabled = False
API = "/api/v1"

AGENTS = [("Kumar", "kumar@test.dev", ["refund", "billing", "damaged"]),
          ("Deepak", "deepak@test.dev", ["refund", "billing", "delivery"]),
          ("Priya", "priya@test.dev", ["technical", "account"])]
CUSTOMER_PW = "CustPass!1"


def _order(customer_id: int, number: str, items, fee=35.0, late_minutes=0, status="delivered") -> Order:
    placed = utcnow() - timedelta(days=1)
    promised = placed + timedelta(hours=2)
    lines = [OrderItem(name=n, category="grocery", quantity=q, unit_price=p) for n, q, p in items]
    subtotal = sum(q * p for _, q, p in items)
    return Order(order_number=number, customer_id=customer_id, status=status, placed_at=placed,
                 promised_by=promised, delivered_at=promised + timedelta(minutes=late_minutes),
                 delivery_fee=fee, total=subtotal + fee, payment_method="upi", items=lines)


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    login_guard._failures.clear()
    login_guard._locked_until.clear()
    with SessionLocal() as s:
        for name, email, skills in AGENTS:
            s.add(User(name=name, email=email, role="agent", skills=skills, password_hash=hash_password("AgentPass!1")))
        ana = User(name="Ana Customer", email="ana@test.dev", role="customer", password_hash=hash_password(CUSTOMER_PW))
        bob = User(name="Bob Customer", email="bob@test.dev", role="customer", password_hash=hash_password(CUSTOMER_PW))
        s.add_all([ana, bob])
        s.flush()
        s.add_all([
            _order(ana.id, "GR-100001", [("Honey Jar", 1, 320.0), ("Milk 1L", 2, 68.0)], late_minutes=95),
            _order(ana.id, "GR-100002", [("Basmati Rice 5kg", 1, 650.0), ("Olive Oil 1L", 3, 899.0)], fee=0),
            _order(bob.id, "GR-200001", [("Bananas", 1, 60.0)]),
        ])
        s.commit()
        yield s


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        yield c


def login(client, email, password):
    r = client.post(f"{API}/auth/login", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def admin_headers(client):
    return login(client, "admin@test.dev", "AdminPass!1")


@pytest.fixture()
def agent_headers(client):
    return lambda email: login(client, email, "AgentPass!1")


@pytest.fixture()
def customer_headers(client):
    return lambda email="ana@test.dev": login(client, email, CUSTOMER_PW)


def png_bytes(size=(40, 30), fmt="PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, fmt)
    return buf.getvalue()


def order_id(client, headers, number):
    return next(o["id"] for o in client.get(f"{API}/me/orders", headers=headers).json()
                if o["order_number"] == number)


def raise_ticket(client, headers, number="GR-100001", issue="damaged_item", items=("Honey Jar",),
                 description="The honey jar arrived cracked and leaked everywhere.", files=None):
    data = {"order_id": str(order_id(client, headers, number)), "issue_type": issue, "description": description,
            "affected_items": list(items)}
    return client.post(f"{API}/me/tickets", headers=headers, data=data, files=files or [])
