"""Async model pull executor with progress tracking and cancellation.

Runs Ollama model pulls in a background thread so the request/response cycle is
never blocked, and streams Ollama's NDJSON progress events into a durable
``core/operations`` record that the Web UI can poll.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from .operations import (
    advance_operation,
    audit_entry,
    begin_operation,
    complete_operation,
    fail_operation,
    get_operation,
)
from .state import executor


def _ollama_url() -> str:
    return os.getenv("OLLAMA_URL", "").rstrip("/")


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
                        advance_operation(operation_id, status,
                                          get_operation(operation_id).get("progress", 0)
                                          if get_operation(operation_id) else 0,
                                          status)
        op = get_operation(operation_id)
        if op and op.get("status") == "RUNNING":
            complete_operation(operation_id, {"model": model, "pulled": True})
            audit_entry(operation_id, "pull_completed", f"model={model}")
    except Exception as error:  # noqa: BLE001 - surface any transport failure durably
        fail_operation(operation_id, f"pull failed: {error}")
    finally:
        _CANCEL_EVENTS.pop(operation_id, None)


_CANCEL_EVENTS: dict[str, threading.Event] = {}
_LOCK = threading.Lock()


def start_pull(model: str, requested_by: str = "web") -> dict[str, Any]:
    """Create a durable operation and start the background pull thread.

    Idempotent: if a pull for the same model is already queued/running, the
    existing operation is returned instead of starting a duplicate.
    """
    from .operations import get_or_create_operation

    operation, created = get_or_create_operation(
        "model_pull", requested_by, target=model, idempotency_key=f"pull:{model}")
    if not created:
        return operation
    executor.submit(_pull_model, operation["operation_id"], model)
    return operation


def cancel_pull(operation_id: str, reason: str | None = None) -> dict[str, Any] | None:
    """Signal a running pull to stop and mark the operation CANCELLED."""
    with _LOCK:
        event = _CANCEL_EVENTS.get(operation_id)
    if event is not None:
        event.set()
    from .operations import cancel_operation
    return cancel_operation(operation_id, reason)