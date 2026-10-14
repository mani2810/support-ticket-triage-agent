"""Thin HTTP client for the TriageDesk API. The frontend never talks to the database directly."""
import logging
import os
import time
import uuid

import requests
import streamlit as st

import session_cookie

API_URL = os.getenv("API_URL", "http://127.0.0.1:8000").rstrip("/") + "/api/v1"
# Backend worst case per ticket: 2 LLM attempts x 10 s + backoff, then rules fallback.
TIMEOUT = (5, 30)        # (connect, read) seconds
GET_RETRIES = 2          # only idempotent GETs are retried; a retried POST could duplicate a ticket

log = logging.getLogger("triagedesk.frontend")
_session = requests.Session()   # connection pooling (keep-alive) for every API call


class ApiError(Exception):
    def __init__(self, status: int, detail):
        self.status = status
        if isinstance(detail, list):  # FastAPI validation errors
            detail = "; ".join(f"{'.'.join(map(str, e.get('loc', [])[1:]))}: {e.get('msg')}" for e in detail)
        super().__init__(str(detail))


def _client_headers() -> dict:
    headers = {"X-Request-ID": uuid.uuid4().hex[:12]}             # same id appears in the backend logs
    try:
        ip = st.context.ip_address                                 # the end user's browser IP
    except Exception:
        ip = None
    if ip:
        # Lets the API rate-limit / lock out per real user instead of per Streamlit server.
        headers["X-Forwarded-For"] = ip
    return headers


