"""Durable, one-node-at-a-time rollout with promotion and automatic rollback."""

import json
import os
import secrets
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.RLock()
_ACTIVE_PHASES = {"DRAINING", "UPDATING", "SELF_TESTING"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path() -> Path:
    return Path(os.getenv("UPDATE_STATE_DIR", "/data/config/update")) / "rollout.json"


def _database_url() -> str | None:
    if os.getenv("SYSTEM_STATE_BACKEND", "file").lower() != "postgres":
        return None
    return os.getenv("DATABASE_URL") or os.getenv("UPDATE_DATABASE_URL")


@contextmanager
def _coordination_lock():
    dsn = _database_url()
    if not dsn:
        yield
        return
    import psycopg
    connection = psycopg.connect(dsn, autocommit=True)
    try:
        connection.execute("SELECT pg_advisory_lock(%s)", (37031809926993,))
        yield
    finally:
        connection.execute("SELECT pg_advisory_unlock(%s)", (37031809926993,))
        connection.close()


def _write(value: dict) -> dict:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    value["updated_at"] = _now()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    dsn = _database_url()
    if dsn:
        import psycopg
        with psycopg.connect(dsn) as connection:
            connection.execute("""INSERT INTO rolling_update_state(singleton,payload,updated_at)
                VALUES(TRUE,%s::jsonb,now()) ON CONFLICT(singleton) DO UPDATE SET
                payload=excluded.payload,updated_at=excluded.updated_at""",
                (json.dumps(value, ensure_ascii=False),))
    return value


def rollout_status() -> dict:
    dsn = _database_url()
    if dsn:
        import psycopg
        with psycopg.connect(dsn) as connection:
            row = connection.execute(
                "SELECT payload FROM rolling_update_state WHERE singleton=TRUE").fetchone()
        if row:
            return dict(row[0])
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "IDLE", "nodes": []}


def _ordered_nodes(node_ids: list[str], order: str) -> list[str]:
    unique = list(dict.fromkeys(node_ids)) if order == "custom" else sorted(set(node_ids))
    cores = [node for node in unique if node == "core" or node.startswith("core-")]
    workers = [node for node in unique if node not in cores]
    return cores + workers if order == "core-first" else workers + cores if order == "workers-first" else unique


def start_rollout(target_version: str, node_ids: list[str], order: str = "workers-first",
                  update_timeout_seconds: int = 600, canary: bool = False) -> dict:
    if not target_version or len(target_version) > 64:
        raise ValueError("Target version is invalid")
    if order not in {"workers-first", "core-first", "custom"}:
        raise ValueError("Invalid rollout order")
    if not 60 <= update_timeout_seconds <= 86400:
        raise ValueError("Update timeout must be between 60 and 86400 seconds")
    ordered = _ordered_nodes(node_ids, order)
    if not ordered:
        raise ValueError("Rolling update requires at least one node")
    with _lock, _coordination_lock():
        if rollout_status().get("state") in {"RUNNING", "AWAITING_PROMOTION", "ROLLING_BACK"}:
            raise RuntimeError("A rolling update is already running")
        now = _now()
        nodes = [{"node_id": node, "phase": "PENDING", "canary": canary and index == 0,
                  "previous_version": None, "phase_started_at": now}
                 for index, node in enumerate(ordered)]
        return _write({"operation_id": secrets.token_hex(16), "state": "RUNNING",
                       "target_version": target_version, "max_unavailable": 1, "order": order,
                       "update_timeout_seconds": update_timeout_seconds, "canary": canary,
                       "canary_promoted": not canary, "created_at": now, "nodes": nodes})


def promote_rollout() -> dict:
    with _lock, _coordination_lock():
        rollout = rollout_status()
        if rollout.get("state") != "AWAITING_PROMOTION":
            raise RuntimeError("The rollout is not awaiting canary promotion")
        rollout.update({"state": "RUNNING", "canary_promoted": True})
        return _write(rollout)


def cancel_rollout() -> dict:
    with _lock, _coordination_lock():
        rollout = rollout_status()
        if rollout.get("state") in {"RUNNING", "AWAITING_PROMOTION", "ROLLING_BACK"}:
            for node in rollout.get("nodes", []):
                if node.get("phase") not in {"READY", "FAILED", "ROLLED_BACK"}:
                    node["phase"] = "CANCELLED"
            rollout["state"] = "CANCELLED"
            return _write(rollout)
        return rollout


