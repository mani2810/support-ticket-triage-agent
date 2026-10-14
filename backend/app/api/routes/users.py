"""Admin: manage staff accounts and view the dashboard."""
import logging

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from ...core.security import hash_password
from ...db.models import STAFF_ROLES, User
from ...schemas import Stats, UserCreate, UserOut, UserUpdate
from ...services import sessions
from ...services import tickets as svc
from ..deps import DB, Admin

router = APIRouter(tags=["admin"])


@router.get("/users", response_model=list[UserOut])
def list_users(db: DB, _: Admin, role: str | None = None):
    """Staff by default; `?role=customer` lists customers."""
    stmt = select(User).order_by(User.role, User.name)
    stmt = stmt.where(User.role == role) if role else stmt.where(User.role.in_(STAFF_ROLES))
    return db.scalars(stmt).all()


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, db: DB, _: Admin):
    if db.scalar(select(User.id).where(User.email == body.email.lower())):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already exists")
    user = User(email=body.email.lower(), name=body.name, role=body.role, skills=list(body.skills),
                password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: UserUpdate, db: DB, admin: Admin):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.id == admin.id and body.is_active is False:
        raise HTTPException(status.HTTP_409_CONFLICT, "You cannot deactivate yourself")
    data = body.model_dump(exclude_none=True)
    password_changed = "password" in data
    if password_changed:
        user.password_hash = hash_password(data.pop("password"))
    deactivating = user.is_active and data.get("is_active") is False
    for k, v in data.items():
        setattr(user, k, list(v) if k == "skills" else v)
    db.commit()
    db.refresh(user)
    if deactivating or password_changed:
        sessions.revoke_user(db, user.id)          # log out everywhere
    if deactivating and user.role == "agent":
        moved = svc.reroute_agent_tickets(db, user, admin)
        logging.getLogger("audit").info("agent deactivated", extra={"user_id": user.id, "tickets_rerouted": moved})
    return user


@router.get("/stats", response_model=Stats)
def dashboard_stats(db: DB, _: Admin):
    return svc.stats(db)
