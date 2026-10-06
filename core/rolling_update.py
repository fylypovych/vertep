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
    from .first_run import default_update_state_dir
    return default_update_state_dir() / "rollout.json"


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


def is_core_node(node_id: str) -> bool:
    """CORE (and its scaled replicas) are rollout targets of their own kind.

    They never appear in the worker heartbeat map, so every phase of a CORE
    target runs against the durable ``core_coordinators`` record instead of
    ``workers[node_id]``.  Treating a missing worker as an offline node made a
    workers-only lookup fail the rollout the moment CORE itself was in scope.
    """
    return node_id == "core" or node_id.startswith("core-")


def _coordinator(rollout: dict, node_id: str) -> dict:
    return rollout.setdefault("core_coordinators", {}).setdefault(
        node_id, {"node_id": node_id, "coordinator_state": "PENDING"})


def update_core_coordinator(node_id: str, updates: dict) -> dict:
    """Canonical write API for the CORE rollout coordinator record.

    The disposable coordinator (CORE-side update agent) reports drain, applied
    version and self-test results through this call; ``reconcile_rollout``
    advances the CORE node from the resulting record.
    """
    if not is_core_node(node_id):
        raise ValueError("Only a CORE node has a rollout coordinator")
    if not isinstance(updates, dict):
        raise ValueError("Coordinator updates must be a mapping")
    with _lock, _coordination_lock():
        rollout = rollout_status()
        if not rollout.get("nodes"):
            raise RuntimeError("There is no active rollout for this CORE node")
        if node_id not in {node.get("node_id") for node in rollout["nodes"]}:
            raise RuntimeError(f"Node {node_id!r} is not part of the active rollout")
        coordinator = _coordinator(rollout, node_id)
        ack = updates.get("cancel_fence_ack")
        if isinstance(ack, dict) and ack.get("cancel_fence_token"):
            coordinator["cancel_fence_ack"] = ack
        assert_not_fenced(coordinator, rollout)
        coordinator.update(updates)
        coordinator["updated_at"] = _now()
        return _write(rollout)


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
            # A cancel is not just a state flip: every agent still holding a
            # pre-cancel snapshot must be fenced out until it acknowledges this
            # token.  Clearing desired_state alone cannot stop a stale apply.
            rollout["cancel_fence_token"] = secrets.token_hex(16)
            rollout["cancelled_at"] = _now()
            return _write(rollout)
        return rollout


def _ensure_self_test_request(node: dict, rollout: dict, target_version: str | None,
                              record: dict | None = None) -> dict:
    """Issue the bound self-test request for this node's current rollout phase.

    The request carries a per-phase nonce so only a self-test run that echoes
    this exact request (operation, target version and nonce) can approve the
    phase; while the existing request matches, it is kept unchanged.
    """
    request = node.get("self_test_request")
    if (isinstance(request, dict)
            and request.get("operation_id") == rollout.get("operation_id")
            and request.get("target_version") == target_version):
        if record is not None:
            record["self_test_request"] = request
        return request
    request = {"nonce": secrets.token_hex(16),
               "operation_id": rollout.get("operation_id"),
               "target_version": target_version,
               "requested_at": _now()}
    node["self_test_request"] = request
    if record is not None:
        record["self_test_request"] = request
    return request


def _self_test_bound(test: dict, node: dict, rollout: dict) -> bool:
    """True when ``test`` echoes the node's current bound self-test request."""
    request = node.get("self_test_request")
    if request is None:
        return True
    if not isinstance(request, dict) or not request.get("nonce"):
        return False
    return (bool(test.get("nonce"))
            and test.get("nonce") == request.get("nonce")
            and test.get("operation_id") == request.get("operation_id")
            and test.get("target_version") == request.get("target_version")
            and request.get("operation_id") == rollout.get("operation_id"))


def assert_not_fenced(worker: dict, rollout: dict) -> None:
    """Refuse a mutation from an agent that never acknowledged a cancelled rollout.

    The token is delivered through ``_cleanup_cancelled_workers``; an agent that
    echoes it back as ``cancel_fence_ack`` proves it has observed the cancel
    before mutating anything.  A token merely stamped onto the record does not
    count as an acknowledgement.
    """
    if not rollout or rollout.get("state") != "CANCELLED":
        return
    token = rollout.get("cancel_fence_token")
    if not token:
        return
    tied = (worker.get("update_operation_id") == rollout.get("operation_id")
            or worker.get("cancelled_operation_id") == rollout.get("operation_id")
            or worker.get("cancel_fence_token") == token)
    if not tied:
        return
    ack = worker.get("cancel_fence_ack") or {}
    if (ack.get("cancel_fence_token") == token
            and ack.get("operation_id") == rollout.get("operation_id")):
        return
    raise RuntimeError(
        "Mutation blocked: rollout operation "
        f"{rollout.get('operation_id')} was cancelled and this agent has not "
        "acknowledged cancel_fence_token")


