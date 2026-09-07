"""Worker heartbeat, registry-listing and health routes.

Extracted from ``core/app.py``: worker lifecycle status reflection for the Web
UI and the node registry.
"""
import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from ..first_run import config_root
from ..health_checks import health_status as _health_status
from ..models import WorkerHeartbeat, worker_transition_allowed, utc_now
from ..node_registry import node_roles, registered_nodes
from ..rolling_update import reconcile_rollout
from ..security import _valid_worker_request
from ..state import store
from ..system_state import get_system_state
from .job_helpers import _recover_stale_workers

router = APIRouter()


@router.post("/api/workers/heartbeat")
def heartbeat(payload: WorkerHeartbeat, request: Request):
    if not _valid_worker_request(payload.node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    data = payload.model_dump()
    if data["status"] in {"ONLINE", "FREE"}:
        data["status"] = "READY"
    role = node_roles().get(data["role"], {})
    allowed = set(role.get("capabilities", []))
    if data["role"] == "core":
        try:
            deployed = json.loads((config_root() / "deployment-plan.json").read_text(encoding="utf-8"))
            for additional in deployed.get("additional_roles", []):
                allowed.update(node_roles().get(additional, {}).get("capabilities", []))
        except (OSError, ValueError):
            pass
    declared = set(data.get("capabilities", [])) & allowed
    self_test = data.get("self_test") or {}
    data["capabilities"] = sorted(declared)
    data["tested_capabilities"] = (sorted(declared) if self_test.get("status") == "PASSED"
                                    and self_test.get("role") == data["role"] else [])
    if (os.getenv("REQUIRE_WORKER_SELF_TEST", "false").lower() == "true"
            and data["status"] == "READY" and not data["tested_capabilities"]):
        data["status"] = "ERROR"
    previous = next((worker for worker in store.load_workers()
                     if worker.get("node_name") == payload.node_name), None)
    if previous is None:
        previous = store.workers.get(payload.node_name)
    desired = (previous or {}).get("desired_state")
    drain_operation_id = (previous or {}).get("drain_operation_id")
    if desired == "DRAINING" and drain_operation_id and get_system_state()["state"] == "NORMAL":
        desired = None
        drain_operation_id = None
    if desired == "QUARANTINED":
        data["status"] = "QUARANTINED"
        data["desired_state"] = desired
    elif desired == "DRAINING":
        data["desired_state"] = desired
        data["drain_operation_id"] = drain_operation_id
        if data["status"] != "BUSY" and not data.get("current_task"):
            data["status"] = "DRAINING"
    if previous and previous.get("self_test_requested_at"):
        checked_at = str((data.get("self_test") or {}).get("checked_at", ""))
        if checked_at > previous["self_test_requested_at"]:
            data["self_test_requested_at"] = None
        else:
            data["self_test_requested_at"] = previous["self_test_requested_at"]
    if previous and not worker_transition_allowed(previous.get("status", "OFFLINE"), data["status"]):
        raise HTTPException(409, f"Illegal worker state transition: {previous.get('status')} -> {data['status']}")
    data["last_seen"] = utc_now()
    store.workers[payload.node_name] = data
    store.save_worker(data)
    reconcile_rollout(store.workers)
    data = store.workers[payload.node_name]
    store.save_worker(data)
    return {"accepted": True, "workers": len(store.workers),
            "desired_state": data.get("desired_state"),
            "self_test_requested_at": data.get("self_test_requested_at"),
            "update_target_version": data.get("update_target_version"),
            "rollback_target_version": data.get("rollback_target_version")}


@router.get("/api/workers")
def workers(role: str | None = None, status: str | None = None, capability: str | None = None):
    _recover_stale_workers()
    now = datetime.now(timezone.utc)
    timeout = int(os.getenv("HEARTBEAT_TIMEOUT", "45"))
    registry = {node["node_id"]: node for node in registered_nodes()}
    result = []
    for worker in store.load_workers(role=role, status=status, capability=capability):
        item = dict(worker)
        try:
            last_seen = datetime.fromisoformat(str(worker["last_seen"]))
        except (KeyError, TypeError, ValueError):
            last_seen = None
        if last_seen is None or (now - last_seen).total_seconds() > timeout:
            item["status"] = "OFFLINE"
        node_id = item.get("node_id") or item.get("node_name")
        record = registry.get(node_id, {})
        item["certificate_serial"] = record.get("certificate_serial")
        item["certificate_expires_at"] = record.get("certificate_expires_at")
        item["credential_generation"] = record.get("credential_generation")
        item["registered_at"] = record.get("registered_at")
        item["revoked_at"] = record.get("revoked_at")
        item["update_state"] = {
            "desired_state": item.pop("desired_state", None),
            "update_target_version": item.pop("update_target_version", None),
            "rollback_target_version": item.pop("rollback_target_version", None),
            "self_test_requested_at": item.pop("self_test_requested_at", None),
        }
        result.append(item)
    return result


@router.get("/api/workers/health")
def workers_health():
    _recover_stale_workers()
    now = datetime.now(timezone.utc)
    timeout = int(os.getenv("HEARTBEAT_TIMEOUT", "45"))
    result = []
    for worker in store.workers.values():
        last_seen = datetime.fromisoformat(worker["last_seen"])
        role = worker.get("role", "unknown")
        checks = {"status": "OFFLINE" if (now - last_seen).total_seconds() > timeout else worker.get("status", "UNKNOWN"),
                  "role": role, "last_seen": worker["last_seen"],
                  "gpu_name": worker.get("gpu_name"), "vram_mb": worker.get("vram_mb"),
                  "cuda_version": worker.get("cuda_version"), "capabilities": worker.get("capabilities", [])}
        if role == "gpu":
            checks["gpu_available"] = worker.get("gpu_available", False)
            checks["free_vram_mb"] = worker.get("free_vram_mb")
        result.append({"node_id": worker.get("node_id"), "node_name": worker.get("node_name"), "checks": checks})
    return {"status": _health_status({item["node_name"]: tuple(item["checks"].values())[0] for item in result}), "workers": result}