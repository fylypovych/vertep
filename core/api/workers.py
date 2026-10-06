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
from ..models import ModelProgressReport, WorkerHeartbeat, worker_transition_allowed, utc_now
from ..node_registry import node_roles, registered_nodes
from ..pull_executor import ingest_model_progress, pending_model_command
from ..rolling_update import assert_not_fenced, reconcile_rollout, rollout_status
from ..security import _valid_worker_request
from ..state import store
from ..system_state import get_system_state
from .job_helpers import _recover_stale_workers

router = APIRouter()

# Fields CORE owns on a worker record; they are not part of the heartbeat
# payload and must survive every heartbeat overwrite.
_MODEL_LIFECYCLE_FIELDS = ("model_command", "model_command_ack",
                           "model_catalog", "model_catalog_at",
                           "cancel_fence_token", "cancelled_operation_id",
                           "cancel_fence_ack", "cancel_fence_ack_at")


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
    data["runtime_status"] = ("ONLINE" if self_test.get("status") == "PASSED"
                              and self_test.get("role") == data["role"] else "PENDING_SELF_TEST")
    if (os.getenv("REQUIRE_WORKER_SELF_TEST", "false").lower() == "true"
            and data["status"] == "READY" and not data["tested_capabilities"]):
        data["status"] = "ERROR"
    previous = next((worker for worker in store.load_workers()
                     if worker.get("node_name") == payload.node_name), None)
    if previous is None:
        previous = store.workers.get(payload.node_name)
    fence_rollout = rollout_status()
    fenced = dict(previous or {})
    if payload.cancel_fence_ack:
        fenced["cancel_fence_ack"] = payload.cancel_fence_ack
    try:
        assert_not_fenced(fenced, fence_rollout)
    except RuntimeError as error:
        raise HTTPException(409, {
            "message": str(error),
            "rollout_state": fence_rollout.get("state"),
            "rollout_operation_id": fence_rollout.get("operation_id"),
            "cancel_fence_token": fence_rollout.get("cancel_fence_token"),
        }) from error
    desired = (previous or {}).get("desired_state")
    drain_operation_id = (previous or {}).get("drain_operation_id")
    restart_operation_id = (previous or {}).get("restart_operation_id")
    previous_instance_id = (previous or {}).get("runtime_instance_id")
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
    elif desired == "RESTARTING":
        current_instance_id = data.get("runtime_instance_id")
        if (current_instance_id and previous_instance_id
                and current_instance_id != previous_instance_id):
            data["restart_ack"] = {
                "operation_id": restart_operation_id,
                "previous_instance_id": previous_instance_id,
                "runtime_instance_id": current_instance_id,
                "acknowledged_at": utc_now(),
            }
            data["status"] = "READY"
        else:
            data["desired_state"] = desired
            data["restart_operation_id"] = restart_operation_id
            data["status"] = "UPDATING"
    elif desired in {"UPDATING", "ROLLBACK", "DISABLED", "REVOKED", "SELF_TESTING"}:
        data["desired_state"] = desired
    if previous and previous.get("self_test_requested_at"):
        checked_at = str((data.get("self_test") or {}).get("checked_at", ""))
        if checked_at > previous["self_test_requested_at"]:
            data["self_test_requested_at"] = None
        else:
            data["self_test_requested_at"] = previous["self_test_requested_at"]
    if previous and not worker_transition_allowed(previous.get("status", "OFFLINE"), data["status"]):
        raise HTTPException(409, f"Illegal worker state transition: {previous.get('status')} -> {data['status']}")
    for field in _MODEL_LIFECYCLE_FIELDS:
        if previous and previous.get(field) is not None:
            data[field] = previous[field]
    if payload.cancel_fence_ack:
        data["cancel_fence_ack"] = payload.cancel_fence_ack
        data["cancel_fence_ack_at"] = utc_now()
    reported_catalog = payload.model_catalog or {}
    if isinstance(reported_catalog.get("models"), list):
        data["model_catalog"] = [str(name) for name in reported_catalog["models"]]
        data["model_catalog_at"] = utc_now()
    if data.get("desired_state") in {"DISABLED", "REVOKED"}:
        # Cache lifecycle: a disabled or revoked node keeps no model cache.
        for field in ("model_catalog", "model_catalog_at", "model_command"):
            data.pop(field, None)
        data.pop("model_command_ack", None)
    data["last_seen"] = utc_now()
    store.workers[payload.node_name] = data
    store.save_worker(data)
    rollout = reconcile_rollout(store.workers)
    data = store.workers[payload.node_name]
    store.save_worker(data)
    node_record = next((node for node in rollout.get("nodes", [])
                        if node.get("node_id") == payload.node_name), None)
    active_request = None
    if node_record and node_record.get("phase") in {"SELF_TESTING", "ROLLING_BACK"}:
        active_request = node_record.get("self_test_request")
    return {"accepted": True, "workers": len(store.workers),
            "desired_state": data.get("desired_state"),
            "self_test_requested_at": data.get("self_test_requested_at"),
            "self_test_request": active_request,
            "update_target_version": data.get("update_target_version"),
            "rollback_target_version": data.get("rollback_target_version"),
            "restart_operation_id": restart_operation_id,
            # Cancel delivery: cleanup stamps the token into this record, so the
            # worker learns the rollout is cancelled and can fence its own
            # pending host-apply request before the agent processes it.
            "rollout_state": rollout.get("state", "IDLE"),
            "rollout_operation_id": rollout.get("operation_id"),
            "cancel_fence_token": data.get("cancel_fence_token"),
            "model_command": pending_model_command(payload.node_name)}