def _cleanup_cancelled_workers(workers: dict[str, dict], rollout: dict) -> None:
    """Clear residual update commands on workers whose rollout was cancelled."""
    if rollout.get("state") != "CANCELLED":
        return
    cancelled_ids = {node["node_id"] for node in rollout.get("nodes", [])
                     if node.get("phase") == "CANCELLED"}
    token = rollout.get("cancel_fence_token")
    for node_id in cancelled_ids:
        worker = workers.get(node_id)
        if worker is None:
            continue
        dirty = False
        for field in ("desired_state", "update_target_version", "update_operation_id",
                       "rollback_target_version"):
            if worker.pop(field, None) is not None:
                dirty = True
        if dirty:
            worker.pop("self_test_requested_at", None)
        if token:
            worker["cancel_fence_token"] = token
            worker["cancelled_operation_id"] = rollout.get("operation_id")


def _begin_rollback(rollout: dict, workers: dict[str, dict], error: str) -> dict:
    rollout.update({"state": "ROLLING_BACK", "error": error})
    target = rollout["target_version"]
    for node in rollout["nodes"]:
        worker = workers.get(node["node_id"])
        updated = node.get("phase") in {"READY", "SELF_TESTING"} or (
            worker is not None and worker.get("version") == target)
        if updated:
            node.update({"phase": "ROLLING_BACK", "phase_started_at": _now()})
            if is_core_node(node["node_id"]):
                coordinator = _coordinator(rollout, node["node_id"])
                request = _ensure_self_test_request(
                    node, rollout, node.get("previous_version"), coordinator)
                coordinator.update({
                    "coordinator_state": "ROLLBACK",
                    "rollback_target_version": node.get("previous_version"),
                    "update_operation_id": rollout["operation_id"],
                    "self_test_request": request,
                    "self_test_requested_at": _now()})
            elif worker is not None:
                request = _ensure_self_test_request(
                    node, rollout, node.get("previous_version"), worker)
                worker.update({"desired_state": "ROLLBACK",
                               "rollback_target_version": node.get("previous_version"),
                               "update_operation_id": rollout["operation_id"],
                               "self_test_request": request,
                               "self_test_requested_at": _now()})
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


def _rollback_confirmed(rollout: dict, node: dict, record: dict) -> bool:
    """Accept a rollback only from a fresh, request-bound self-test of the rolled-back version."""
    test = record.get("self_test") or {}
    request = node.get("self_test_request")
    return (record.get("version") == node.get("previous_version")
            and test.get("status") == "PASSED"
            and str(test.get("checked_at", "")) > str(node.get("phase_started_at", ""))
            and isinstance(request, dict)
            and request.get("operation_id") == rollout.get("operation_id")
            and request.get("target_version") == node.get("previous_version")
            and _self_test_bound(test, node, rollout))


def _reconcile_core_active(rollout: dict, node: dict, workers: dict[str, dict]) -> dict:
    """Advance a CORE rollout target from its durable coordinator record.

    CORE has no heartbeat entry, so every phase reads ``core_coordinators``
    instead of ``workers[node_id]``; a missing worker must not fail the node.
    """
    coordinator = _coordinator(rollout, node["node_id"])
    if _timed_out(node, rollout.get("update_timeout_seconds", 600)):
        node.update({"phase": "FAILED", "error": f"{node['phase']} timed out"})
        return _begin_rollback(rollout, workers, node["error"])
    if node["phase"] == "DRAINING":
        coordinator.update({"coordinator_state": "DRAINING",
                            "update_operation_id": rollout["operation_id"]})
        if coordinator.get("drained"):
            node.update({"phase": "UPDATING", "phase_started_at": _now(),
                         "previous_version": coordinator.get("version")})
            coordinator.update({"coordinator_state": "UPDATING",
                                "update_target_version": rollout["target_version"]})
    elif node["phase"] == "UPDATING":
        coordinator.update({"coordinator_state": "UPDATING",
                            "update_operation_id": rollout["operation_id"],
                            "update_target_version": rollout["target_version"]})
        if coordinator.get("version") == rollout["target_version"]:
            node.update({"phase": "SELF_TESTING", "phase_started_at": _now()})
            request = _ensure_self_test_request(
                node, rollout, rollout["target_version"], coordinator)
            coordinator.update({"coordinator_state": "SELF_TESTING",
                                "self_test_requested_at": _now(),
                                "self_test_request": request})
    elif node["phase"] == "SELF_TESTING":
        test = coordinator.get("self_test") or {}
        if test.get("status") == "FAILED":
            node.update({"phase": "FAILED", "error": test.get("error", "Self-test failed")})
            return _begin_rollback(rollout, workers, node["error"])
        _ensure_self_test_request(node, rollout, rollout["target_version"], coordinator)
        bound = _self_test_bound(test, node, rollout)
        if not bound:
            coordinator["self_test_requested_at"] = _now()
        if (bound
                and test.get("status") == "PASSED"
                and coordinator.get("version") == rollout["target_version"]
                and str(test.get("checked_at", "")) > str(node.get("phase_started_at", ""))):
            node["phase"] = "READY"
            coordinator.update({"coordinator_state": "READY"})
            coordinator.pop("self_test_requested_at", None)
            if node.get("canary") and not rollout.get("canary_promoted"):
                rollout["state"] = "AWAITING_PROMOTION"
    return _write(rollout)


