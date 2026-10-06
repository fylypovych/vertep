import json
import logging
import os
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_KEY_VALUE = re.compile(
    r"(?i)((?:\"|\')?"
    r"(?:access_token|api[_-]?key|client[_-]?secret|refresh[_-]?token|auth[_-]?token|"
    r"session[_-]?id|password|passwd|secret|token|authorization|proxy[_-]?password)"
    r"(?:\"|\')?\s*[:=]\s*)"
    # A quoted value is consumed whole; an unquoted value may itself contain
    # separators such as the colon in a Telegram bot token (``123456:AAH...``).
    r"(?!(?:bearer\b|bearer\s+))"
    r"(?:\"[^\"]*\"|'[^']*'|[A-Za-z0-9._\-+/=:]{4,})",
)
# Match quoted JSON keys with secret-like names to redact their values
_JSON_SECRET_KEY = re.compile(
    r"(?i)(?:\"|\')(access_token|api[_-]?key|client[_-]?secret|refresh[_-]?token|"
    r"auth[_-]?token|session[_-]?id|password|passwd|secret|token|authorization|"
    r"proxy[_-]?password)(?:\"|\')?\s*:\s*(?:\"[^\"]*\"|'[^']*')",
)
# Match secret-like keys for extra field redaction
_SECRET_KEY_NAMES = {"access_token", "api_key", "api-key", "client_secret", "client-secret",
                   "refresh_token", "refresh-token", "auth_token", "auth-token",
                   "session_id", "session-id", "password", "passwd", "secret",
                   "token", "authorization", "proxy_password", "proxy-password"}
_BEARER_TOKEN = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-+/=]{4,}")
# Credentials embedded in a URL userinfo part (``https://user:pass@host``) are a
# real leak vector: proxy/registry/Ollama endpoints are routinely built from env
# values and end up in transport error text.
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/@\s:]+):([^/@\s]+)@")
# ``key=secret`` inside a query string, where the key is preceded by ``?&``.
_QUERY_CREDENTIAL = re.compile(
    r"(?i)((?:\?|&)(?:access_token|api[_-]?key|token|auth|key|password|secret)=)([^&#\s]+)")


def secret_redact(text: str) -> str:
    """Replace known secret-shaped values with a placeholder before logging.

    Handles plain ``key=value``, JSON ``"key": "value"``, ``bearer <token>``,
    URL userinfo credentials (``https://user:pass@host``) and query-string
    credentials (``?token=...``) forms, including quoted keys, so a serialized
    payload such as ``{"token": "abc123"}`` is redacted in addition to plain
    ``token=abc123``.  Only the value is masked; the key and surrounding
    punctuation are preserved.
    """
    if not text:
        return text
    text = _BEARER_TOKEN.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _URL_USERINFO.sub(lambda m: m.group(1) + m.group(2) + ":[REDACTED]@", text)
    text = _QUERY_CREDENTIAL.sub(lambda m: m.group(1) + "[REDACTED]", text)
    # Redact JSON secret key-value pairs first
    text = _JSON_SECRET_KEY.sub(lambda m: f'"{m.group(1)}": "[REDACTED]"', text)
    text = _SECRET_KEY_VALUE.sub(lambda m: m.group(1) + "[REDACTED]", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                   "logger": record.name, "message": secret_redact(record.getMessage())}
        for key in ("job_id", "node_name", "action", "actor"):
            if hasattr(record, key):
                payload[key] = secret_redact(str(getattr(record, key)))
        # Only include string extra fields (skip methods/functions)
        for key, value in record.__dict__.items():
            if key not in ("name", "msg", "args", "levelname", "levelno", "pathname", "filename",
                          "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
                          "created", "msecs", "relativeCreated", "thread", "threadName",
                          "processName", "process", "message", "asctime", "timestamp", "level",
                          "logger", "job_id", "node_name", "action", "actor"):
                if isinstance(value, str):
                    # Redact if key contains secret-like patterns or value contains secret pattern
                    key_lower = key.lower()
                    if any(name in key_lower for name in _SECRET_KEY_NAMES) or _SECRET_KEY_VALUE.search(value):
                        payload[key] = "[REDACTED]"
                    else:
                        payload[key] = secret_redact(value)
        if record.exc_info:
            payload["exception"] = secret_redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(service: str) -> logging.Logger:
    root = Path(os.getenv("LOG_ROOT", "logs"))
    root.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(service)
    if not logger.handlers:
        handler = RotatingFileHandler(root / f"{service}.jsonl", maxBytes=int(os.getenv("LOG_MAX_BYTES", "5242880")),
                                      backupCount=int(os.getenv("LOG_BACKUPS", "5")), encoding="utf-8")
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    return logger


def _rotation_order(path: Path) -> tuple[int, str]:
    """Order rotated log files newest-first: base ``.jsonl`` (0), ``.jsonl.1`` (1), ..."""
    name = path.name
    if name.endswith(".jsonl"):
        return (0, name)
    if ".jsonl." in name:
        suffix = name.split(".jsonl.", 1)[1]
        try:
            return (int(suffix), name)
        except ValueError:
            return (10_000, name)
    return (10_000, name)


def read_logs(limit: int = 200, level: str | None = None, job_id: str | None = None,
              node_name: str | None = None, before: str | None = None) -> list[dict]:
    """Return structured log records newest-first.

    Reads both the active ``*.jsonl`` files and their size-rotated backups
    (``*.jsonl.1``, ``*.jsonl.2``, ...) so older relevant records stay
    retrievable.  Records are filtered *before* the tail is taken, so a
    job/node/level filter never silently drops older matching entries.
    ``before`` (ISO timestamp) pages to records strictly older than it.
    """
    root = Path(os.getenv("LOG_ROOT", "logs"))
    files = sorted(root.glob("*.jsonl*"), key=_rotation_order)
    result = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if level and item.get("level") != level.upper():
                continue
            if job_id and item.get("job_id") != job_id:
                continue
            if node_name and item.get("node_name") != node_name:
                continue
            timestamp = item.get("timestamp", "")
            if before and timestamp and timestamp >= before:
                continue
            result.append(item)
    result.sort(key=lambda item: item.get("timestamp", ""), reverse=True)
    return result[:min(max(limit, 1), 1000)]