def _begin_rollback(rollout: dict, workers: dict[str, dict], error: str) -> dict:
    rollout.update({"state": "ROLLING_BACK", "error": error})
    target = rollout["target_version"]
    for node in rollout["nodes"]:
        worker = workers.get(node["node_id"])
        updated = node.get("phase") in {"READY", "SELF_TESTING"} or (
            worker is not None and worker.get("version") == target)
        if updated:
            node.update({"phase": "ROLLING_BACK", "phase_started_at": _now()})
            if worker is not None:
                worker.update({"desired_state": "ROLLBACK",
                               "rollback_target_version": node.get("previous_version"),
                               "update_operation_id": rollout["operation_id"]})
        elif node.get("phase") not in {"FAILED", "CANCELLED"}:
            node["phase"] = "CANCELLED"
    if not any(node.get("phase") == "ROLLING_BACK" for node in rollout["nodes"]):
        rollout["state"] = "FAILED"
    return _write(rollout)


def rollback_ready_nodes(workers: dict[str, dict]) -> dict:
    with _lock, _coordination_lock():
        rollout = rollout_status()
        if rollout.get("state") not in {"RUNNING", "AWAITING_PROMOTION", "ROLLING_BACK"}:
            return rollout
        return _begin_rollback(rollout, workers, "Rollback requested by operator")


def _timed_out(node: dict, timeout: int) -> bool:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(node["phase_started_at"])).total_seconds() > timeout
    except (KeyError, TypeError, ValueError):
        return False


def reconcile_rollout(workers: dict[str, dict]) -> dict:
    """Advance durable rollout state. Safe to call after any process restart."""
    with _lock, _coordination_lock():
        rollout = rollout_status()
        if rollout.get("state") == "ROLLING_BACK":
            for node in rollout.get("nodes", []):
                if node.get("phase") != "ROLLING_BACK":
                    continue
                worker = workers.get(node["node_id"])
                if worker is None or _timed_out(node, rollout.get("update_timeout_seconds", 600)):
                    node.update({"phase": "ROLLBACK_FAILED", "error": "Rollback timed out"})
                    rollout["state"] = "ROLLBACK_FAILED"
                    return _write(rollout)
                worker.update({"desired_state": "ROLLBACK",
                               "rollback_target_version": node.get("previous_version"),
                               "update_operation_id": rollout["operation_id"]})
                if (worker.get("version") == node.get("previous_version")
                        and (worker.get("self_test") or {}).get("status") == "PASSED"):
                    node["phase"] = "ROLLED_BACK"
                    worker.pop("desired_state", None)
                    worker.pop("rollback_target_version", None)
            if all(node.get("phase") != "ROLLING_BACK" for node in rollout["nodes"]):
                rollout["state"] = "ROLLED_BACK"
            return _write(rollout)
        if rollout.get("state") != "RUNNING":
            return rollout

        nodes = rollout["nodes"]
        active = next((node for node in nodes if node["phase"] in _ACTIVE_PHASES), None)
        if active is None:
            pending = [node for node in nodes if node["phase"] == "PENDING"]
            if not pending:
                rollout["state"] = "SUCCEEDED"
                return _write(rollout)
            active = pending[0]
            active.update({"phase": "DRAINING", "phase_started_at": _now()})
        worker = workers.get(active["node_id"])
        if worker is None:
            active.update({"phase": "FAILED", "error": "Worker is offline"})
            return _begin_rollback(rollout, workers, active["error"])
        if _timed_out(active, rollout.get("update_timeout_seconds", 600)):
            active.update({"phase": "FAILED", "error": f"{active['phase']} timed out"})
            return _begin_rollback(rollout, workers, active["error"])
        if active["phase"] == "DRAINING":
            worker.update({"desired_state": "DRAINING", "update_operation_id": rollout["operation_id"]})
            if not worker.get("current_task") and worker.get("status") != "BUSY":
                active.update({"phase": "UPDATING", "phase_started_at": _now(),
                               "previous_version": worker.get("version")})
                worker.update({"desired_state": "UPDATING", "update_target_version": rollout["target_version"]})
        elif active["phase"] == "UPDATING":
            worker.update({"desired_state": "UPDATING", "update_target_version": rollout["target_version"],
                           "update_operation_id": rollout["operation_id"]})
            if worker.get("status") == "ERROR":
                active.update({"phase": "FAILED", "error": "Worker update failed"})
                return _begin_rollback(rollout, workers, active["error"])
            if worker.get("version") == rollout["target_version"]:
                active.update({"phase": "SELF_TESTING", "phase_started_at": _now()})
                worker.update({"desired_state": "SELF_TESTING", "self_test_requested_at": _now()})
        elif active["phase"] == "SELF_TESTING":
            test = worker.get("self_test") or {}
            if test.get("status") == "FAILED":
                active.update({"phase": "FAILED", "error": test.get("error", "Self-test failed")})
                return _begin_rollback(rollout, workers, active["error"])
            if test.get("status") == "PASSED" and worker.get("version") == rollout["target_version"]:
                active["phase"] = "READY"
                worker.pop("desired_state", None)
                worker.pop("update_target_version", None)
                if active.get("canary") and not rollout.get("canary_promoted"):
                    rollout["state"] = "AWAITING_PROMOTION"
        return _write(rollout)
