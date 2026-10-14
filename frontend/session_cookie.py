"""Keep the login across browser refreshes.

Streamlit forgets st.session_state on F5 (a new session starts), so the refresh token is kept in a
first-party cookie:
  * written by a tiny script in a zero-height component (SameSite=Strict, Secure on https, 7 days);
  * read server-side from the incoming request via st.context.cookies when a new session starts.

The cookie only holds a *refresh* token: it is rotated on every use, stored hashed server-side, and
revoked on logout / deactivation / reuse. (It is not HttpOnly because Streamlit cannot set response
headers; all untrusted text is markdown-escaped in this UI, which limits script-injection risk.)
"""
import json

import streamlit as st
import streamlit.components.v1 as components

COOKIE = "fc_session"
MAX_AGE = 7 * 24 * 3600


def read() -> str | None:
    try:
        value = st.context.cookies.get(COOKIE)
    except Exception:
        return None
    return value or None


def queue_write(value: str) -> None:
    """Ask app.py to write the cookie on its next render (works across st.rerun())."""
    st.session_state["_cookie_write"] = value


def queue_clear() -> None:
    st.session_state["_cookie_write"] = ""


def flush() -> None:
    """Render the pending cookie write, if any. Call once per run from app.py."""
    if "_cookie_write" not in st.session_state:
        return
    value = st.session_state.pop("_cookie_write")
    max_age = MAX_AGE if value else 0
    components.html(f"""<script>
      const doc = window.parent.document;
      const secure = window.parent.location.protocol === "https:" ? "; Secure" : "";
      doc.cookie = "{COOKIE}=" + encodeURIComponent({json.dumps(value)}) +
                   "; Max-Age={max_age}; Path=/; SameSite=Strict" + secure;
    </script>""", height=0)
