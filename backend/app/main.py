"""FastAPI application.

    cd backend && uvicorn app.main:app --reload      ->  http://127.0.0.1:8000/docs
"""
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text

from .api.routes import auth, customer, tickets, users
from .bootstrap import ensure_admin, init_db, warn_insecure_defaults
from .core.config import BACKEND_DIR, get_settings
from .core.logging import request_id_var, setup_logging
from .core.ratelimit import limiter
from .db.session import SessionLocal
from .services.llm import llm_status

settings = get_settings()
setup_logging(settings.log_level, settings.log_format, settings.log_file, base_dir=BACKEND_DIR)
log = logging.getLogger("app")

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",                 # API responses contain personal data
    "Content-Security-Policy": "frame-ancestors 'none'",
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with SessionLocal() as db:
        ensure_admin(db)
        warn_insecure_defaults(db)
    llm = llm_status()
    log.info("startup complete", extra={"env": settings.environment, "llm": llm["mode"],
                                        "model": llm.get("model", "-"), "db": settings.database_url.split(":")[0]})
    yield


app = FastAPI(
    title=settings.app_name,
    version="2.0.0",
    description=("Grocery customer support: customers raise tickets on their orders, an LLM on Groq "
                 "classifies and LangGraph routes them, agents resolve with AI assistance."),
    lifespan=lifespan,
    # Interactive docs help locally; in production they expose the full API surface (ENABLE_DOCS to override).
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                   allow_methods=["GET", "POST", "PATCH"], allow_headers=["Authorization", "Content-Type",
                                                                          "X-Request-ID"])


@app.middleware("http")
async def request_context(request: Request, call_next):
    # Accept a caller's request id only if it is safe to put in a log line (no log injection).
    incoming = request.headers.get("X-Request-ID", "")
    rid = incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex[:12]
    request_id_var.set(rid)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error")
        response = JSONResponse({"detail": "Internal server error", "request_id": rid}, status_code=500)
    response.headers["X-Request-ID"] = rid
    for name, value in _SECURITY_HEADERS.items():
        if not (name == "Cache-Control" and request.url.path in ("/docs", "/openapi.json")):
            response.headers.setdefault(name, value)
    level = logging.WARNING if response.status_code >= 500 else logging.INFO
    log.log(level, "request", extra={"method": request.method, "path": request.url.path,
                                     "status": response.status_code,
                                     "duration_ms": round((time.perf_counter() - start) * 1000, 1)})
    return response


API = "/api/v1"
for r in (auth.router, customer.router, tickets.router, users.router):
    app.include_router(r, prefix=API)


@app.get("/health", tags=["ops"])
def health():
    """Liveness: the process is up."""
    return {"status": "ok"}


@app.get("/ready", tags=["ops"])
def ready():
    """Readiness: the database is reachable; reports LLM mode and circuit state (no secrets)."""
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
    llm = llm_status()
    return {"status": "ready", "llm": llm["mode"], "llm_circuit": llm.get("breaker", "n/a")}
