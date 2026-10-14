import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...core.ratelimit import client_ip, limiter
from ...core.security import create_access_token, hash_password, login_guard, verify_password
from ...db.models import User
from ...schemas import RegisterIn, Token, UserOut
from ...services import sessions
from ..deps import DB, CurrentUser

router = APIRouter(prefix="/auth", tags=["auth"])
audit = logging.getLogger("audit")


class RefreshIn(BaseModel):
    refresh_token: str = Field(..., min_length=20, max_length=200)


def _token_pair(db, user: User, refresh: str | None = None) -> Token:
    access, expires_in = create_access_token(user.id, user.role)
    return Token(access_token=access, expires_in=expires_in, user=UserOut.model_validate(user),
                 refresh_token=refresh or sessions.issue(db, user))


@router.post("/login", response_model=Token)
@limiter.limit("20/minute")
def login(request: Request, db: DB, form: Annotated[OAuth2PasswordRequestForm, Depends()]):
    """OAuth2 password flow: `username` is the email address."""
    email = form.username.strip().lower()
    client = client_ip(request)
    # Lockout is per (email, client IP): an attacker elsewhere cannot lock the real owner out.
    guard_key = f"{email}|{client}"
    if wait := login_guard.locked_for(guard_key):
        audit.warning("login blocked (locked)", extra={"email": email, "client": client})
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            f"Too many failed attempts. Try again in {wait // 60 + 1} minute(s).",
                            headers={"Retry-After": str(wait)})

    user = db.scalar(select(User).where(User.email == email))
    # Always run bcrypt (dummy hash for unknown emails): same response time either way.
    valid = verify_password(form.password, user.password_hash if user else None)
    if not valid:
        locked = login_guard.record_failure(guard_key)
        audit.warning("login failed", extra={"email": email, "client": client, "locked": locked})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    if not user.is_active:
        audit.warning("login refused (disabled)", extra={"email": email, "client": client})
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account disabled")

    login_guard.record_success(guard_key)
    audit.info("login ok", extra={"user_id": user.id, "role": user.role, "client": client})
    return _token_pair(db, user)


@router.post("/refresh", response_model=Token)
@limiter.limit("30/minute")
def refresh(request: Request, body: RefreshIn, db: DB):
    """Exchange a refresh token for a new access + refresh token (the old refresh token stops working)."""
    user, new_refresh = sessions.rotate(db, body.refresh_token)
    return _token_pair(db, user, refresh=new_refresh)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(body: RefreshIn, db: DB):
    """Revoke the session (refresh token family). Idempotent."""
    sessions.revoke(db, body.refresh_token)


@router.post("/register", response_model=Token, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
def register(request: Request, body: RegisterIn, db: DB):
    """Customer self-sign-up. Staff accounts are created by an admin, never here."""
    email = body.email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists. Please log in.")
    user = User(email=email, name=body.name.strip(), role="customer", phone=body.phone, address=body.address,
                password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    audit.info("customer registered", extra={"user_id": user.id, "client": client_ip(request)})
    return _token_pair(db, user)


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    return user
