"""Durable, process-safe global operating state for Vertep."""

import json
import os
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class SystemState(str, Enum):
    NORMAL = "NORMAL"
    MAINTENANCE = "MAINTENANCE"
    UPDATING = "UPDATING"
    RECOVERING = "RECOVERING"
    READ_ONLY = "READ_ONLY"
    EMERGENCY = "EMERGENCY"


_lock = threading.RLock()


def _path() -> Path:
    return Path(os.getenv("UPDATE_STATE_DIR", "/var/lib/vertep/update")) / "system-state.json"


def get_system_state() -> dict:
    path = _path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        state = SystemState(value["state"])
        return {**value, "state": state.value}
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": SystemState.NORMAL.value, "updated_at": None, "reason": None,
                "operation_id": None}


def set_system_state(state: SystemState | str, reason: str, operation_id: str | None = None) -> dict:
    state = SystemState(state)
    value = {"state": state.value, "updated_at": datetime.now(timezone.utc).isoformat(),
             "reason": reason, "operation_id": operation_id}
    path = _path()
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    return value


def dispatch_allowed() -> bool:
    return get_system_state()["state"] == SystemState.NORMAL.value
