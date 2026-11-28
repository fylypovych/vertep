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

import hashlib
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
from .logging_config import secret_redact as _redact


class OperationStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ROLLING_BACK = "ROLLING_BACK"
    RECOVERING = "RECOVERING"


_OPERATION_TYPES = frozenset({"install", "status", "update", "restart", "backup", "restore",
                              "test", "model_pull", "model_delete"})
_ACTIVE_STATUSES = {OperationStatus.QUEUED, OperationStatus.RUNNING,
                    OperationStatus.ROLLING_BACK, OperationStatus.RECOVERING}
_TERMINAL_STATUSES = {OperationStatus.COMPLETED.value, OperationStatus.FAILED.value,
                      OperationStatus.CANCELLED.value}
_LIFECYCLE_ACTIONS = frozenset({"update", "restart", "rollback"})

_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_dir() -> Path:
    from .first_run import default_update_state_dir
    return default_update_state_dir()


def _path(state_dir: Path | None = None) -> Path:
    return (state_dir or _state_dir()) / "operations.json"


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


def _read_all(state_dir: Path | None = None) -> dict[str, dict]:
    """Return the operations map from the file backend (empty on failure)."""
    try:
        value = json.loads(_path(state_dir).read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, ValueError):
        pass
    return {}


def _write_all(operations: dict[str, dict], state_dir: Path | None = None) -> None:
    """Atomically persist the entire operations map."""
    target_dir = state_dir or _state_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(_path(target_dir), operations)


