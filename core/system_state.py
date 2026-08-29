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
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
        # Directory fsync makes the rename durable on Linux appliance hosts.
        # Windows has neither O_DIRECTORY nor portable directory fsync
        # semantics, so local development must stop after the atomic rename.
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    return value


def dispatch_allowed() -> bool:
    return get_system_state()["state"] == SystemState.NORMAL.value


def jobs_may_be_created() -> bool:
    """Whether the public API may durably accept a new job in this state."""
    return get_system_state()["state"] not in {SystemState.READ_ONLY.value,
                                                SystemState.EMERGENCY.value}


def new_job_status() -> str:
    """Return the durable initial status without allowing non-NORMAL dispatch."""
    return "NEW" if dispatch_allowed() else "WAITING_FOR_SYSTEM"


STATE_OPERATION_POLICY = {
    SystemState.NORMAL: {"read", "create_job", "mutate_job", "node_control", "configuration", "update"},
    SystemState.MAINTENANCE: {"read", "create_job", "mutate_job", "node_control", "update"},
    SystemState.UPDATING: {"read", "create_job"},
    SystemState.RECOVERING: {"read", "create_job"},
    SystemState.READ_ONLY: {"read"},
    SystemState.EMERGENCY: {"read", "recovery"},
}


def operation_allowed(operation: str) -> bool:
    """Central policy used by API modules instead of scattered state checks."""
    state = SystemState(get_system_state()["state"])
    return operation in STATE_OPERATION_POLICY[state]
