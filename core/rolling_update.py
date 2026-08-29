"""Durable stop-on-first-failure rolling update coordinator."""

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path


_lock = threading.RLock()


def _path() -> Path:
    return Path(os.getenv("UPDATE_STATE_DIR", "/data/config/update")) / "rollout.json"


def _write(value: dict) -> dict:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    return value


def rollout_status() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "IDLE", "nodes": []}


def start_rollout(target_version: str, node_ids: list[str], order: str = "workers-first",
                  update_timeout_seconds: int = 600) -> dict:
    if not target_version or len(target_version) > 64:
        raise ValueError("Target version is invalid")
    if order not in {"workers-first", "core-first", "custom"}:
        raise ValueError("Invalid rollout order")
    if update_timeout_seconds < 60 or update_timeout_seconds > 86400:
        raise ValueError("Update timeout must be between 60 and 86400 seconds")
    unique = sorted(set(node_ids))
    if not unique:
        raise ValueError("Rolling update requires at least one node")
    with _lock:
        current = rollout_status()
        if current.get("state") == "RUNNING":
            raise RuntimeError("A rolling update is already running")
        now = datetime.now(timezone.utc).isoformat()
        return _write({"operation_id": secrets.token_hex(16), "state": "RUNNING",
                       "target_version": target_version, "max_unavailable": 1,
                       "order": order, "update_timeout_seconds": update_timeout_seconds,
                       "created_at": now, "updated_at": now,
                       "nodes": [{"node_id": node, "phase": "PENDING"} for node in unique]})


def reconcile_rollout(workers: dict[str, dict]) -> dict:
    """Advance one node at a time; callers persist returned desired worker state."""
    with _lock:
        rollout = rollout_status()
        if rollout.get("state") != "RUNNING":
            return rollout
        target = rollout["target_version"]
        order = rollout.get("order", "workers-first")
        nodes = rollout["nodes"]
        active = next((node for node in nodes if node["phase"] in {"DRAINING", "UPDATING", "SELF_TESTING"}), None)
        if active is None:
            pending = [node for node in nodes if node["phase"] == "PENDING"]
            if not pending:
                rollout["state"] = "SUCCEEDED"
                return _write(rollout)
            if order == "core-first":
                core_nodes = [node for node in pending if node["node_id"] == "core"]
                worker_nodes = [node for node in pending if node["node_id"] != "core"]
                ordered_pending = core_nodes + worker_nodes
            else:
                core_nodes = [node for node in pending if node["node_id"] == "core"]
                worker_nodes = [node for node in pending if node["node_id"] != "core"]
                ordered_pending = worker_nodes + core_nodes
            active = ordered_pending[0] if ordered_pending else pending[0]
            active["phase"] = "DRAINING"
        worker = workers.get(active["node_id"])
        if not worker:
            active.update({"phase": "FAILED", "error": "Worker is offline"})
            rollout["state"] = "FAILED"
        elif active["phase"] == "DRAINING":
            worker["desired_state"] = "DRAINING"
            if not worker.get("current_task") and worker.get("status") != "BUSY":
                active["phase"] = "UPDATING"
                worker.update({"desired_state": "UPDATING", "update_target_version": target})
        elif active["phase"] == "UPDATING":
            worker.update({"desired_state": "UPDATING", "update_target_version": target})
            if worker.get("status") == "ERROR":
                active.update({"phase": "FAILED", "error": "Worker update failed"})
                rollout["state"] = "FAILED"
            elif worker.get("version") == target:
                active["phase"] = "SELF_TESTING"
                worker.update({"desired_state": "SELF_TESTING",
                               "self_test_requested_at": datetime.now(timezone.utc).isoformat()})
            else:
                started_at = active.get("updated_at") or rollout.get("updated_at")
                if started_at:
                    try:
                        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds()
                        if elapsed > rollout.get("update_timeout_seconds", 600):
                            active.update({"phase": "FAILED", "error": "Worker update timeout"})
                            rollout["state"] = "FAILED"
                    except (ValueError, TypeError):
                        pass
        elif active["phase"] == "SELF_TESTING":
            test = worker.get("self_test") or {}
            if test.get("status") == "FAILED":
                active.update({"phase": "FAILED", "error": test.get("error", "Self-test failed")})
                rollout["state"] = "FAILED"
            elif test.get("status") == "PASSED" and worker.get("version") == target:
                active["phase"] = "READY"
                worker.pop("desired_state", None)
                worker.pop("update_target_version", None)
        rollout["updated_at"] = datetime.now(timezone.utc).isoformat()
        return _write(rollout)
