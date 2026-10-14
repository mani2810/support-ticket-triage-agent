"""Application settings, loaded from environment variables / backend/.env (12-factor style).

Paths are anchored to the backend/ folder, so the app behaves the same no matter which
directory you start it from.
"""
import logging
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
SECRETS_DIR = BACKEND_DIR / ".secrets"            # git-ignored; holds the auto-generated dev JWT secret
DEMO_ADMIN_PASSWORD = "ChangeMe!2026"

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "TriageDesk API"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # Database: SQLite for local dev, e.g. postgresql+psycopg2://user:pass@host/db in production.
    # A relative SQLite path is resolved against backend/.
    database_url: str = "sqlite:///./triage.db"

    # Auth. Leave JWT_SECRET empty locally: a random secret is generated once and kept in backend/.secrets/.
    jwt_secret: str | None = Field(None, min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "triagedesk"
    access_token_minutes: int = 60 * 8

    # First admin account, created on startup if no admin exists
    bootstrap_admin_email: str = "admin@triagedesk.dev"
    bootstrap_admin_password: str = DEMO_ADMIN_PASSWORD

    # LLM (Groq cloud: fast inference for open models; free tier is rate-limited)
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
    llm_mode: Literal["groq", "rules"] = "groq"
    llm_timeout_seconds: float = 10            # per HTTP attempt
    llm_max_attempts: int = Field(2, ge=1, le=4)  # 1 call + at most 1 retry, transient errors only
    llm_max_calls_per_minute: int = 25         # stay under Groq free-tier RPM; excess -> rules
    llm_breaker_failures: int = 3              # consecutive failures that open the circuit
    llm_breaker_reset_seconds: int = 60        # how long the circuit stays open
    llm_cache_ttl_seconds: int = 3600          # identical ticket text is never classified twice

    # Customer image uploads (stored outside the web root, metadata stripped)
    upload_dir: str = "uploads"                 # relative paths resolve against backend/
    max_upload_mb: float = 5
    max_images_per_ticket: int = 3
    max_image_pixels: int = 4096                # longest side after re-encoding

    # Logging
    log_format: Literal["json", "text"] = "text"
    log_file: str | None = None                # e.g. logs/triagedesk.log (rotated, 5 x 5 MB)

    # Login protection
    bcrypt_rounds: int = Field(12, ge=4, le=15)   # cost factor; tests use 4 for speed
    login_max_failures: int = 5
    login_lockout_minutes: int = 15

    # Sessions: refresh tokens keep users logged in across page refreshes (rotated on every use)
    refresh_token_days: int = 7

    # HTTP
    cors_origins: list[str] = ["http://localhost:8501"]
    public_rate_limit: str = "10/minute"
    # Only these peers may tell us the end-user IP via X-Forwarded-For (the Streamlit server).
    trusted_proxies: list[str] = ["127.0.0.1", "::1"]
    enable_docs: bool | None = None            # None = on, except when ENVIRONMENT=production

    # Ticket eligibility
    ticket_window_days: int = 30               # item problems must be reported within N days of delivery
    max_images_total: int = 9                  # photos per ticket, including follow-ups

    # Notifications: real e-mail when SMTP_HOST is set, otherwise .eml files in backend/outbox/
    notify_enabled: bool = True
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "FreshCart Support <support@freshcart.local>"
    outbox_dir: str = "outbox"
    app_url: str = "http://localhost:8501"

    @property
    def docs_enabled(self) -> bool:
        return self.enable_docs if self.enable_docs is not None else self.environment != "production"

    @model_validator(mode="before")
    @classmethod
    def _blank_to_none(cls, data):
        # "JWT_SECRET=" / "GROQ_API_KEY=" in .env mean "not set", not "empty string".
        if isinstance(data, dict):
            for key in ("jwt_secret", "JWT_SECRET", "groq_api_key", "GROQ_API_KEY", "log_file", "LOG_FILE",
                        "smtp_host", "SMTP_HOST", "smtp_user", "SMTP_USER", "smtp_password", "SMTP_PASSWORD",
                        "enable_docs", "ENABLE_DOCS"):
                if data.get(key) == "":
                    data[key] = None
        return data

    @model_validator(mode="after")
    def _finalise(self):
        if self.environment == "production":
            if not self.jwt_secret or self.jwt_secret.startswith("change-me"):
                raise ValueError("Set a strong JWT_SECRET (32+ chars) in production")
            if self.bootstrap_admin_password == DEMO_ADMIN_PASSWORD:
                raise ValueError("Set BOOTSTRAP_ADMIN_PASSWORD in production")
        if not self.jwt_secret or self.jwt_secret.startswith("change-me"):
            self.jwt_secret = _local_jwt_secret(self.environment)
        self.database_url = _anchor_sqlite(self.database_url)
        if not Path(self.upload_dir).is_absolute():
            self.upload_dir = str((BACKEND_DIR / self.upload_dir).resolve())
        if not Path(self.outbox_dir).is_absolute():
            self.outbox_dir = str((BACKEND_DIR / self.outbox_dir).resolve())
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


def _anchor_sqlite(url: str) -> str:
    """sqlite:///./triage.db -> sqlite:///<backend>/triage.db (absolute), so cwd does not matter."""
    prefix = "sqlite:///"
    if not url.startswith(prefix) or url == "sqlite:///:memory:":
        return url
    path = Path(url[len(prefix):])
    if not path.is_absolute():
        path = (BACKEND_DIR / path).resolve()
    return prefix + path.as_posix()


def _local_jwt_secret(environment: str) -> str:
    """Never sign tokens with a secret that is published on GitHub.

    Tests get a throwaway secret; local dev gets a random one persisted in backend/.secrets/
    so logins survive restarts (and `--reload`).
    """
    if environment == "test":
        return secrets.token_urlsafe(48)
    path = SECRETS_DIR / "jwt_secret"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    SECRETS_DIR.mkdir(exist_ok=True)
    value = secrets.token_urlsafe(48)
    path.write_text(value, encoding="utf-8")
    log.warning("Generated a local JWT secret in %s (set JWT_SECRET to override)", path)
    return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
