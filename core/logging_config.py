import json
import logging
import os
import re
from typing import Any
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_KEY_VALUE = re.compile(
    r"(?ix)((?:\"|\')?"
    r"(?:access_token|access_key|api[_-]?key|client[_-]?secret|refresh[_-]?token|auth[_-]?token|"
    r"session[_-]?id|password|passwd|passphrase|secret|token|proxy[_-]?password|"
    r"private[_-]?key|ssh[_-]?private[_-]?key|aws[_-]?access[_-]?key|aws[_-]?secret[_-]?key|"
    r"aws_access_key|credentials|basic[_-]?auth|smtp_password|youtube_client_secret|telegram_bot_token|"
    r"external_ai_api_key|license_key|ssh_private_key|secret_store_passphrase)"
    r"(?:\"|\')?\s*[:=]\s*)"
    # A quoted value is consumed whole; an unquoted value may itself contain
    # separators such as the colon in a Telegram bot token (``123456:AAH...``).
    # Space is included for cases like ``basic_auth=Basic <base64>``.
    r"(?:\"[^\"]*\"|'[^']*'|[A-Za-z0-9._\-+/=:\s]{4,})",
)
# Match quoted JSON keys with secret-like names to redact their values.
# Accepts both ordinary JSON strings and their escaped-quote form used by
# serialized payloads in logs, transport layers, and telemetry.
_JSON_SECRET_KEY = re.compile(
    r'''(?ix)
    (?P<prefix>(?:\\?['\"])
      (?:access_token|access_key|api[_-]?key|client[_-]?secret|refresh[_-]?token|auth[_-]?token|
         session[_-]?id|password|passwd|passphrase|secret|token|authorization|
         proxy[_-]?password|private_key|ssh_private_key|aws_[a-z_]*|aws_access_key|credentials|
         telegram_bot_token|youtube_client_secret|smtp_password|secret_store_passphrase)
      (?:\\?['\"])\s*:\s*)
    (?:
      (?P<quote>\\?['\"])(?P<quoted>[^'\"\\]*(?:\\.[^'\"\\]*)*)(?P=quote)
      |
      (?P<number>\d+)
    )
    '''
)
# Match secret-like keys for extra field redaction
_SECRET_KEY_NAMES = {"access_token", "access_key", "api_key", "api-key", "client_secret", "client-secret",
                   "refresh_token", "refresh-token", "auth_token", "auth-token",
                   "session_id", "session-id", "password", "passwd", "passphrase",
                   "secret", "token", "authorization", "proxy_password", "proxy-password",
                   "private_key", "ssh_private_key", "aws_key", "aws_access_key",
                   "credentials", "basic_auth"}
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
    text = re.sub(r"(?i)(Authorization\s*:\s*Basic\s+)([A-Za-z0-9._\-+/=]+)",
                  r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(basic[_-]?auth\s*[:=]\s*)((?:Basic\s+)?[A-Za-z0-9._\-+/=]+)",
                  r"\1[REDACTED]", text)
    text = _JSON_SECRET_KEY.sub(_redact_json_value, text)
    text = _SECRET_KEY_VALUE.sub(lambda m: m.group(1) + "[REDACTED]", text)
    return text


def _redact_json_value(match: re.Match) -> str:
    """Redact a JSON key-value pair, handling both string and non-string values."""
    prefix = match.group("prefix")
    if match.groupdict().get("number") is not None:
        return prefix + "[REDACTED]"
    quote = match.group("quote") or '"'
    return prefix + quote + "[REDACTED]" + quote


class JsonFormatter(logging.Formatter):
    def _redact_extra_value(self, value: Any, key: str = "") -> Any:
        """Recursively redact secrets in dict/list extras, safely stringify others."""
        if isinstance(value, str):
            # If the key suggests this is a secret, redact the entire value
            if key and any(name in key.lower() for name in _SECRET_KEY_NAMES):
                return "[REDACTED]"
            # Also redact if the value itself contains secret-like patterns
            if any(name in value.lower() for name in _SECRET_KEY_NAMES):
                return "[REDACTED]"
            return secret_redact(value)
        if isinstance(value, dict):
            return {k: self._redact_extra_value(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact_extra_value(v, key) for v in value]
        # Safely stringify non-secret non-string types
        try:
            return str(value)
        except Exception:
            return "<redacted>"

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
                if isinstance(value, (str, dict, list)):
                    # Redact if key contains secret-like patterns or value contains secret pattern
                    key_lower = key.lower()
                    if any(name in key_lower for name in _SECRET_KEY_NAMES) or _SECRET_KEY_VALUE.search(
                            str(value) if not isinstance(value, str) else value):
                        payload[key] = "[REDACTED]"
                    else:
                        payload[key] = self._redact_extra_value(value)
                else:
                    payload[key] = self._redact_extra_value(value)
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
            # Redact secrets from the raw log line before returning structured data
            item = _redact_log_item(item)
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


def _redact_log_item(item: dict) -> dict:
    """Recursively redact secrets from a log record, preserving structure."""
    redacted = {}
    for key, value in item.items():
        key_lower = key.lower()
        if any(name in key_lower for name in _SECRET_KEY_NAMES):
            redacted[key] = "[REDACTED]"
        elif isinstance(value, str):
            redacted[key] = secret_redact(value)
        elif isinstance(value, dict):
            redacted[key] = {k: (_redact_log_item(v) if isinstance(v, dict) else secret_redact(str(v)))
                            for k, v in value.items()}
        elif isinstance(value, list):
            redacted[key] = [secret_redact(str(v)) if not isinstance(v, dict) else _redact_log_item(v)
                            for v in value]
        else:
            redacted[key] = secret_redact(str(value) if not isinstance(value, dict) else _redact_log_item(value))
    return redacted
