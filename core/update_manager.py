import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_root() -> Path:
    return Path(os.getenv("UPDATE_STATE_DIR", "/var/lib/vertep/update"))


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
        "update_available": None, "request_id": None, "log": [],
    })
    status["enabled"] = os.getenv("WEB_UPDATE_ENABLED", "false").lower() == "true"
    status["pending"] = len(list((root / "requests").glob("*.json"))) if (root / "requests").is_dir() else 0
    return status


def request_update(action: str) -> dict:
    if os.getenv("WEB_UPDATE_ENABLED", "false").lower() != "true":
        raise RuntimeError("Web updates are disabled on this node")
    if action not in {"check", "update"}:
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
        pending = {**status, "state": "PENDING", "phase": "CHECKING", "action": action, "request_id": request_id,
                   "message": f"{action.title()} request queued", "updated_at": utc_now(), "pending": 1}
        status_temporary = root / ".status.tmp"
        status_temporary.write_text(json.dumps(pending, indent=2), encoding="utf-8")
        status_temporary.replace(root / "status.json")
        # Publish the request only after PENDING is durable; the systemd path unit may react immediately.
        temporary.replace(destination)
        return pending
