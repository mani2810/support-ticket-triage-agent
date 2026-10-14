"""Startup tasks and demo seed data.

    python -m app.bootstrap --seed     # demo agents, customers, grocery orders and sample tickets (from backend/)
"""
import argparse
import logging
import random
import secrets
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .core.config import DEMO_ADMIN_PASSWORD, get_settings
from .core.security import hash_password, verify_password
from .db.models import Order, OrderItem, Ticket, User, utcnow
from .db.session import Base, SessionLocal, engine
from .schemas import TicketCreate

log = logging.getLogger(__name__)

DEMO_PASSWORD = "Agent!2026"
DEMO_CUSTOMER_PASSWORD = "Customer!2026"
DEMO_AGENTS = [
    ("Kumar", "kumar@triagedesk.dev", ["refund", "billing", "damaged"]),
    ("Deepak", "deepak@triagedesk.dev", ["refund", "billing", "delivery"]),
    ("Priya", "priya@triagedesk.dev", ["technical", "account"]),
    ("Arjun", "arjun@triagedesk.dev", ["delivery", "damaged", "other"]),
]
DEMO_CUSTOMERS = [
    ("Ananya Sharma", "ananya@example.com", "+91 98480 11111", "12 MG Road, Guntur"),
    ("Rahul Verma", "rahul@example.com", "+91 98480 22222", "45 Ring Road, Vijayawada"),
    ("Meera Iyer", "meera@example.com", "+91 98480 33333", "7 Beach Road, Visakhapatnam"),
]
CATALOGUE = [  # name, category, price (INR)
    ("Amul Toned Milk 1L", "dairy", 68), ("Farm Fresh Eggs (12)", "dairy", 96), ("Paneer 200g", "dairy", 90),
    ("Greek Yogurt 400g", "dairy", 140), ("Bananas (1 dozen)", "fruits", 60), ("Alphonso Mangoes 1kg", "fruits", 420),
    ("Apples Shimla 1kg", "fruits", 180), ("Tomatoes 1kg", "vegetables", 40), ("Onions 2kg", "vegetables", 70),
    ("Spinach bunch", "vegetables", 30), ("Basmati Rice 5kg", "staples", 650), ("Toor Dal 1kg", "staples", 165),
    ("Sunflower Oil 1L", "staples", 155), ("Atta 10kg", "staples", 480), ("Brown Bread", "bakery", 55),
    ("Olive Oil 1L", "staples", 899), ("Almonds 500g", "dry fruits", 520), ("Dishwash Liquid 750ml", "household", 115),
    ("Glass Jar Honey 500g", "pantry", 320), ("Coffee Powder 200g", "pantry", 285),
]
# Each customer: list of orders as (days_ago, status, minutes_late, [(catalogue index, qty), ...])
DEMO_ORDERS = {
    "ananya@example.com": [
        (1, "delivered", 95, [(1, 2), (5, 1), (10, 1), (15, 1)]),        # late delivery, big basket
        (6, "delivered", -10, [(0, 4), (7, 2), (14, 2), (18, 1)]),       # honey jar (glass) -> damaged
        (20, "delivered", -5, [(11, 2), (12, 1), (16, 1)]),
    ],
    "rahul@example.com": [
        (0, "out_for_delivery", None, [(13, 1), (8, 1), (19, 1)]),
        (2, "delivered", 0, [(2, 2), (3, 1), (9, 3), (4, 1)]),           # missing item
        (12, "delivered", 30, [(6, 2), (17, 1)]),
    ],
    "meera@example.com": [
        (3, "placed", None, [(10, 1), (15, 1), (16, 1)]),                # not delivered, past promise
        (9, "delivered", -15, [(5, 2), (6, 1), (0, 2)]),                 # spoiled mangoes
    ],
}
# Sample tickets raised through the real service (so they are classified and routed like live ones)
DEMO_TICKETS = [
    ("ananya@example.com", 1, "damaged_item", ["Glass Jar Honey 500g"],
     "The honey jar arrived cracked and honey leaked over the other items in the bag."),
    ("ananya@example.com", 0, "late_delivery", [],
     "My order was promised by 7 pm but arrived after 8:30 pm. The milk was warm."),
    ("rahul@example.com", 1, "missing_item", ["Greek Yogurt 400g"],
     "Greek yogurt was billed but not in the bag. Everything else was fine."),
    ("meera@example.com", 1, "quality_issue", ["Alphonso Mangoes 1kg"],
     "Both packs of mangoes were overripe and spoiled inside. Please refund."),
]


def init_db() -> None:
    """Bring the schema up to date on startup.

    * development: run the Alembic migrations (`upgrade head`). Never `create_all()`: that created new
      tables ahead of their migration (which then failed) and never altered existing tables, so a
      model change could silently leave the database without a column.
    * test: create_all on a throwaway database (fast; migrations are tested separately in CI).
    * production: migrations are a deliberate release step; refuse to start if the DB is behind.
    """
    s = get_settings()
    if s.environment == "test":
        Base.metadata.create_all(engine)
        return
    from alembic import command
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from .core.config import BACKEND_DIR

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.attributes["app_logging"] = True          # keep the app's logging config (env.py would reset it)
    if s.environment == "development":
        command.upgrade(cfg, "head")
        return
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    if current != head:
        raise RuntimeError(f"Database schema is at {current}, code expects {head}: run `alembic upgrade head`")


