import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                   "logger": record.name, "message": record.getMessage()}
        for key in ("job_id", "node_name", "action", "actor"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
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


def read_logs(limit: int = 200, level: str | None = None, job_id: str | None = None, node_name: str | None = None) -> list[dict]:
    result = []
    for path in Path(os.getenv("LOG_ROOT", "logs")).glob("*.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
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
            result.append(item)
    return sorted(result, key=lambda item: item.get("timestamp", ""), reverse=True)[:limit]
