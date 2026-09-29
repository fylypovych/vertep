"""Async model pull executor with progress tracking and cancellation.

Runs Ollama model pulls in a background thread so the request/response cycle is
never blocked, and streams Ollama's NDJSON progress events into a durable
``core/operations`` record that the Web UI can poll.

Issue #78 extends this with a per-node placement lifecycle: a pull/delete may be
placed on a specific Text node.  CORE never pushes work to workers — the command
is stored on the worker record, delivered through the heartbeat control channel
and acknowledged through the worker-authenticated model-progress endpoint.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

from .operations import (
    advance_operation,
    audit_entry,
    begin_operation,
    cancel_operation,
    complete_operation,
    fail_operation,
    get_operation,
    get_or_create_operation,
    list_operations,
    set_operation_fields,
)
from .state import executor

_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
_MODEL_OPERATION_TYPES = {"model_pull", "model_delete"}


class PlacementError(Exception):
    """Raised when a model operation cannot be placed on the requested node."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _ollama_url() -> str:
    return os.getenv("OLLAMA_URL", "").rstrip("/")


def _heartbeat_timeout() -> int:
    try:
        return int(os.getenv("HEARTBEAT_TIMEOUT", "45"))
    except ValueError:
        return 45


# ---------------------------------------------------------------------------
# Local (CORE-node) pull
# ---------------------------------------------------------------------------


