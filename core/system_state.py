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


def _database_url() -> str | None:
    if os.getenv("SYSTEM_STATE_BACKEND", "file").lower() != "postgres":
        return None
    return os.getenv("DATABASE_URL") or os.getenv("UPDATE_DATABASE_URL")


def _database_state() -> dict | None:
    if os.getenv("SYSTEM_STATE_BACKEND", "file").lower() != "postgres":
        return None
    dsn = _database_url()
    if not dsn:
        raise RuntimeError("PostgreSQL system state requires a database URL")
    import psycopg
    with psycopg.connect(dsn) as connection:
        row = connection.execute("""SELECT state,updated_at,reason,operation_id
            FROM system_operating_state WHERE singleton=TRUE""").fetchone()
    if not row:
        return None
    return {"state": SystemState(row[0]).value,
            "updated_at": row[1].isoformat() if hasattr(row[1], "isoformat") else str(row[1]),
            "reason": row[2], "operation_id": row[3]}


def _path(state_dir: Path | None = None) -> Path:
    return (state_dir or Path(os.getenv("UPDATE_STATE_DIR", "/var/lib/vertep/update"))) / "system-state.json"


def get_system_state() -> dict:
    database = _database_state()
    if database is not None:
        return database
    path = _path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        state = SystemState(value["state"])
        return {**value, "state": state.value}
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": SystemState.NORMAL.value, "updated_at": None, "reason": None,
                "operation_id": None}


def set_system_state(state: SystemState | str, reason: str, operation_id: str | None = None,
                     state_dir: Path | None = None) -> dict:
    state = SystemState(state)
    value = {"state": state.value, "updated_at": datetime.now(timezone.utc).isoformat(),
             "reason": reason, "operation_id": operation_id}
    dsn = _database_url()
    if dsn:
        import psycopg
        with psycopg.connect(dsn) as connection:
            connection.execute("""INSERT INTO system_operating_state(singleton,state,reason,operation_id,updated_at)
                VALUES(TRUE,%s,%s,%s,now()) ON CONFLICT(singleton) DO UPDATE SET
                state=excluded.state,reason=excluded.reason,operation_id=excluded.operation_id,
                updated_at=excluded.updated_at""", (state.value, reason, operation_id))
    path = _path(state_dir)
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
