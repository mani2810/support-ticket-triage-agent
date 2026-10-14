"""Logging: per-request id, text (local) or JSON (production) format, optional rotating
file, and a redaction filter so secrets never reach a log line.

    LOG_FORMAT=text  ->  2026-10-14 10:21:03 INFO  [a1b2c3] app: request method=POST path=/api/v1/... status=201
    LOG_FORMAT=json  ->  {"time": "...", "level": "INFO", "request_id": "a1b2c3", "message": "request", ...}
"""
import json
import logging
import re
import sys
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Attributes every LogRecord has; anything else was passed via `extra=` and is printed as a field.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime", "request_id"}

_SECRET_PATTERNS = [
    (re.compile(r"gsk_[A-Za-z0-9]{10,}"), "gsk_***"),                          # Groq API keys
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9\-_.=]+"), "Bearer ***"),             # JWTs in headers
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"), "***jwt***"),
    (re.compile(r"(?i)(password|api_key|secret)(['\"]?\s*[:=]\s*['\"]?)[^\s,'\"}]+"), r"\1\2***"),
]


def redact(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class ContextFilter(logging.Filter):
    """Adds the request id and scrubs secrets from the message and extra fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.msg = redact(str(record.msg))
        if record.args:
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args) \
                if isinstance(record.args, tuple) else record.args
        for key, value in list(vars(record).items()):
            if key not in _STANDARD and isinstance(value, str):
                setattr(record, key, redact(value))
        return True


def _extras(record: logging.LogRecord) -> dict:
    return {k: v for k, v in vars(record).items() if k not in _STANDARD and not k.startswith("_")}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {"time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
                "logger": record.name, "request_id": getattr(record, "request_id", "-"),
                "message": record.getMessage(), **_extras(record)}
        if record.exc_info:
            data["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(data, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = (f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} {record.levelname:<5} "
                f"[{getattr(record, 'request_id', '-')}] {record.name}: {record.getMessage()}")
        if extras := _extras(record):
            line += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            line += "\n" + redact(self.formatException(record.exc_info))
        return line


def setup_logging(level: str = "INFO", fmt: str = "text", log_file: str | None = None,
                  base_dir: Path | None = None) -> None:
    formatter = JsonFormatter() if fmt == "json" else TextFormatter()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        path = Path(log_file)
        if not path.is_absolute() and base_dir:
            path = base_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"))
    context = ContextFilter()
    for h in handlers:
        h.setFormatter(formatter)
        h.addFilter(context)
    root = logging.getLogger()
    root.handlers = handlers
    root.setLevel(level)
    logging.getLogger("uvicorn.access").disabled = True   # replaced by our request log
    for noisy in ("httpx", "httpcore", "groq._base_client", "alembic"):  # quiet internals
        logging.getLogger(noisy).setLevel(logging.WARNING)
