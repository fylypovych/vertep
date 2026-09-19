"""Durable idempotency records for Telegram system callbacks."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_write import atomic_write_json
from .operations import _state_dir
from .configuration import read_json


_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path() -> Path:
    return _state_dir() / "telegram_callbacks.json"


def _read() -> dict[str, dict[str, Any]]:
    try:
        value = read_json(_path())
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def claim_callback(callback_id: str, *, actor: str, action: str,
                   target: str | None = None) -> tuple[dict[str, Any], bool]:
    """Persist a callback claim before executing its side effect."""
    if not callback_id:
        return {}, True
    with _lock:
        records = _read()
        if callback_id in records:
            return records[callback_id], False
        record = {
            "callback_id": callback_id,
            "actor": actor,
            "action": action,
            "target": target,
            "status": "CLAIMED",
            "claimed_at": _now(),
            "completed_at": None,
        }
        records[callback_id] = record
        _state_dir().mkdir(parents=True, exist_ok=True)
        atomic_write_json(_path(), records)
        return record, True


def complete_callback(callback_id: str, *, status: str = "COMPLETED") -> None:
    if not callback_id:
        return
    with _lock:
        records = _read()
        record = records.get(callback_id)
        if record is None:
            return
        record["status"] = status
        record["completed_at"] = _now()
        atomic_write_json(_path(), records)


class DurableCallbackSet:
    """Compatibility facade for the former process-local callback set."""

    def __contains__(self, callback_id: object) -> bool:
        return isinstance(callback_id, str) and callback_id in _read()

    def __len__(self) -> int:
        return len(_read())

    def add(self, callback_id: str) -> None:
        claim_callback(callback_id, actor="unknown", action="unknown")

    def clear(self) -> None:
        with _lock:
            _state_dir().mkdir(parents=True, exist_ok=True)
            atomic_write_json(_path(), {})