def _pull_model(operation_id: str, model: str) -> None:
    """Background worker: stream Ollama pull events into the operation record."""
    import httpx

    begin_operation(operation_id, "pull")
    audit_entry(operation_id, "pull_started", f"model={model}")
    base = _ollama_url()
    if not base:
        fail_operation(operation_id, "OLLAMA_URL is not configured")
        return
    cancel_event = threading.Event()
    _CANCEL_EVENTS[operation_id] = cancel_event
    try:
        with httpx.Client(timeout=None) as client:
            with client.stream("POST", f"{base}/api/pull",
                               json={"name": model, "stream": True}) as response:
                if response.status_code != 200:
                    body = response.text[:500]
                    fail_operation(operation_id, f"Ollama returned {response.status_code}: {body}")
                    return
                for line in response.iter_lines():
                    if cancel_event.is_set():
                        audit_entry(operation_id, "pull_canceled", f"model={model}")
                        return
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    completed = event.get("completed")
                    total = event.get("total")
                    if completed is not None and total:
                        progress = max(0, min(100, int(completed * 100 // total)))
                        advance_operation(operation_id, "pull", progress,
                                          event.get("error") or None)
                    status = event.get("status")
                    if status:
                        current = get_operation(operation_id)
                        advance_operation(operation_id, status,
                                          current.get("progress", 0) if current else 0,
                                          status)
        op = get_operation(operation_id)
        if op and op.get("status") == "RUNNING":
            complete_operation(operation_id, {"model": model, "node_name": None, "pulled": True})
            audit_entry(operation_id, "pull_completed", f"model={model}")
    except Exception as error:  # noqa: BLE001 - surface any transport failure durably
        fail_operation(operation_id, f"pull failed: {error}")
    finally:
        _CANCEL_EVENTS.pop(operation_id, None)


_CANCEL_EVENTS: dict[str, threading.Event] = {}
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Node placement helpers
# ---------------------------------------------------------------------------


def _worker_store() -> Any:
    from .state import store
    return store


def text_nodes() -> dict[str, dict]:
    """Workers that can host text models (role ``text`` or the capability)."""
    store = _worker_store()
    nodes: dict[str, dict] = {}
    for name, worker in store.workers.items():
        capabilities = worker.get("capabilities") or []
        if worker.get("role") == "text" or "text_generation" in capabilities:
            nodes[name] = worker
    return nodes


def _node_or_raise(node_name: str) -> dict:
    nodes = text_nodes()
    worker = nodes.get(node_name)
    if worker is None:
        store = _worker_store()
        worker = store.workers.get(node_name)
        if worker is None:
            raise PlacementError(404, f"Unknown node: {node_name}")
        raise PlacementError(409, f"Node {node_name} cannot host text models")
    if worker.get("desired_state") in {"DISABLED", "REVOKED"}:
        raise PlacementError(409, f"Node {node_name} is {worker.get('desired_state')}")
    status = worker.get("status")
    try:
        last_seen = datetime.fromisoformat(str(worker.get("last_seen")))
    except (TypeError, ValueError):
        last_seen = None
    if status in {"OFFLINE", "ERROR"} or last_seen is None:
        raise PlacementError(409, f"Node {node_name} is not reachable")
    if (datetime.now(timezone.utc) - last_seen).total_seconds() > _heartbeat_timeout():
        raise PlacementError(409, f"Node {node_name} heartbeat timed out")
    return worker


def _target_key(node_name: str | None, model: str) -> str:
    return model if not node_name else f"{node_name}|{model}"


def split_target(target: str | None) -> tuple[str | None, str]:
    """Split a ``model`` / ``node|model`` operation target."""
    value = str(target or "")
    if "|" in value:
        node, model = value.split("|", 1)
        return node or None, model
    return None, value


def _issue_command(node_name: str, action: str, operation_id: str, model: str) -> None:
    worker = _node_or_raise(node_name)
    worker["model_command"] = {
        "operation_id": operation_id,
        "action": action,
        "model": model,
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }
    worker.pop("model_command_ack", None)
    store = _worker_store()
    store.workers[node_name] = worker
    store.save_worker(worker)


def _clear_command(worker: dict) -> None:
    worker.pop("model_command", None)
    worker.pop("model_command_ack", None)


def invalidate_model_catalog(node_name: str) -> None:
    """Mark a node's cached model catalog as needing a refresh."""
    store = _worker_store()
    worker = store.workers.get(node_name)
    if worker is None:
        return
    worker["model_catalog_at"] = None
    store.save_worker(worker)


def pending_model_command(node_name: str) -> dict[str, Any] | None:
    """Return the undelivered model command for a node, if any."""
    store = _worker_store()
    worker = store.workers.get(node_name)
    if worker is None:
        return None
    command = worker.get("model_command")
    if not command:
        return None
    operation_id = str(command.get("operation_id") or "")
    if worker.get("model_command_ack") == operation_id:
        return None
    operation = get_operation(operation_id)
    if operation is None:
        _clear_command(worker)
        store.save_worker(worker)
        return None
    status = operation.get("status")
    if status == "COMPLETED":
        _clear_command(worker)
        store.save_worker(worker)
        return None
    if status in {"FAILED", "CANCELLED"}:
        return {"operation_id": operation_id, "action": "cancel",
                "model": command.get("model")}
    return dict(command)


def ingest_model_progress(node_name: str, report: dict[str, Any]) -> dict[str, Any] | None:
    """Apply a worker-reported model progress update and acknowledge the command.

    Returns ``{"cancel_requested": bool, "operation": dict}`` when the report
    belongs to a known model operation, otherwise ``None``.
    """
    operation_id = str(report.get("operation_id") or "")
    operation = get_operation(operation_id)
    if operation is None or operation.get("type") not in _MODEL_OPERATION_TYPES:
        return None

    store = _worker_store()
    worker = store.workers.get(node_name)
    reported_status = str(report.get("status") or "RUNNING").upper()

    if worker is not None and (worker.get("model_command") or {}).get("operation_id") == operation_id:
        worker["model_command_ack"] = operation_id
        if reported_status in {"COMPLETED", "FAILED", "CANCELLED"}:
            _clear_command(worker)
            if reported_status == "COMPLETED":
                worker["model_catalog_at"] = None
        store.save_worker(worker)

    if operation.get("status") in _TERMINAL:
        return {"cancel_requested": reported_status == "RUNNING",
                "operation": operation}

    phase = str(report.get("phase") or ("delete" if operation.get("type") == "model_delete" else "pull"))
    progress = int(report.get("progress") or 0)
    error = report.get("error")

    if operation.get("status") == "QUEUED":
        begin_operation(operation_id, phase)
    if reported_status == "COMPLETED":
        _, model = split_target(operation.get("target"))
        complete_operation(operation_id, {"model": model, "node_name": node_name,
                                          "pulled": operation.get("type") == "model_pull"})
        audit_entry(operation_id, f"{operation.get('type')}_completed",
                    f"node={node_name} model={model}")
    elif reported_status == "FAILED":
        fail_operation(operation_id, str(error or "node reported failure"))
        audit_entry(operation_id, f"{operation.get('type')}_failed",
                    f"node={node_name} error={error}")
    elif reported_status == "CANCELLED":
        cancel_operation(operation_id, reason=str(error or "cancelled on node"))
        audit_entry(operation_id, f"{operation.get('type')}_canceled", f"node={node_name}")
    else:
        advance_operation(operation_id, phase, progress, str(error) if error else None)

    operation = get_operation(operation_id) or operation
    terminal = operation.get("status") in _TERMINAL
    return {"cancel_requested": terminal and reported_status == "RUNNING",
            "operation": operation}


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def start_pull(model: str, requested_by: str = "web", node_name: str | None = None) -> dict[str, Any]:
    """Create a durable operation and start the pull locally or on a Text node.

    Idempotent: if a pull for the same ``(node, model)`` is already
    queued/running, the existing operation is returned instead of starting a
    duplicate.
    """
    node = (node_name or "").strip() or None
    if node is not None:
        _node_or_raise(node)

    operation, created = get_or_create_operation(
        "model_pull", requested_by, target=_target_key(node, model),
        idempotency_key=f"pull:{node or 'local'}:{model}")
    if not created:
        return operation
    set_operation_fields(operation["operation_id"], node_name=node, model=model)
    if node is None:
        executor.submit(_pull_model, operation["operation_id"], model)
    else:
        begin_operation(operation["operation_id"], "queued")
        audit_entry(operation["operation_id"], "placement_requested",
                    f"node={node} model={model}")
        _issue_command(node, "pull", operation["operation_id"], model)
    return get_operation(operation["operation_id"]) or operation


def start_delete(model: str, requested_by: str = "web", node_name: str = "") -> dict[str, Any]:
    """Place a model deletion on a specific Text node (async operation)."""
    node = (node_name or "").strip()
    if not node:
        raise PlacementError(422, "node is required for a node-targeted delete")
    _node_or_raise(node)
    operation, created = get_or_create_operation(
        "model_delete", requested_by, target=_target_key(node, model),
        idempotency_key=f"delete:{node}:{model}")
    if not created:
        return operation
    set_operation_fields(operation["operation_id"], node_name=node, model=model)
    begin_operation(operation["operation_id"], "queued")
    audit_entry(operation["operation_id"], "placement_requested",
                f"node={node} model={model}")
    _issue_command(node, "delete", operation["operation_id"], model)
    return get_operation(operation["operation_id"]) or operation


def cancel_pull(operation_id: str, reason: str | None = None) -> dict[str, Any] | None:
    """Signal a running pull to stop and mark the operation CANCELLED."""
    with _LOCK:
        event = _CANCEL_EVENTS.get(operation_id)
    if event is not None:
        event.set()
    operation = get_operation(operation_id)
    if operation is None:
        return None
    if operation.get("type") in _MODEL_OPERATION_TYPES and operation.get("node_name"):
        node = operation.get("node_name")
        store = _worker_store()
        worker = store.workers.get(node)
        if worker is not None and (worker.get("model_command") or {}).get("operation_id") == operation_id:
            worker["model_command"] = {**worker["model_command"], "action": "cancel"}
            store.save_worker(worker)
    return cancel_operation(operation_id, reason)


def reconcile_model_pull_operations() -> int:
    """Fail model operations orphaned by a CORE restart.

    A local pull runs on an in-process thread that does not survive a restart.
    A node-placed operation survives only while its command is still pending
    delivery to an online node that has not yet acknowledged it.
    """
    recovered = 0
    for operation in list_operations(200):
        if operation.get("type") not in _MODEL_OPERATION_TYPES:
            continue
        if operation.get("status") not in {"QUEUED", "RUNNING"}:
            continue
        node, _model = split_target(operation.get("target"))
        if node:
            worker = _worker_store().workers.get(node)
            if worker is not None:
                command = worker.get("model_command") or {}
                if (command.get("operation_id") == operation["operation_id"]
                        and worker.get("model_command_ack") != operation["operation_id"]):
                    continue
        fail_operation(operation["operation_id"],
                       "Interrupted by CORE restart — start the operation again")
        audit_entry(operation["operation_id"], "reconciled_after_restart",
                    f"target={operation.get('target')}")
        recovered += 1
    return recovered


def model_placement() -> list[dict[str, Any]]:
    """Per-node model placement/readiness view derived from cached catalogs."""
    from .node_registry import registered_nodes

    timeout = _heartbeat_timeout()
    now = datetime.now(timezone.utc)
    registry = {node.get("node_id") or node.get("node_name"): node
                for node in registered_nodes()}
    entries: list[dict[str, Any]] = []
    for name, worker in sorted(text_nodes().items()):
        record = registry.get(name) or registry.get(worker.get("node_id")) or {}
        if record.get("revoked_at"):
            continue
        try:
            last_seen = datetime.fromisoformat(str(worker.get("last_seen")))
        except (TypeError, ValueError):
            last_seen = None
        age = int((now - last_seen).total_seconds()) if last_seen else None
        catalog = list(worker.get("model_catalog") or [])
        catalog_at = worker.get("model_catalog_at")
        desired = worker.get("desired_state")
        offline = (age is None or age > timeout or worker.get("status") in {"OFFLINE", "ERROR"})
        disabled = desired in {"DISABLED", "REVOKED"}
        stale = offline or disabled or not catalog_at
        entries.append({
            "node_name": name,
            "role": worker.get("role"),
            "status": "OFFLINE" if offline else worker.get("status"),
            "desired_state": desired,
            "models": catalog,
            "model_count": len(catalog),
            "catalog_at": catalog_at,
            "catalog_age_seconds": age,
            "stale": stale,
            "ready": (not stale) and not disabled,
            "pending_command": bool(worker.get("model_command")),
        })
    return entries
