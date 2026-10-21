import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .atomic_write import atomic_write_json
from .version import application_version

_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_root() -> Path:
    from .first_run import default_update_state_dir
    return default_update_state_dir()


def _read_json(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (OSError, ValueError):
        return default


def update_status() -> dict:
    root = state_root()
    status = _read_json(root / "status.json", {
        "state": "IDLE", "phase": "NORMAL", "action": None, "message": "No update operation has run",
        "updated_at": None, "current_version": None, "available_version": None,
        "update_available": None, "request_id": None, "progress": 0, "log": [],
    })
    status["enabled"] = os.getenv("WEB_UPDATE_ENABLED", "false").lower() == "true"
    status["pending"] = len(list((root / "requests").glob("*.json"))) if (root / "requests").is_dir() else 0
    if not status.get("current_version"):
        status["current_version"] = application_version()
    return status


def pending_request_ids() -> set[str]:
    """Identifiers of update requests still waiting for the host agent.

    A request file is removed by the host agent once it starts processing the
    request, so an identifier that is neither queued nor reported by the
    durable status belongs to a request the host no longer knows about.
    """
    root = state_root()
    requests = root / "requests"
    if not requests.is_dir():
        return set()
    return {path.stem for path in requests.glob("*.json") if path.is_file()}


def mark_request_failed(request_id: str, message: str) -> dict:
    """Move the durable update status of a lost request to a terminal state.

    The host agent writes ``status.json`` while it works on a request.  If it
    dies the status stays ``PENDING``/``RUNNING`` forever and every later
    request is refused as "already pending or running" (Issue #174.3).  The
    CORE marks such a request as failed so the failure path is terminal and
    a retry becomes possible.
    """
    root = state_root()
    status = _read_json(root / "status.json", {})
    if not status:
        status = update_status()
    if str(status.get("state", "")).upper() in {"SUCCEEDED", "FAILED", "ROLLED_BACK"}:
        return status
    failed = {**status,
              "state": "FAILED",
              "phase": "RECOVERING",
              "request_id": request_id or status.get("request_id"),
              "message": message,
              "updated_at": utc_now()}
    atomic_write_json(root / "status.json", failed)
    return failed


def request_update(action: str) -> dict:
    if os.getenv("WEB_UPDATE_ENABLED", "false").lower() != "true":
        raise RuntimeError("Web updates are disabled on this node")
    if action not in {"check", "update", "restart"}:
        raise ValueError("Unsupported update action")
    root = state_root()
    requests = root / "requests"
    with _lock:
        requests.mkdir(parents=True, exist_ok=True)
        status = update_status()
        if status.get("state") in {"PENDING", "RUNNING"} or status.get("pending", 0):
            raise FileExistsError("An update operation is already pending or running")
        request_id = uuid.uuid4().hex
        payload = {"request_id": request_id, "action": action, "requested_at": utc_now()}
        temporary = requests / f".{request_id}.tmp"
        destination = requests / f"{request_id}.json"
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        phase = "RESTARTING" if action == "restart" else "CHECKING"
        pending = {**status, "state": "PENDING", "phase": phase, "action": action,
                   "request_id": request_id, "progress": 1,
                   "message": f"{action.title()} request queued", "updated_at": utc_now(), "pending": 1}
        atomic_write_json(root / "status.json", pending)
        # Publish the request only after PENDING is durable; the systemd path unit may react immediately.
        temporary.replace(destination)
        return pending