def ensure_admin(db: Session) -> None:
    s = get_settings()
    if not db.scalar(select(User.id).where(User.role == "admin")):
        db.add(User(email=s.bootstrap_admin_email.lower(), name="Admin", role="admin",
                    password_hash=hash_password(s.bootstrap_admin_password)))
        db.commit()
        log.warning("Created bootstrap admin %s - change the password!", s.bootstrap_admin_email)


def warn_insecure_defaults(db: Session) -> None:
    """Loud startup warnings for anything a user should change before exposing the app."""
    for admin in db.scalars(select(User).where(User.role == "admin", User.is_active.is_(True))):
        if verify_password(DEMO_ADMIN_PASSWORD, admin.password_hash):
            log.warning("SECURITY: admin %s still uses the public demo password - change it on the Team page",
                        admin.email)
    for agent in db.scalars(select(User).where(User.role == "agent", User.is_active.is_(True)).limit(1)):
        if verify_password(DEMO_PASSWORD, agent.password_hash):
            log.warning("SECURITY: demo accounts with public passwords exist - fine locally, "
                        "remove them before exposing the app")


def _order_number(db: Session) -> str:
    while True:
        n = f"GR-{secrets.randbelow(900000) + 100000}"
        if not db.scalar(select(Order.id).where(Order.order_number == n)):
            return n


def _make_order(db: Session, customer: User, days_ago: int, status: str, minutes_late, lines) -> Order:
    placed = utcnow() - timedelta(days=days_ago, hours=random.randint(2, 6))
    promised = placed + timedelta(hours=2)
    if status == "placed":                                  # stuck: promise already passed
        promised = utcnow() - timedelta(hours=3)
    delivered = promised + timedelta(minutes=minutes_late) if status == "delivered" else None
    items = [OrderItem(name=CATALOGUE[i][0], category=CATALOGUE[i][1], quantity=q, unit_price=CATALOGUE[i][2])
             for i, q in lines]
    subtotal = sum(i.quantity * i.unit_price for i in items)
    fee = 0.0 if subtotal >= 499 else 35.0
    order = Order(order_number=_order_number(db), customer_id=customer.id, status=status, placed_at=placed,
                  promised_by=promised, delivered_at=delivered, delivery_fee=fee, total=round(subtotal + fee, 2),
                  payment_method=random.choice(["upi", "card", "cod"]), address=customer.address or "", items=items)
    db.add(order)
    return order


def seed_demo(db: Session) -> None:
    from .services.tickets import create_ticket

    random.seed(7)
    for name, email, skills in DEMO_AGENTS:
        if not db.scalar(select(User.id).where(User.email == email)):
            db.add(User(name=name, email=email, role="agent", skills=skills,
                        password_hash=hash_password(DEMO_PASSWORD)))
    customers: dict[str, User] = {}
    for name, email, phone, address in DEMO_CUSTOMERS:
        user = db.scalar(select(User).where(User.email == email))
        if not user:
            user = User(name=name, email=email, role="customer", phone=phone, address=address,
                        password_hash=hash_password(DEMO_CUSTOMER_PASSWORD))
            db.add(user)
        customers[email] = user
    db.commit()

    for email, orders in DEMO_ORDERS.items():
        customer = customers[email]
        if db.scalar(select(func.count(Order.id)).where(Order.customer_id == customer.id)):
            continue                                        # idempotent: don't duplicate orders
        for spec in orders:
            _make_order(db, customer, *spec)
    db.commit()

    for email, order_idx, issue, items, body in DEMO_TICKETS:
        customer = customers[email]
        order = db.scalars(select(Order).where(Order.customer_id == customer.id)
                           .order_by(Order.placed_at.desc())).all()[order_idx]
        if db.scalar(select(func.count(Ticket.id)).where(Ticket.order_id == order.id)):
            continue                                        # idempotent: one demo ticket per order
        from .schemas import ISSUE_LABELS
        t = create_ticket(db, TicketCreate(
            customer_name=customer.name, customer_email=customer.email, customer_id=customer.id, order_id=order.id,
            issue_type=issue, affected_items=items, body=body,
            subject=f"{ISSUE_LABELS[issue]} - order {order.order_number}"), order=order)
        print(f"{t.reference}  {order.order_number}  {t.category:<9} P{t.priority}  "
              f"-> {t.assignee.name if t.assignee else '-'}  ({t.classified_by})")
    print("\nDemo logins:")
    print(f"  admin     {get_settings().bootstrap_admin_email} / (BOOTSTRAP_ADMIN_PASSWORD)")
    print(f"  agents    kumar@ / deepak@ / priya@ / arjun@triagedesk.dev / {DEMO_PASSWORD}")
    print(f"  customers ananya@ / rahul@ / meera@example.com / {DEMO_CUSTOMER_PASSWORD}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true", help="add demo agents, customers, orders and sample tickets")
    args = ap.parse_args()
    init_db()
    with SessionLocal() as session:
        ensure_admin(session)
        if args.seed:
            seed_demo(session)