def reconcile_rollout(workers: dict[str, dict]) -> dict:
    """Advance durable rollout state. Safe to call after any process restart."""
    with _lock, _coordination_lock():
        rollout = rollout_status()
        # When a rollout was cancelled, clean up residual update commands on
        # affected workers so they don't continue processing stale directives.
        _cleanup_cancelled_workers(workers, rollout)
        if rollout.get("state") == "ROLLING_BACK":
            for node in rollout["nodes"]:
                if node.get("phase") != "ROLLING_BACK":
                    continue
                if is_core_node(node["node_id"]):
                    coordinator = _coordinator(rollout, node["node_id"])
                    if _timed_out(node, rollout.get("update_timeout_seconds", 600)):
                        node.update({"phase": "ROLLBACK_FAILED", "error": "Rollback timed out"})
                        rollout["state"] = "ROLLBACK_FAILED"
                        return _write(rollout)
                    coordinator.update({"coordinator_state": "ROLLBACK",
                                        "rollback_target_version": node.get("previous_version"),
                                        "update_operation_id": rollout["operation_id"]})
                    _ensure_self_test_request(
                        node, rollout, node.get("previous_version"), coordinator)
                    if _rollback_confirmed(rollout, node, coordinator):
                        node["phase"] = "ROLLED_BACK"
                        coordinator["coordinator_state"] = "ROLLED_BACK"
                        coordinator.pop("self_test_requested_at", None)
                    elif not _self_test_bound(coordinator.get("self_test") or {}, node, rollout):
                        coordinator["self_test_requested_at"] = _now()
                    continue
                worker = workers.get(node["node_id"])
                if worker is None or _timed_out(node, rollout.get("update_timeout_seconds", 600)):
                    node.update({"phase": "ROLLBACK_FAILED", "error": "Rollback timed out"})
                    rollout["state"] = "ROLLBACK_FAILED"
                    return _write(rollout)
                worker.update({"desired_state": "ROLLBACK",
                               "rollback_target_version": node.get("previous_version"),
                               "update_operation_id": rollout["operation_id"]})
                _ensure_self_test_request(node, rollout, node.get("previous_version"), worker)
                if _rollback_confirmed(rollout, node, worker):
                    node["phase"] = "ROLLED_BACK"
                    worker.pop("desired_state", None)
                    worker.pop("rollback_target_version", None)
                    worker.pop("self_test_requested_at", None)
                elif not _self_test_bound(worker.get("self_test") or {}, node, rollout):
                    worker["self_test_requested_at"] = _now()
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
        if is_core_node(active["node_id"]):
            return _reconcile_core_active(rollout, active, workers)
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
                _ensure_self_test_request(active, rollout, rollout["target_version"], worker)
                worker.update({"desired_state": "SELF_TESTING", "self_test_requested_at": _now()})
        elif active["phase"] == "SELF_TESTING":
            test = worker.get("self_test") or {}
            if test.get("status") == "FAILED":
                active.update({"phase": "FAILED", "error": test.get("error", "Self-test failed")})
                return _begin_rollback(rollout, workers, active["error"])
            # The self-test must be PASSED, the worker must be at the target
            # version, AND the test result must be newer than the phase start
            # to prevent a stale PASSED result from approving a different
            # target version.  (Issue #53, #15 — self-test binding.)
            _ensure_self_test_request(active, rollout, rollout["target_version"], worker)
            bound = _self_test_bound(test, active, rollout)
            if not bound:
                worker["self_test_requested_at"] = _now()
            phase_started = active.get("phase_started_at", "")
            checked_at = str(test.get("checked_at", ""))
            if (bound
                    and test.get("status") == "PASSED"
                    and worker.get("version") == rollout["target_version"]
                    and checked_at > phase_started):
                active["phase"] = "READY"
                worker.pop("desired_state", None)
                worker.pop("update_target_version", None)
                worker.pop("self_test_requested_at", None)
                if active.get("canary") and not rollout.get("canary_promoted"):
                    rollout["state"] = "AWAITING_PROMOTION"
        return _write(rollout)
