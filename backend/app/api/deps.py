"""Shared FastAPI dependencies: DB session, current user, role guards, object-level access.

Access model (deny by default):
    admin     every ticket, every staff endpoint
    agent     only tickets assigned to them; no admin endpoints
    customer  only /me endpoints and only their own orders / tickets
"""
import logging
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from ..core.security import decode_access_token
from ..db.models import STAFF_ROLES, Ticket, User
from ..db.session import get_db

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")
DB = Annotated[Session, Depends(get_db)]
audit = logging.getLogger("audit")


def current_user(db: DB, token: Annotated[str, Depends(oauth2)]) -> User:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token",
                                 headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        raise unauthorized
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise unauthorized
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def _require(roles: tuple[str, ...], label: str):
    def guard(user: CurrentUser) -> User:
        if user.role not in roles:
            audit.warning("forbidden", extra={"user_id": user.id, "role": user.role, "needs": label})
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"{label} only")
        return user
    return guard


Admin = Annotated[User, Depends(_require(("admin",), "Admin"))]
Staff = Annotated[User, Depends(_require(STAFF_ROLES, "Staff"))]
Customer = Annotated[User, Depends(_require(("customer",), "Customer"))]


def get_ticket_for_staff(ticket_id: int, db: DB, user: Staff) -> Ticket:
    """Admins see all tickets; agents only those assigned to them (404 otherwise: no existence leak)."""
    ticket = db.get(Ticket, ticket_id)
    if not ticket or (user.role == "agent" and ticket.assigned_to_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
    return ticket


AccessibleTicket = Annotated[Ticket, Depends(get_ticket_for_staff)]
