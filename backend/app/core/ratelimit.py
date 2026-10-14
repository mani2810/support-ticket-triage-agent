"""Rate limiting.

Why custom keys: every browser reaches this API *through the Streamlit server*, so the TCP peer
is always that server's IP. Keying limits on the peer IP made them global (all customers shared one
bucket). Now:
  * authenticated requests are limited per USER (from the verified JWT);
  * anonymous requests (login, register) per END-USER IP, taken from X-Forwarded-For **only** when
    the TCP peer is a trusted proxy (TRUSTED_PROXIES) - otherwise the header is ignored (no spoofing).

The limiter is in-memory (one process). For several instances set storage_uri="redis://...".
"""
import jwt
from fastapi import Request
from slowapi import Limiter

from .config import get_settings
from .security import decode_access_token


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if peer in get_settings().trusted_proxies:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first and len(first) <= 64:
            return first
    return peer


def user_or_ip(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            return f"user:{decode_access_token(auth[7:])['sub']}"
        except jwt.PyJWTError:
            pass                                   # invalid token -> treat as anonymous
    return f"ip:{client_ip(request)}"


limiter = Limiter(key_func=user_or_ip)
