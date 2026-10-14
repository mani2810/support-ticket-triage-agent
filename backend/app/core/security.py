"""Password hashing (bcrypt), JWT access tokens and brute-force protection."""
import threading
import time
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from .config import get_settings

BCRYPT_MAX_BYTES = 72  # bcrypt ignores (bcrypt>=5: rejects) anything longer
# Pre-computed hash used when the email does not exist, so a login takes the same time
# whether or not the account exists (prevents user enumeration by timing).
_DUMMY_HASH = bcrypt.hashpw(b"timing-equaliser", bcrypt.gensalt(get_settings().bcrypt_rounds)).decode()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:BCRYPT_MAX_BYTES], bcrypt.gensalt(get_settings().bcrypt_rounds)).decode()


def verify_password(password: str, hashed: str | None) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:BCRYPT_MAX_BYTES], (hashed or _DUMMY_HASH).encode()) \
            and hashed is not None
    except ValueError:
        return False


def create_access_token(user_id: int, role: str) -> tuple[str, int]:
    s = get_settings()
    expires_in = s.access_token_minutes * 60
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "role": role, "iss": s.jwt_issuer, "iat": now,
               "exp": now + timedelta(seconds=expires_in)}
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm), expires_in


def decode_access_token(token: str) -> dict:
    s = get_settings()
    return jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm], issuer=s.jwt_issuer,
                      options={"require": ["exp", "iat", "sub", "iss"]})


class LoginGuard:
    """Locks an account for N minutes after M consecutive failed logins.

    In-memory (per process) - right for a single-instance deployment. With several
    instances, move this to Redis or a DB table.
    """

    def __init__(self, max_failures: int, lockout_seconds: int, clock=time.monotonic):
        self.max_failures, self.lockout_seconds, self._clock = max_failures, lockout_seconds, clock
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def locked_for(self, email: str) -> int:
        """Seconds remaining on a lockout (0 = not locked)."""
        with self._lock:
            until = self._locked_until.get(email, 0)
            remaining = until - self._clock()
            if remaining <= 0:
                self._locked_until.pop(email, None)
                return 0
            return int(remaining) + 1

    def record_failure(self, email: str) -> bool:
        """Returns True when this failure triggered a lockout."""
        with self._lock:
            now = self._clock()
            recent = [t for t in self._failures.get(email, []) if now - t < self.lockout_seconds] + [now]
            if len(recent) >= self.max_failures:
                self._locked_until[email] = now + self.lockout_seconds
                self._failures.pop(email, None)
                return True
            self._failures[email] = recent
            return False

    def record_success(self, email: str) -> None:
        with self._lock:
            self._failures.pop(email, None)


_settings = get_settings()
login_guard = LoginGuard(_settings.login_max_failures, _settings.login_lockout_minutes * 60)
