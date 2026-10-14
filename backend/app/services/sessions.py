"""Refresh-token sessions: stay logged in across page refreshes, with rotation and revocation.

    login            -> access token (JWT, short-lived) + refresh token (random, 7 days)
    POST /refresh    -> old refresh token revoked, new pair issued (rotation)
    reuse of a revoked refresh token -> the whole session family is revoked (theft detection)
      (grace: a token rotated < ROTATION_GRACE_S ago may be presented again - two tabs / double F5 -
       and simply gets a fresh token in the same family)
    logout / account deactivated -> refresh tokens revoked; access tokens already die with is_active
"""
import hashlib
import logging
import secrets
from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..db.models import RefreshToken, User, utcnow

log = logging.getLogger("audit")
ROTATION_GRACE_S = 60


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue(db: Session, user: User, family: str | None = None) -> str:
    token = secrets.token_urlsafe(32)
    db.add(RefreshToken(user_id=user.id, token_hash=_hash(token), family=family or secrets.token_hex(16),
                        expires_at=utcnow() + timedelta(days=get_settings().refresh_token_days)))
    db.commit()
    return token


def rotate(db: Session, token: str) -> tuple[User, str]:
    invalid = HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired. Please log in again.")
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == _hash(token)))
    if row is None:
        raise invalid
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise invalid
    now = utcnow()
    if row.revoked_at is not None:
        recently_rotated = row.revoked_reason == "rotated" and (now - row.revoked_at).total_seconds() < ROTATION_GRACE_S
        if not recently_rotated:
            # A rotated token was used again: someone holds a copy. Kill the whole session family.
            revoke_family(db, row.family, reason="reuse")
            log.warning("refresh token reuse detected - session family revoked", extra={"user_id": row.user_id})
            raise invalid
        return user, issue(db, user, family=row.family)          # benign race (two tabs / quick refresh)
    if row.expires_at < now:
        row.revoked_at, row.revoked_reason = now, "expired"
        db.commit()
        raise invalid
    row.revoked_at, row.revoked_reason = now, "rotated"
    db.commit()
    return user, issue(db, user, family=row.family)


def revoke(db: Session, token: str) -> None:
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == _hash(token)))
    if row:
        revoke_family(db, row.family, reason="logout")


def revoke_family(db: Session, family: str, reason: str) -> None:
    """Revoke every token of a session - including recently rotated ones, which lose their grace."""
    db.execute(update(RefreshToken).where(RefreshToken.family == family)
               .where((RefreshToken.revoked_at.is_(None)) | (RefreshToken.revoked_reason == "rotated"))
               .values(revoked_at=utcnow(), revoked_reason=reason))
    db.commit()


def revoke_user(db: Session, user_id: int) -> None:
    db.execute(update(RefreshToken).where(RefreshToken.user_id == user_id)
               .where((RefreshToken.revoked_at.is_(None)) | (RefreshToken.revoked_reason == "rotated"))
               .values(revoked_at=utcnow(), revoked_reason="account"))
    db.commit()
