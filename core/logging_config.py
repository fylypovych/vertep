import json
import logging
import os
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_KEY_VALUE = re.compile(
    r"(?i)\b(access_token|api[_-]?key|client[_-]?secret|refresh[_-]?token|auth[_-]?token|"
    r"session[_-]?id|password|passwd|secret|token|authorization|proxy[_-]?password)\b"
    r"\s*[:=]\s*\"?[A-Za-z0-9._\-+/=]{4,}\"?",
)
_BEARER_TOKEN = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-+/=]{4,}")


def secret_redact(text: str) -> str:
    """Replace known secret-shaped values with a placeholder before logging."""
    if not text:
        return text
    text = _SECRET_KEY_VALUE.sub(lambda m: m.group(1).rstrip() + "=[REDACTED]", text)
    text = _BEARER_TOKEN.sub(lambda m: m.group(1) + "[REDACTED]", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                   "logger": record.name, "message": secret_redact(record.getMessage())}
        for key in ("job_id", "node_name", "action", "actor"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
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