@router.post("/api/workers/model-progress")
def worker_model_progress(payload: ModelProgressReport, request: Request):
    """Worker-authenticated progress/cancel channel for node-local model work."""
    if not _valid_worker_request(payload.node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    result = ingest_model_progress(payload.node_name, {
        "operation_id": payload.operation_id,
        "status": payload.status,
        "phase": payload.phase,
        "progress": payload.progress,
        "error": payload.error,
    })
    if result is None:
        raise HTTPException(404, "Unknown model operation")
    return {"accepted": True, "cancel_requested": result["cancel_requested"],
            "operation": result["operation"]}


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
        # Durable self-test outcome is a registry contract, not a heartbeat
        # detail: a node that last reported PENDING_SELF_TEST / OFFLINE must
        # still carry that status in the listing even after its live record
        # goes offline, so dispatch never treats a stale record as ready.
        if record.get("runtime_status"):
            item["runtime_status"] = record["runtime_status"]
        else:
            item.setdefault("runtime_status", None)
        item["self_test_capabilities"] = record.get("self_test_capabilities") or []
        item["last_self_test_at"] = record.get("last_self_test_at")
        item["update_state"] = {
            "desired_state": item.pop("desired_state", None),
            "update_target_version": item.pop("update_target_version", None),
            "rollback_target_version": item.pop("rollback_target_version", None),
            "self_test_requested_at": item.pop("self_test_requested_at", None),
            "restart_operation_id": item.pop("restart_operation_id", None),
            "restart_ack": item.pop("restart_ack", None),
        }
        result.append(item)
    seen = {item.get("node_id") or item.get("node_name") for item in result}
    for node_id, record in registry.items():
        if node_id in seen or record.get("revoked_at"):
            continue
        if role and record.get("role") != role:
            continue
        if status and status != "OFFLINE":
            continue
        if capability and capability not in (record.get("capabilities") or []):
            continue
        definition = node_roles().get(record.get("role", ""), {})
        result.append({
            **record,
            "node_name": node_id,
            "status": "OFFLINE",
            "modules": definition.get("modules", []),
            "services": definition.get("services", []),
            "update_state": {},
        })
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