def _deterministic_operation_id(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _iso_timestamp(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return _now()


def _config_dir_for_sync(state_dir: Path | None = None, config_dir: Path | None = None) -> Path | None:
    if config_dir is not None:
        return config_dir
    override = os.getenv("CONFIG_ROOT")
    if override:
        return Path(override)
    if state_dir is not None or os.getenv("UPDATE_STATE_DIR"):
        parent = (state_dir or _state_dir()).parent
        for marker in ("installation-manifest.json", "installation.json", "installed-versions.json"):
            if (parent / marker).is_file():
                return parent
        return None
    from .first_run import default_config_root
    return default_config_root()


def _find_matching_operation(operations: dict[str, dict], *,
                             external_request_id: str | None = None,
                             idempotency_key: str | None = None) -> dict[str, Any] | None:
    if external_request_id:
        for op in operations.values():
            if op.get("external_request_id") == external_request_id:
                return op
    if idempotency_key:
        for op in operations.values():
            if op.get("idempotency_key") == idempotency_key:
                return op
    return None


def _agent_status_to_operation_status(action: str, agent_state: str, phase: str) -> str:
    normalized = str(agent_state or "").upper()
    if normalized == "PENDING":
        return OperationStatus.QUEUED.value
    if normalized == "RUNNING":
        if action == "rollback" or str(phase or "").upper() == "RECOVERING":
            return OperationStatus.ROLLING_BACK.value
        return OperationStatus.RUNNING.value
    if normalized == "SUCCEEDED":
        return OperationStatus.COMPLETED.value
    if normalized == "ROLLED_BACK":
        return (OperationStatus.COMPLETED.value if action == "rollback"
                else OperationStatus.FAILED.value)
    if normalized == "FAILED":
        return OperationStatus.FAILED.value
    return OperationStatus.RUNNING.value


def _resolved_update_version(state: dict[str, Any]) -> str | None:
    action = str(state.get("action") or "").strip().lower()
    agent_state = str(state.get("state") or "").upper()
    current = str(state.get("current_version") or "").strip() or None
    available = str(state.get("available_version") or "").strip() or None
    target = str(state.get("target_version") or "").strip() or None
    if action == "update" and agent_state == "SUCCEEDED" and state.get("update_available") is False:
        return current or available or target
    return available or target or current


def _apply_update_operation(operations: dict[str, dict], state: dict[str, Any], *,
                            requested_by: str) -> tuple[dict[str, Any] | None, bool]:
    """Upsert one update/restart/rollback operation into ``operations``.

    Returns ``(operation, changed)`` without performing disk I/O so callers can
    batch multiple synced records into a single atomic write.
    """
    action = str(state.get("action") or "").strip().lower()
    if action not in _LIFECYCLE_ACTIONS:
        return None, False
    request_id = str(state.get("request_id") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", request_id):
        return None, False

    op_type = "restart" if action == "restart" else "update"
    idempotency_key = f"{op_type}:{request_id}"
    agent_state = str(state.get("state") or "").upper()
    phase = str(state.get("phase") or "").strip() or None
    try:
        progress = max(0, min(100, int(state.get("progress") or 0)))
    except (TypeError, ValueError):
        progress = 0
    version = _resolved_update_version(state)
    message = str(state.get("message") or "").strip() or None
    status_value = _agent_status_to_operation_status(action, agent_state, phase or "")
    is_terminal = status_value in _TERMINAL_STATUSES
    timestamp = _iso_timestamp(state.get("updated_at") or state.get("created_at"))
    created_at = _iso_timestamp(state.get("created_at") or state.get("updated_at"))

    result_payload = {
        "request_id": request_id,
        "action": action,
        "state": agent_state or None,
        "version": version,
        "current_version": state.get("current_version"),
        "available_version": state.get("available_version"),
        "message": _redact(message)[:2000] if message else None,
    }
    if agent_state == "ROLLED_BACK":
        phase = "ROLLED_BACK"
    if status_value == OperationStatus.COMPLETED.value:
        progress = 100

    existing = _find_matching_operation(
        operations, external_request_id=request_id, idempotency_key=idempotency_key,
    )
    if existing is not None:
        # Telegram operations are finalized by _reconcile_update_operations() so
        # chat notifications are never skipped; only mirror live progress here.
        if (str(existing.get("requested_by") or "").startswith("telegram:")
                and existing.get("status") not in _TERMINAL_STATUSES):
            changed = False
            if phase and existing.get("current_phase") != phase and not is_terminal:
                existing["current_phase"] = phase
                changed = True
            if not is_terminal and progress > int(existing.get("progress") or 0):
                existing["progress"] = progress
                changed = True
            if changed:
                existing["updated_at"] = _now()
            return existing, changed
        if existing.get("status") in _TERMINAL_STATUSES:
            return existing, False

        changed = False
        updates: dict[str, Any] = {
            "status": status_value,
            "current_phase": phase or existing.get("current_phase"),
            "progress": progress,
            "external_request_id": request_id,
            "external_action": action,
        }
        if version and not existing.get("target"):
            updates["target"] = version
        elif version and action == "update":
            updates["target"] = version
        if message:
            updates["message"] = _redact(message)[:2000]
        if status_value != OperationStatus.QUEUED.value and not existing.get("started_at"):
            updates["started_at"] = timestamp
        if is_terminal:
            updates["finished_at"] = timestamp
            if status_value == OperationStatus.COMPLETED.value:
                updates["result"] = result_payload
            else:
                updates["result"] = result_payload
                updates["error"] = _redact(message or agent_state or "Update failed")[:2000]
        for key, value in updates.items():
            if existing.get(key) != value:
                existing[key] = value
                changed = True
        if changed:
            existing["updated_at"] = timestamp
        return existing, changed

    operation_id = _deterministic_operation_id(idempotency_key)
    operation: dict[str, Any] = {
        "operation_id": operation_id,
        "type": op_type,
        "requested_by": requested_by,
        "target": version or action,
        "idempotency_key": idempotency_key,
        "external_request_id": request_id,
        "external_action": action,
        "created_at": created_at,
        "started_at": None if status_value == OperationStatus.QUEUED.value else created_at,
        "finished_at": timestamp if is_terminal else None,
        "status": status_value,
        "current_phase": phase,
        "progress": progress,
        "result": result_payload if is_terminal else None,
        "error": (_redact(message or agent_state or "Update failed")[:2000]
                  if status_value == OperationStatus.FAILED.value else None),
        "message": _redact(message)[:2000] if message else None,
        "updated_at": timestamp,
    }
    operations[operation_id] = operation
    return operation, True


def record_update_operation(state: dict[str, Any], *,
                            requested_by: str = "system:update-agent",
                            state_dir: Path | None = None) -> dict[str, Any] | None:
    """Idempotently record or advance a host update/restart/rollback operation."""
    with _lock:
        operations = _read_all(state_dir)
        operation, changed = _apply_update_operation(operations, state, requested_by=requested_by)
        if operation is not None and changed:
            _write_all(operations, state_dir)
            _sync_to_database(operation)
        return operation


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _audit_update_states(state_dir: Path) -> list[dict[str, Any]]:
    """Reconstruct update/restart/rollback summaries from update-agent audit.jsonl."""
    audit_path = state_dir / "audit.jsonl"
    if not audit_path.is_file():
        return []
    grouped: dict[str, dict[str, Any]] = {}
    try:
        lines = audit_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        action = str(entry.get("action") or "").strip().lower()
        request_id = str(entry.get("operation_id") or "").strip()
        if action not in _LIFECYCLE_ACTIONS or not re.fullmatch(r"[0-9a-f]{32}", request_id):
            continue
        record = grouped.setdefault(request_id, {
            "request_id": request_id,
            "action": action,
            "state": "RUNNING",
            "phase": entry.get("phase"),
            "progress": 0,
            "message": entry.get("message"),
            "created_at": entry.get("timestamp"),
            "updated_at": entry.get("timestamp"),
        })
        if entry.get("timestamp"):
            record["updated_at"] = entry["timestamp"]
        if entry.get("message"):
            record["message"] = entry["message"]
        for version_key in ("current_version", "available_version", "target_version"):
            if entry.get(version_key):
                record[version_key] = entry[version_key]
        if entry.get("phase") == "OUTCOME" and entry.get("state"):
            record["state"] = entry["state"]
            record["progress"] = 100 if entry["state"] in {"SUCCEEDED", "ROLLED_BACK"} else record["progress"]
        elif entry.get("phase"):
            record["phase"] = entry["phase"]
    return list(grouped.values())


def _has_update_for_version(operations: dict[str, dict], version: str) -> bool:
    for op in operations.values():
        if op.get("type") != "update":
            continue
        if op.get("idempotency_key") == f"update:version:{version}":
            return True
        if op.get("target") == version:
            return True
        result = op.get("result")
        if isinstance(result, dict) and (
            result.get("version") == version or result.get("available_version") == version
        ):
            return True
    return False


def sync_lifecycle_operations(state_dir: Path | None = None,
                              config_dir: Path | None = None) -> None:
    """Idempotently sync installation and host-update records into operations.json.

    Ensures ``GET /api/operations`` always reflects the appliance installation
    and every update executed by the host update agent without duplicating
    entries across reloads or CORE restarts.
    """
    effective_state_dir = state_dir or _state_dir()
    effective_config_dir = _config_dir_for_sync(effective_state_dir, config_dir)

    with _lock:
        operations = _read_all(effective_state_dir)
        changed_ops: list[dict[str, Any]] = []

        # 1. Installation record from installation-manifest.json / installation.json
        if effective_config_dir is not None:
            install_data = (
                _read_optional_json(effective_config_dir / "installation-manifest.json")
                or _read_optional_json(effective_config_dir / "installation.json")
            )
            if install_data and (
                install_data.get("installation_id")
                or (install_data.get("version") and (install_data.get("completed_at") or install_data.get("created_at")))
            ):
                version = str(install_data.get("version") or "").strip() or None
                install_id = str(
                    install_data.get("installation_id")
                    or install_data.get("manifest_sha256")
                    or f"v{version or 'unknown'}"
                )
                key = f"install:{install_id}"
                if _find_matching_operation(operations, idempotency_key=key) is None:
                    created_at = _iso_timestamp(install_data.get("created_at") or install_data.get("completed_at"))
                    finished_at = _iso_timestamp(install_data.get("completed_at") or created_at)
                    op_id = _deterministic_operation_id(key)
                    install_op = {
                        "operation_id": op_id,
                        "type": "install",
                        "requested_by": "system:installer",
                        "target": version,
                        "idempotency_key": key,
                        "created_at": created_at,
                        "started_at": created_at,
                        "finished_at": finished_at,
                        "status": OperationStatus.COMPLETED.value,
                        "current_phase": "COMPLETED",
                        "progress": 100,
                        "result": {
                            "version": version,
                            "installation_id": install_id,
                            "node_role": install_data.get("node_role") or "core",
                            "installation_name": install_data.get("installation_name"),
                        },
                        "error": None,
                        "message": f"Встановлення Vertep {version}" if version else "Встановлення Vertep",
                        "updated_at": finished_at,
                    }
                    operations[op_id] = install_op
                    changed_ops.append(install_op)

        # 2. Update-agent journal (audit.jsonl) and current status.json
        for audited in _audit_update_states(effective_state_dir):
            op, changed = _apply_update_operation(
                operations, audited, requested_by="system:update-agent",
            )
            if op is not None and changed:
                changed_ops.append(op)

        status_data = _read_optional_json(effective_state_dir / "status.json")
        if status_data:
            op, changed = _apply_update_operation(
                operations, status_data, requested_by="system:update-agent",
            )
            if op is not None and changed:
                changed_ops.append(op)

        # 3. Installed version history (config/installed-versions.json) for host
        #    installs/updates whose status.json was later overwritten by a check.
        if effective_config_dir is not None:
            history = _read_optional_json(effective_config_dir / "installed-versions.json")
            versions = history.get("versions") if isinstance(history, dict) else None
            if isinstance(versions, list):
                valid_entries = [
                    entry for entry in versions
                    if isinstance(entry, dict) and str(entry.get("version") or "").strip()
                ]
                has_install = any(op.get("type") == "install" for op in operations.values())
                for index, entry in enumerate(valid_entries):
                    ver = str(entry["version"]).strip()
                    recorded_at = _iso_timestamp(entry.get("recorded_at"))
                    if index == 0 and not has_install:
                        key = f"install:version:{ver}"
                        if _find_matching_operation(operations, idempotency_key=key) is None:
                            op_id = _deterministic_operation_id(key)
                            install_op = {
                                "operation_id": op_id,
                                "type": "install",
                                "requested_by": "system:installer",
                                "target": ver,
                                "idempotency_key": key,
                                "created_at": recorded_at,
                                "started_at": recorded_at,
                                "finished_at": recorded_at,
                                "status": OperationStatus.COMPLETED.value,
                                "current_phase": "COMPLETED",
                                "progress": 100,
                                "result": {"version": ver, "source": "installed-versions"},
                                "error": None,
                                "message": f"Встановлення Vertep {ver}",
                                "updated_at": recorded_at,
                            }
                            operations[op_id] = install_op
                            changed_ops.append(install_op)
                            has_install = True
                    elif index > 0 and not _has_update_for_version(operations, ver):
                        key = f"update:version:{ver}"
                        if _find_matching_operation(operations, idempotency_key=key) is None:
                            op_id = _deterministic_operation_id(key)
                            update_op = {
                                "operation_id": op_id,
                                "type": "update",
                                "requested_by": "system:update-agent",
                                "target": ver,
                                "idempotency_key": key,
                                "created_at": recorded_at,
                                "started_at": recorded_at,
                                "finished_at": recorded_at,
                                "status": OperationStatus.COMPLETED.value,
                                "current_phase": "NORMAL",
                                "progress": 100,
                                "result": {
                                    "version": ver,
                                    "state": "SUCCEEDED",
                                    "source": "installed-versions",
                                },
                                "error": None,
                                "message": f"Оновлення до версії {ver} завершено",
                                "updated_at": recorded_at,
                            }
                            operations[op_id] = update_op
                            changed_ops.append(update_op)

        if changed_ops:
            _write_all(operations, effective_state_dir)
            for op in changed_ops:
                _sync_to_database(op)


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
    return operation


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
        found = dsn_operations.get(operation_id)
        if found is not None:
            return found
    with _lock:
        found = _read_all().get(operation_id)
    if found is not None:
        return found
    sync_lifecycle_operations()
    dsn_operations = _database_operations()
    if dsn_operations is not None:
        found = dsn_operations.get(operation_id)
        if found is not None:
            return found
    with _lock:
        return _read_all().get(operation_id)


def list_operations(limit: int = 50) -> list[dict[str, Any]]:
    """Return recent operations sorted by created_at descending."""
    sync_lifecycle_operations()
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
    """Update the phase/progress of a running operation.

    ``message`` is informational progress text; it is stored in its own field so
    that routine progress never masquerades as an error in the Web UI or
    Telegram.  Failures are reported through :func:`fail_operation`.

    Issue i.0.0.1.69: a late progress sample must never rewrite a terminal
    record — a cancelled pull that emits one more line keeps its state.
    """
    with _lock:
        operations = _read_all()
        operation = operations.get(operation_id)
        if operation is None:
            raise KeyError(f"Operation not found: {operation_id}")
        if operation["status"] in {OperationStatus.COMPLETED.value,
                                   OperationStatus.FAILED.value,
                                   OperationStatus.CANCELLED.value}:
            return operation
        operation["current_phase"] = phase
        operation["progress"] = max(0, min(100, progress))
        if message:
            operation["message"] = str(message)[:2000]
        operations[operation_id] = operation
        _write_all(operations)
    _sync_to_database(operation)
    return operation


def set_operation_fields(operation_id: str, **fields: Any) -> dict[str, Any]:
    """Persist arbitrary metadata on an operation (e.g. target node placement)."""
    with _lock:
        operations = _read_all()
        operation = operations.get(operation_id)
        if operation is None:
            raise KeyError(f"Operation not found: {operation_id}")
        operation.update(fields)
        operation["updated_at"] = _now()
        operations[operation_id] = operation
        _write_all(operations)
    _sync_to_database(operation)
    return operation


def complete_operation(operation_id: str, result: Any = None) -> dict[str, Any]:
    """Mark an operation as COMPLETED.

    A terminal state (COMPLETED/FAILED/CANCELLED) is final: a late success
    arriving after cancellation or failure is ignored instead of rewriting it
    (Issue i.0.0.1.69).
    """
    with _lock:
        operations = _read_all()
        operation = operations.get(operation_id)
        if operation is None:
            raise KeyError(f"Operation not found: {operation_id}")
        if operation["status"] in {OperationStatus.COMPLETED.value,
                                   OperationStatus.FAILED.value,
                                   OperationStatus.CANCELLED.value}:
            return operation
        operation["status"] = OperationStatus.COMPLETED.value
        operation["finished_at"] = _now()
        operation["progress"] = 100
        operation["result"] = result
        operations[operation_id] = operation
        _write_all(operations)
    _sync_to_database(operation)
    return operation


def fail_operation(operation_id: str, error: str) -> dict[str, Any]:
    """Mark an operation as FAILED.

    A terminal state (COMPLETED/FAILED/CANCELLED) is final: a late transport
    error after cancellation must not turn CANCELLED into FAILED
    (Issue i.0.0.1.69).
    """
    with _lock:
        operations = _read_all()
        operation = operations.get(operation_id)
        if operation is None:
            raise KeyError(f"Operation not found: {operation_id}")
        if operation["status"] in {OperationStatus.COMPLETED.value,
                                   OperationStatus.FAILED.value,
                                   OperationStatus.CANCELLED.value}:
            return operation
        operation["status"] = OperationStatus.FAILED.value
        operation["finished_at"] = _now()
        operation["error"] = _redact(str(error)[:2000])  # Redact secrets in error
        operations[operation_id] = operation
        _write_all(operations)
    _sync_to_database(operation)
    return operation


def link_operation_request(operation_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """Persist the update-agent correlation before returning to Telegram."""
    with _lock:
        operations = _read_all()
        operation = operations.get(operation_id)
        if operation is None:
            raise KeyError(f"Operation not found: {operation_id}")
        operation["external_request_id"] = request.get("request_id")
        operation["external_action"] = request.get("action")
        operation["updated_at"] = _now()
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


def cancel_operation(operation_id: str, reason: str | None = None) -> dict[str, Any] | None:
    """Mark a queued/running operation as CANCELLED.

    Returns the updated operation, or None if it does not exist or is already
    in a terminal state.  The caller (executor) is responsible for stopping the
    underlying work; this only records the cancellation durably.
    """
    with _lock:
        operations = _read_all()
        operation = operations.get(operation_id)
        if operation is None:
            return None
        if operation["status"] in {OperationStatus.COMPLETED.value,
                                    OperationStatus.FAILED.value,
                                    OperationStatus.CANCELLED.value}:
            return operation
        operation["status"] = OperationStatus.CANCELLED.value
        operation["finished_at"] = _now()
        operation["error"] = (_redact(reason or "cancelled by user")[:2000]  # Redact secrets
        )
        operations[operation_id] = operation
        _write_all(operations)
    _sync_to_database(operation)
    audit_entry(operation_id, "cancel", reason or "cancelled by user")
    return operation


def audit_entry(operation_id: str, phase: str, message: str | None = None,
                actor: str | None = None, confirmation: str | None = None) -> dict[str, Any]:
    """Append an audit line for an operation phase transition."""
    operation = get_operation(operation_id)
    actor = actor or (operation or {}).get("requested_by")
    # Audit lines are an operator-visible surface, so secret-shaped text in
    # messages and error payloads must be masked before it is persisted.
    entry = {"operation_id": operation_id, "phase": phase,
             "timestamp": _now(), "message": _redact(message or ""),
             "actor": actor,
             "chat": actor.split(":", 1)[1] if actor and actor.startswith("telegram:") else None,
             "confirmation": confirmation,
             "operation_type": (operation or {}).get("type"),
             "target": (operation or {}).get("target"),
             "status": (operation or {}).get("status"),
             "result": (operation or {}).get("result"),
             "error": _redact((operation or {}).get("error"))}
    audit_path = _state_dir() / (operation_id + ".audit.jsonl")
    try:
        _state_dir().mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass
    return entry

def add_pending_notification(chat_id: str, message: str) -> str:
    """Persist a notification to be sent after system restart."""
    path = _state_dir() / "pending_notifications.json"
    _state_dir().mkdir(parents=True, exist_ok=True)

    notifications = {}
    if path.exists():
        try:
            notifications = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    if not isinstance(notifications, dict):
        notifications = {}

    notification_id = secrets.token_hex(8)
    notifications[notification_id] = {"notification_id": notification_id,
                                     "chat_id": chat_id, "message": message,
                                     "timestamp": _now()}
    atomic_write_json(path, notifications)
    return notification_id


def pending_notifications() -> list[dict[str, str]]:
    """Read delivery records without removing them."""
    path = _state_dir() / "pending_notifications.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return list(value.values()) if isinstance(value, dict) else []


def acknowledge_notification(notification_id: str) -> None:
    """Remove one notification only after Telegram accepted it."""
    path = _state_dir() / "pending_notifications.json"
    with _lock:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(value, dict) or notification_id not in value:
            return
        del value[notification_id]
        atomic_write_json(path, value)

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
