"""Shared Telegram helpers: operation fields, wait loops, internal API calls and admin checks."""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from datetime import timezone

from core.api._internal import (
    _sync_internal_api,
    _internal_auth_headers,
)
from core.operations import (
    add_pending_notification,
    list_operations,
    set_operation_fields,
)
from core.state import logger
from core.telegram_callbacks import (
    DurableCallbackSet,
    link_operation,
)
from core.telegram_store import (
    get_admin_chat_ids,
    is_admin_chat,
)


# ---------------------------------------------------------------------------
# Local operation helpers (mirrored from core/app.py so this module is
# self-contained and does not import core.app).
# ---------------------------------------------------------------------------

def _record_operation_field(operation_id: str, **fields) -> None:
    """Persist correlation fields of an operation without breaking the flow."""
    try:
        set_operation_fields(operation_id, **fields)
    except KeyError:
        logger.warning(
            "Operation %s is not persisted; correlation fields were dropped",
            str(operation_id)[:8],
        )


def _queue_operation_notification(operation: dict, marker: str, message: str) -> None:
    """Queue a durable Telegram notification for a finished operation."""
    requested_by = str(operation.get("requested_by") or "")
    if not requested_by.startswith("telegram:"):
        return
    chat_id = requested_by.split(":", 1)[1]
    add_pending_notification(chat_id, f"{marker} {message}")


# ---------------------------------------------------------------------------
# Wait helpers (extracted from the two ``for _ in range(30)`` loops).
# ---------------------------------------------------------------------------

def _wait_for_core_healthy(previous_instance_id: str) -> bool:
    """Wait up to ~60 s for CORE to report HEALTHY with a new instance id."""
    for _ in range(30):  # up to 60 seconds
        time.sleep(2)
        health = _call_core_api("GET", "/api/health", timeout=5)
        if (health and health.get("status") == "healthy"
                and health.get("runtime_instance_id") != previous_instance_id):
            return True
    return False


def _wait_for_worker_ready(node_id: str, restart_operation_id: str) -> dict | None:
    """Wait up to ~60 s for a worker to report READY with a matching restart ack."""
    for _ in range(30):  # up to 60 seconds
        time.sleep(2)
        node = _call_core_api("GET", f"/api/nodes/{node_id}", timeout=5)
        restart_ack = ((node or {}).get("update_state") or {}).get("restart_ack") or {}
        if (node and node.get("status") == "READY"
                and restart_ack.get("operation_id") == restart_operation_id):
            return node
    return None


# ---------------------------------------------------------------------------
# Internal API helpers use the shared HTTP owner directly.
# ---------------------------------------------------------------------------















_PROCESS_STARTED_AT = datetime.now(timezone.utc)


_telegram_system_callbacks = DurableCallbackSet()


_telegram_system_restore: dict[str, dict] = {}


_ACTION_TO_OP_TYPE = {
    "sys_update": "update", "sys_update_check": "update", "sys_update_confirm": "update",
    "sys_restart": "restart", "sys_restart_core": "restart", "sys_restart_worker": "restart",
    "sys_backup": "backup", "sys_backup_confirm": "backup",
    "sys_restore": "restore", "sys_restore_confirm": "restore", "sys_restore_execute": "restore",
    "sys_test": "test", "sys_test_quick": "test", "sys_test_full": "test", "sys_test_node": "test",
}


_ABS_PATH_RE = re.compile(r"(?<![:/])(?<!\w)(?:[A-Za-z]:[\\/]|/)[^\s'\";)]*")


def _admin_chat_ids() -> list[str]:
    return get_admin_chat_ids()


def _is_admin_chat(chat_id: str) -> bool:
    return is_admin_chat(chat_id)




def _host_status_is_stale(status: dict) -> bool:
    """True when the durable update status stopped progressing.

    The host agent refreshes ``status.json`` while it works on a request.  A
    status that stayed in ``PENDING``/``RUNNING`` longer than the configured
    window belongs to a host agent that died mid-operation (Issue #174.3).
    """
    try:
        window = int(os.getenv("UPDATE_OPERATION_STALE_SECONDS", "3600"))
    except ValueError:
        window = 3600
    if window <= 0:
        return False
    updated_at = str(status.get("updated_at") or "")
    if not updated_at:
        return False
    try:
        moment = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds() > window


def _operation_started_before_process(operation: dict) -> bool:
    """True when the operation was created by an earlier CORE process."""
    created_at = str(operation.get("created_at") or "")
    if not created_at:
        return False
    try:
        moment = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment < _PROCESS_STARTED_AT








def _link_callback_to_operation(callback_id: str, action: str, payload: str,
                                chat_id: str) -> None:
    """Link a CLAIMED callback to its terminal operation for crash-replay checking.

    After the side effect runs, its operation may have reached COMPLETED/FAILED.
    Linking the callback record to that operation lets a later replay detect the
    terminal state and skip re-execution (effect→crash→persist gap, Issue #174).
    """
    op_type = _ACTION_TO_OP_TYPE.get(action)
    if not op_type:
        return
    try:
        operations = list_operations(200)
    except Exception:
        return
    target = payload or None
    requested_by = f"telegram:{chat_id}"
    terminal = {"COMPLETED", "FAILED", "CANCELLED"}
    best = None
    for op in operations:
        if (op.get("type") == op_type
                and op.get("target") == target
                and op.get("requested_by") == requested_by
                and op.get("status") in terminal):
            finished = op.get("finished_at") or ""
            if best is None or finished > best.get("finished_at", ""):
                best = op
    if best and best.get("operation_id"):
        link_operation(callback_id, best["operation_id"])






def _call_core_api(method: str, path: str, payload: dict | None = None,
                   timeout: float = 30.0) -> dict | None:
    """Call the CORE API directly (synchronous).

    Passes the ``INTERNAL_API_KEY`` so that admin-only endpoints invoked by
    Telegram handlers (restart, test, backups, node actions) authenticate
    correctly without requiring user credentials.
    """
    return _sync_internal_api(method, "CORE_API_URL", path, payload, timeout=timeout,
                              headers=_internal_auth_headers())


def _call_backup_api(method: str, path: str, payload: dict | None = None,
                     timeout: float = 120.0) -> dict | None:
    """Call the Backup Service directly (synchronous)."""
    return _sync_internal_api(method, "BACKUP_URL", path, payload, timeout=timeout)