def _send(method: str, path: str, auth: bool, **kwargs) -> requests.Response:
    headers = {**kwargs.pop("headers", {}), **_client_headers()}
    if auth and st.session_state.get("token"):
        headers["Authorization"] = f"Bearer {st.session_state.token}"
    attempts = 1 + (GET_RETRIES if method == "GET" else 0)
    for attempt in range(1, attempts + 1):
        try:
            return _session.request(method, API_URL + path, headers=headers, timeout=TIMEOUT, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            log.warning("api %s %s failed (attempt %d/%d): %s", method, path, attempt, attempts, type(exc).__name__)
            if attempt == attempts:
                raise ApiError(503, f"Cannot reach the API at {API_URL}. Is the backend running?") from exc
            time.sleep(0.5 * attempt)
    raise ApiError(503, "unreachable")                             # pragma: no cover


def _request(method: str, path: str, auth: bool = True, raw: bool = False, **kwargs):
    r = _send(method, path, auth, **kwargs)
    if r.status_code == 401 and auth and st.session_state.get("token"):
        # Access token expired: renew silently with the refresh token, then retry once.
        if _refresh():
            r = _send(method, path, auth, **kwargs)
        if r.status_code == 401:
            logout(remote=False)
            raise ApiError(401, "Your session expired. Please log in again.")
    if r.status_code >= 500:
        log.error("api %s %s -> %s", method, path, r.status_code)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise ApiError(r.status_code, detail)
    return r.content if raw else r.json()


# ---------------------------------------------------------------- session
def _store_session(data: dict) -> None:
    st.session_state.token = data["access_token"]
    st.session_state.user = data["user"]
    if data.get("refresh_token"):
        st.session_state.refresh_token = data["refresh_token"]
        session_cookie.queue_write(data["refresh_token"])        # survives F5 / new tabs


def _refresh() -> bool:
    rt = st.session_state.get("refresh_token")
    if not rt:
        return False
    r = _send("POST", "/auth/refresh", auth=False, json={"refresh_token": rt})
    if r.status_code != 200:
        return False
    _store_session(r.json())
    return True


def restore_session() -> None:
    """On a fresh browser session (e.g. after F5), log back in from the session cookie - once."""
    if st.session_state.get("user") or st.session_state.get("_restore_tried"):
        return
    st.session_state["_restore_tried"] = True
    rt = session_cookie.read()
    if not rt:
        return
    st.session_state.refresh_token = rt
    try:
        ok = _refresh()
    except ApiError:
        ok = False
    if not ok:
        st.session_state.pop("refresh_token", None)
        session_cookie.queue_clear()                               # stale / revoked: forget it


def login(email: str, password: str) -> None:
    _store_session(_request("POST", "/auth/login", auth=False, data={"username": email, "password": password}))


def logout(remote: bool = True) -> None:
    rt = st.session_state.get("refresh_token")
    if remote and rt:
        try:
            _send("POST", "/auth/logout", auth=False, json={"refresh_token": rt})   # revoke server-side
        except ApiError:
            pass
    for k in ("token", "user", "refresh_token"):
        st.session_state.pop(k, None)
    session_cookie.queue_clear()


def current_user() -> dict | None:
    return st.session_state.get("user")


def register(name: str, email: str, password: str, phone: str | None, address: str | None) -> None:
    _store_session(_request("POST", "/auth/register", auth=False,
                            json={"name": name, "email": email, "password": password,
                                  "phone": phone or None, "address": address or None}))


# ---------------------------------------------------------------- customer (/me)
def my_orders() -> list[dict]:
    return _request("GET", "/me/orders")


def raise_ticket(order_id: int, issue_type: str, description: str, affected_items: list[str],
                 subject: str | None, images: list) -> dict:
    """Multipart upload. A POST is never retried automatically (it could create a duplicate ticket)."""
    data = {"order_id": str(order_id), "issue_type": issue_type, "description": description,
            "affected_items": affected_items}
    if subject:
        data["subject"] = subject
    files = [("images", (f.name, f.getvalue(), f.type or "application/octet-stream")) for f in images]
    return _request("POST", "/me/tickets", data=data, files=files)


def my_tickets() -> list[dict]:
    return _request("GET", "/me/tickets")


def my_ticket(reference: str) -> dict:
    return _request("GET", f"/me/tickets/{reference.strip()}")


def reply_to_support(reference: str, body: str) -> dict:
    return _request("POST", f"/me/tickets/{reference.strip()}/messages", json={"body": body})


def add_photos(reference: str, images: list) -> dict:
    files = [("images", (f.name, f.getvalue(), f.type or "application/octet-stream")) for f in images]
    return _request("POST", f"/me/tickets/{reference.strip()}/photos", files=files)


def my_attachment(reference: str, attachment_id: int) -> bytes:
    return _request("GET", f"/me/tickets/{reference}/attachments/{attachment_id}", raw=True)


# ---------------------------------------------------------------- staff
def list_tickets(**filters) -> dict:
    return _request("GET", "/tickets", params={k: v for k, v in filters.items() if v not in (None, "", False)})


def get_ticket(ticket_id: int) -> dict:
    return _request("GET", f"/tickets/{ticket_id}")


def set_status(ticket_id: int, status: str, note: str | None = None) -> dict:
    return _request("POST", f"/tickets/{ticket_id}/status", json={"status": status, "note": note or None})


def reply_to_customer(ticket_id: int, body: str, resolve: bool = False) -> dict:
    return _request("POST", f"/tickets/{ticket_id}/messages", json={"body": body, "resolve": resolve})


def add_note(ticket_id: int, note: str) -> dict:
    return _request("POST", f"/tickets/{ticket_id}/comments", json={"note": note})


def reassign(ticket_id: int, agent_id: int) -> dict:
    return _request("POST", f"/tickets/{ticket_id}/reassign", json={"agent_id": agent_id})


def attachment(ticket_id: int, attachment_id: int) -> bytes:
    return _request("GET", f"/tickets/{ticket_id}/attachments/{attachment_id}", raw=True)


def assist(ticket_id: int, regenerate: bool = False) -> dict:
    return _request("POST", f"/tickets/{ticket_id}/assist", params={"regenerate": str(regenerate).lower()})


def retriage(ticket_id: int, reclassify: bool = False) -> dict:
    return _request("POST", f"/tickets/{ticket_id}/retriage", params={"reclassify": str(reclassify).lower()})


def correct(ticket_id: int, **changes) -> dict:
    return _request("PATCH", f"/tickets/{ticket_id}", json=changes)


def stats() -> dict:
    return _request("GET", "/stats")


def users(role: str | None = None) -> list[dict]:
    return _request("GET", "/users", params={"role": role} if role else None)


def create_user(payload: dict) -> dict:
    return _request("POST", "/users", json=payload)


def update_user(user_id: int, **changes) -> dict:
    return _request("PATCH", f"/users/{user_id}", json=changes)


def ready() -> dict:
    r = _session.get(API_URL.removesuffix("/api/v1") + "/ready", timeout=(2, 3), headers=_client_headers())
    return r.json()
