"""Durable operation lifecycle for Telegram system operations.

Tracks long-running system operations (Status, Update, Restart, Backup,
Restore, Test) with a persisted contract so that progress survives process
restarts.  Telegram is only the control surface; the actual operation work is
delegated to the existing backend services (update agent, backup service,
health checks, node control).

State is stored file-first (UPDATE_STATE_DIR) and mirrored to PostgreSQL when
SYSTEM_STATE_BACKEND=postgres, mirroring the pattern used by
``core/system_state.py``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .atomic_write import atomic_write_json


class OperationStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ROLLING_BACK = "ROLLING_BACK"
    RECOVERING = "RECOVERING"


_OPERATION_TYPES = frozenset({"status", "update", "restart", "backup", "restore", "test"})
_ACTIVE_STATUSES = {OperationStatus.QUEUED, OperationStatus.RUNNING,
                    OperationStatus.ROLLING_BACK, OperationStatus.RECOVERING}

_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_dir() -> Path:
    return Path(os.getenv("UPDATE_STATE_DIR", "/var/lib/vertep/update"))


def _path() -> Path:
    return _state_dir() / "operations.json"


def _database_url() -> str | None:
    if os.getenv("SYSTEM_STATE_BACKEND", "file").lower() != "postgres":
        return None
    return os.getenv("DATABASE_URL") or os.getenv("UPDATE_DATABASE_URL")


def _database_operations() -> dict[str, dict] | None:
    dsn = _database_url()
    if not dsn:
        return None
    try:
        import psycopg
        with psycopg.connect(dsn) as connection:
            rows = connection.execute(
                "SELECT payload FROM system_operations ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
        return {row[0]["operation_id"]: row[0] for row in rows}
    except Exception:
        return None


def _read_all() -> dict[str, dict]:
    """Return the operations map from the file backend (empty on failure)."""
    try:
        value = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, ValueError):
        pass
    return {}


def _write_all(operations: dict[str, dict]) -> None:
    """Atomically persist the entire operations map."""
    _state_dir().mkdir(parents=True, exist_ok=True)
    atomic_write_json(_path(), operations)


def _sync_to_database(operation: dict[str, Any]) -> None:
    dsn = _database_url()
    if not dsn:
        return
    try:
        import psycopg
        with psycopg.connect(dsn) as connection:
            connection.execute(
                """INSERT INTO system_operations(operation_id, type, payload, created_at, updated_at)
                   VALUES(%s, %s, %s::jsonb, %s, %s)
                   ON CONFLICT(operation_id) DO UPDATE SET
                       payload=excluded.payload, updated_at=excluded.updated_at""",
                (operation["operation_id"], operation["type"],
                 json.dumps(operation, ensure_ascii=False),
                 operation["created_at"], operation["updated_at"])
            )
    except Exception:
        pass


def _sync_delete_to_database(operation_id: str) -> None:
    dsn = _database_url()
    if not dsn:
        return
    try:
        import psycopg
        with psycopg.connect(dsn) as connection:
            connection.execute("DELETE FROM system_operations WHERE operation_id=%s", (operation_id,))
    except Exception:
        pass


def create_operation(op_type: str, requested_by: str,
                     target: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
    """Create and persist a new system operation.

    Returns the full operation dict including a generated ``operation_id``.
    Raises ``ValueError`` if ``op_type`` is not one of the allowed types.
    """
    if op_type not in _OPERATION_TYPES:
        raise ValueError(f"Unknown operation type: {op_type}")
    now = _now()
    operation = {
        "operation_id": secrets.token_hex(16),
        "type": op_type,
        "requested_by": requested_by,
        "target": target,
        "idempotency_key": idempotency_key,
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "status": OperationStatus.QUEUED.value,
        "current_phase": None,
        "progress": 0,
        "result": None,
        "error": None,
        "updated_at": now,
    }
    with _lock:
        operations = _read_all()
        operations[operation["operation_id"]] = operation
        _write_all(operations)
        _sync_to_database(operation)
def get_or_create_operation(op_type: str, requested_by: str,
                            target: str | None = None, idempotency_key: str | None = None) -> tuple[dict[str, Any], bool]:
    """Atomically check for an active operation and create one if none exists.

    Returns (operation, created) where 'created' is True if a new operation was started.
    """
    with _lock:
        # 1. Check for existing active operation (by type+target or idempotency_key)
        operations = _read_all()
        for op in operations.values():
            if op["status"] in {s.value for s in _ACTIVE_STATUSES}:
                if idempotency_key and op.get("idempotency_key") == idempotency_key:
                    return op, False
                if op["type"] == op_type and op.get("target") == target:
                    return op, False

        # 2. Create new
        op = create_operation(op_type, requested_by, target, idempotency_key)
        return op, True


def get_operation(operation_id: str) -> dict[str, Any] | None:
    """Retrieve a single operation by ID."""
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        return None
    dsn_operations = _database_operations()
    if dsn_operations is not None:
        return dsn_operations.get(operation_id)
    with _lock:
        return _read_all().get(operation_id)


def list_operations(limit: int = 50) -> list[dict[str, Any]]:
    """Return recent operations sorted by created_at descending."""
    with _lock:
        operations = _read_all()
    sorted_ops = sorted(operations.values(),
                        key=lambda op: op.get("created_at") or "", reverse=True)
    return sorted_ops[:limit]


def _persist_update(operation: dict[str, Any]) -> None:
    operation["updated_at"] = _now()
    with _lock:
        operations = _read_all()
        existing = operations.get(operation["operation_id"])
        if existing is None:
            operations[operation["operation_id"]] = operation
        else:
            existing.update(operation)
            operations[operation["operation_id"]] = existing
        _write_all(operations)
    _sync_to_database(operation)


def begin_operation(operation_id: str, phase: str) -> dict[str, Any]:
    """Transition an operation from QUEUED to RUNNING."""
    with _lock:
        operations = _read_all()
        operation = operations.get(operation_id)
        if operation is None:
            raise KeyError(f"Operation not found: {operation_id}")
        if operation["status"] not in {OperationStatus.QUEUED.value, OperationStatus.RUNNING.value}:
            return operation
        operation["status"] = OperationStatus.RUNNING.value
        operation["started_at"] = operation.get("started_at") or _now()
        operation["current_phase"] = phase
        operations[operation_id] = operation
        _write_all(operations)
    _sync_to_database(operation)
    return operation


def advance_operation(operation_id: str, phase: str, progress: int,
                      message: str | None = None) -> dict[str, Any]:
    """Update the phase/progress of a running operation."""
    with _lock:
        operations = _read_all()
        operation = operations.get(operation_id)
        if operation is None:
            raise KeyError(f"Operation not found: {operation_id}")
        operation["current_phase"] = phase
        operation["progress"] = max(0, min(100, progress))
        if message:
            operation["error"] = message if progress < 100 else operation.get("error")
        operations[operation_id] = operation
        _write_all(operations)
    _sync_to_database(operation)
    return operation


def complete_operation(operation_id: str, result: Any = None) -> dict[str, Any]:
    """Mark an operation as COMPLETED."""
    with _lock:
        operations = _read_all()
        operation = operations.get(operation_id)
        if operation is None:
            raise KeyError(f"Operation not found: {operation_id}")
        operation["status"] = OperationStatus.COMPLETED.value
        operation["finished_at"] = _now()
        operation["progress"] = 100
        operation["result"] = result
        operations[operation_id] = operation
        _write_all(operations)
    _sync_to_database(operation)
    return operation


def fail_operation(operation_id: str, error: str) -> dict[str, Any]:
    """Mark an operation as FAILED."""
    with _lock:
        operations = _read_all()
        operation = operations.get(operation_id)
        if operation is None:
            raise KeyError(f"Operation not found: {operation_id}")
        operation["status"] = OperationStatus.FAILED.value
        operation["finished_at"] = _now()
        operation["error"] = str(error)[:2000]
        operations[operation_id] = operation
        _write_all(operations)
    _sync_to_database(operation)
    return operation


def is_operation_in_progress(op_type: str, target: str | None = None) -> dict[str, Any] | None:
    """Return an active operation of the given type if one is already running.

    Used for idempotency: if an Update operation is QUEUED/RUNNING, a second
    Update callback from Telegram must not create a duplicate.
    """
    with _lock:
        operations = _read_all()
    for operation in operations.values():
        if (operation["type"] == op_type
                and operation["status"] in {s.value for s in _ACTIVE_STATUSES}
                and operation.get("target") == target):
            return operation
    return None


def delete_operation(operation_id: str) -> bool:
    """Remove a completed/failed operation record (admin utility)."""
    with _lock:
        operations = _read_all()
        if operation_id not in operations:
            return False
        del operations[operation_id]
        _write_all(operations)
    _sync_delete_to_database(operation_id)
    return True


def audit_entry(operation_id: str, phase: str, message: str | None = None,
                actor: str | None = None) -> dict[str, Any]:
    """Append an audit line for an operation phase transition."""
    entry = {"operation_id": operation_id, "phase": phase,
             "timestamp": _now(), "message": message or "",
             "actor": actor}
    audit_path = _state_dir() / (operation_id + ".audit.jsonl")
    try:
        _state_dir().mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return entry

def add_pending_notification(chat_id: str, message: str):
    """Persist a notification to be sent after system restart."""
    path = _state_dir() / "pending_notifications.json"
    _state_dir().mkdir(parents=True, exist_ok=True)

    notifications = {}
    if path.exists():
        try:
            notifications = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    notifications[secrets.token_hex(4)] = {"chat_id": chat_id, "message": message, "timestamp": _now()}
    atomic_write_json(path, notifications)

def pop_pending_notifications() -> list[dict[str, str]]:
    """Retrieve and clear all pending notifications."""
    path = _state_dir() / "pending_notifications.json"
    if not path.exists():
        return []

    try:
        notifications = json.loads(path.read_text(encoding="utf-8"))
        _state_dir().mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, {}) # Clear immediately
        return [v for k, v in notifications.items()]
    except (OSError, ValueError):
        return []

