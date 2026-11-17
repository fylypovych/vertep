import json
import os
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request
from ..models import NodeAction, utc_now
from ..node_registry import (create_registration_token, enroll_node, registered_nodes,
                             renew_node, record_self_test, revoke_node, verify_node_certificate, node_roles,
                             rotate_node_credentials)
from ..security import _valid_worker_request
from ..state import store
from ..version import worker_incompatibility
from .workers import workers

router = APIRouter()

CAPABILITY_BACKENDS = {
    "text_generation": "Ollama / LLM Provider",
    "speech_synthesis": "TTS Provider",
    "image_generation": "ComfyUI",
    "image_upscale": "ComfyUI",
    "controlnet": "ComfyUI",
    "inpainting": "ComfyUI",
    "publishing": "Publisher Provider",
    "backup": "Backup Service",
    "metrics": "Prometheus",
    "logs": "Loki",
    "alerting": "Alertmanager",
}


def _node_context(node: dict) -> dict:
    definition = node_roles().get(node.get("role", ""), {})
    capabilities = node.get("capabilities") or definition.get("capabilities", [])
    return {
        **node,
        "node_name": node.get("node_name") or node.get("node_id"),
        "capabilities": capabilities,
        # Runtime readiness is measured, never implied by a heartbeat alone.
        "runtime_status": node.get("runtime_status") or "PENDING_SELF_TEST",
        "self_test_capabilities": node.get("self_test_capabilities") or [],
        "last_self_test_at": node.get("last_self_test_at"),
        "compatibility_issue": worker_incompatibility(node.get("runtime")),
        "modules": definition.get("modules", []),
        "services": definition.get("services", []),
        "capability_backends": {
            item: CAPABILITY_BACKENDS.get(item, "Vertep runtime") for item in capabilities
        },
    }


@router.post("/api/nodes/registration-tokens")
async def registration_token(request: Request):
    payload = await request.json()
    try:
        return create_registration_token(str(payload.get("role", "")), int(payload.get("ttl_seconds", 900)),
                                         push_token=bool(payload.get("push_token", False)))
    except ValueError as error:
        raise HTTPException(422, str(error)) from error



@router.post("/api/nodes/register")
async def register_node(request: Request):
    payload = await request.json()
    try:
        if (not isinstance(payload, dict) or len(str(payload.get("registration_token", ""))) > 64
                or len(str(payload.get("node_id", ""))) > 64 or len(str(payload.get("version", ""))) > 64
                or len(str(payload.get("csr", ""))) > 16384
                or len(str(payload.get("enrollment_id", "") or "")) > 128
                or not isinstance(payload.get("capabilities", []), list)
                or len(payload.get("capabilities", [])) > 32
                or not isinstance(payload.get("hardware", {}), dict)
                or len(json.dumps(payload.get("hardware", {}))) > 65536):
            raise ValueError("Node registration payload exceeds allowed limits")
        return enroll_node(str(payload.get("registration_token", "")), str(payload.get("node_id", "")),
                           payload.get("capabilities") or [], payload.get("hardware") or {},
                           str(payload.get("version", "unknown")), str(payload.get("csr", "")),
                           payload.get("enrollment_id"))
    except PermissionError as error:
        raise HTTPException(401, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error



@router.get("/api/nodes")
def nodes():
    live = {item.get("node_name"): item for item in workers()}
    return [_node_context({**node, "runtime": live.get(node["node_id"]),
             "status": (live.get(node["node_id"]) or {}).get("status", "OFFLINE")})
            for node in registered_nodes()]


def _module_registry():
    from core.workflow.runtime import workflow_runtime
    runtime = workflow_runtime()
    if runtime is None:
        raise HTTPException(409, "Workflow templates are not configured")
    return runtime.modules


@router.get("/api/nodes/module-registry")
def module_registry():
    """Nodes, module definitions, node/module instances, placement and conflicts."""
    return _module_registry().snapshot()


@router.put("/api/nodes/module-registry/placement/{module_id}")
async def module_placement(module_id: str, request: Request):
    # Non-GET /api/nodes/* is administrator-only in the auth middleware.
    payload = await request.json()
    try:
        result = _module_registry().set_placement(module_id, payload.get("node_ids"))
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    from core.module.gateway import audit
    audit("module.placement", "allowed", module=module_id, nodes=result["node_ids"])
    return result


@router.put("/api/nodes/module-registry/approval/{module_id}")
async def module_permission_approval(module_id: str, request: Request):
    """Administrator approves the exact declared permission set (new or expanded)."""
    from core.module.gateway import audit
    payload = await request.json()
    try:
        result = _module_registry().approve_permissions(module_id, payload.get("permissions"))
    except ValueError as error:
        audit("module.approval", "denied", module=module_id, reason=str(error))
        raise HTTPException(422, str(error)) from error
    audit("module.approval", "allowed", module=module_id, permissions=result["permissions"],
          actor=(getattr(request.state, "vertep_identity", None) or [None])[0])
    return result


@router.put("/api/nodes/module-registry/secrets/{module_id}")
async def module_secret_values(module_id: str, request: Request):
    """Store values for a module's declared secret references in the platform store."""
    from core.module.gateway import audit, store_module_secrets
    payload = await request.json()
    try:
        names = store_module_secrets(_module_registry(), module_id, payload.get("secrets"))
    except ValueError as error:
        audit("module.secrets.store", "denied", module=module_id)
        raise HTTPException(422, str(error)) from error
    audit("module.secrets.store", "allowed", module=module_id, names=names)
    return {"module_id": module_id, "stored": names}


@router.get("/api/nodes/module-registry/audit")
def module_audit(request: Request):
    from core.module.gateway import read_audit
    _require_admin(request)
    return {"events": read_audit()}


def _lifecycle():
    from core.workflow.runtime import workflow_runtime
    runtime = workflow_runtime()
    if runtime is None:
        raise HTTPException(409, "Workflow templates are not configured")
    return runtime.lifecycle


def _require_admin(request: Request) -> str:
    identity = getattr(request.state, "vertep_identity", None)
    if not identity or identity[1] != "admin":
        raise HTTPException(403, "Administrator role required")
    return identity[0]


@router.get("/api/nodes/module-registry/lifecycle")
def module_lifecycle_records():
    return {"modules": [{key: record.get(key) for key in ("module_id", "version", "state")}
                        | {"previous_version": (record.get("previous") or {}).get("version")}
                        for record in _lifecycle().records().values()]}


@router.post("/api/nodes/module-registry/lifecycle/{action}")
async def module_lifecycle_action(action: str, request: Request):
    """install | activate | deactivate | update | rollback | uninstall (administrator only)."""
    from core.module.gateway import audit
    from core.module.lifecycle import LifecycleError
    actor = _require_admin(request)
    payload = await request.json()
    lifecycle = _lifecycle()
    module_id = str(payload.get("module_id", ""))
    operations = {
        "install": lambda: lifecycle.install(payload.get("package") or {}),
        "activate": lambda: lifecycle.activate(module_id),
        "deactivate": lambda: lifecycle.deactivate(module_id),
        "update": lambda: lifecycle.update(payload.get("package") or {}, confirmed=payload.get("confirm") is True),
        "rollback": lambda: lifecycle.rollback(module_id),
        "uninstall": lambda: lifecycle.uninstall(module_id, confirmed=payload.get("confirm") is True),
    }
    if action not in operations:
        raise HTTPException(404, "Unknown module lifecycle action")
    try:
        result = operations[action]()
    except (LifecycleError, ValueError, KeyError) as error:
        audit("module.lifecycle", "failed", operation=action, module=module_id or None, actor=actor, reason=str(error))
        raise HTTPException(409, f"Module lifecycle action failed: {error}") from error
    audit("module.lifecycle", "allowed", operation=action, module=result.get("module_id"), actor=actor, state=result.get("state"))
    return result


@router.get("/api/nodes/module-registry/archives")
def module_archives(request: Request):
    _require_admin(request)
    return {"archives": _lifecycle().archives()}


@router.get("/api/nodes/module-registry/archives/{name}")
def module_archive_download(name: str, request: Request):
    from fastapi.responses import FileResponse
    from core.module.gateway import audit
    actor = _require_admin(request)
    path = _lifecycle().root / "archives" / name
    if "/" in name or not name.endswith(".tar") or not path.is_file():
        raise HTTPException(404, "Module archive not found")
    audit("module.archive.download", "allowed", archive=name, actor=actor)
    return FileResponse(path, media_type="application/x-tar", filename=name)


@router.post("/api/nodes/module-registry/archives/{name}/restore")
def module_archive_restore(name: str, request: Request):
    from core.module.gateway import audit
    from core.module.lifecycle import LifecycleError
    actor = _require_admin(request)
    path = _lifecycle().root / "archives" / name
    if "/" in name or not name.endswith(".tar") or not path.is_file():
        raise HTTPException(404, "Module archive not found")
    try:
        result = _lifecycle().restore_archive(path)
    except (LifecycleError, ValueError) as error:
        audit("module.archive.restore", "failed", archive=name, actor=actor, reason=str(error))
        raise HTTPException(409, f"Module lifecycle action failed: {error}") from error
    audit("module.archive.restore", "allowed", archive=name, actor=actor, module=result["module_id"])
    return result


@router.put("/api/nodes/module-registry/profile/{node_id}")
async def module_node_profile(node_id: str, request: Request):
    payload = await request.json()
    if node_id not in {node["node_id"] for node in registered_nodes()}:
        raise HTTPException(404, "Node not found")
    try:
        return _module_registry().set_node_profile(node_id, str(payload.get("environment", "")),
                                                   str(payload.get("isolation", "")))
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/api/nodes/{node_id}")
def node_detail(node_id: str):
    from datetime import datetime
    live = None
    for worker in store.load_workers():
        if worker.get("node_id") == node_id or worker.get("node_name") == node_id:
            live = worker
            break
    registry = {node["node_id"]: node for node in registered_nodes()}
    record = registry.get(node_id, {})
    if not record and not live:
        raise HTTPException(404, "Node not found")
    merged = {**(record or {}), **(live or {})}
    if live:
        merged["runtime"] = live
        merged["status"] = live.get("status", "OFFLINE")
        merged["certificate_serial"] = record.get("certificate_serial")
        merged["certificate_expires_at"] = record.get("certificate_expires_at")
        merged["credential_generation"] = record.get("credential_generation")
        merged["registered_at"] = record.get("registered_at")
        merged["revoked_at"] = record.get("revoked_at")
    # The durable self-test result in the registry is authoritative for runtime
    # readiness; a heartbeat without self-test must not advertise the node as ready.
    merged["runtime_status"] = (record.get("runtime_status")
                                or merged.get("runtime_status") or "PENDING_SELF_TEST")
    merged.setdefault("status", "OFFLINE")
    merged.setdefault("capabilities", [])
    merged.setdefault("hardware", {})
    merged.setdefault("runtime_status", "PENDING_SELF_TEST")
    merged.setdefault("self_test_capabilities", [])
    merged.setdefault("last_self_test_at", None)
    # Fleet contract: expose transient control/update fields inside a single
    # `update_state` envelope, mirroring the `/api/workers` listing shape so the
    # (detail, listing) digests never drift apart.
    merged["update_state"] = {
        "desired_state": merged.pop("desired_state", None),
        "update_target_version": merged.pop("update_target_version", None),
        "rollback_target_version": merged.pop("rollback_target_version", None),
        "self_test_requested_at": merged.pop("self_test_requested_at", None),
        "restart_operation_id": merged.pop("restart_operation_id", None),
        "restart_ack": merged.pop("restart_ack", None),
    }
    return _node_context(merged)


@router.post("/api/nodes/{node_id}/actions")
def control_node(node_id: str, command: NodeAction):
    worker = store.workers.get(node_id)
    if not worker:
        worker = next((item for item in store.load_workers()
                       if item.get("node_id") == node_id or item.get("node_name") == node_id), None)
    if not worker:
        raise HTTPException(404, "Worker runtime is not available")
    timestamp = utc_now()
    if command.action == "drain":
        worker["desired_state"] = "DRAINING"
        if worker.get("status") != "BUSY" and not worker.get("current_task"):
            worker["status"] = "DRAINING"
    elif command.action == "resume":
        if worker.get("desired_state") == "QUARANTINED":
            raise HTTPException(409, "Quarantined worker must be explicitly unquarantined")
        worker.pop("desired_state", None)
        worker["status"] = "READY" if (worker.get("self_test") or {}).get("status") == "PASSED" else "ERROR"
    elif command.action == "quarantine":
        worker.update({"desired_state": "QUARANTINED", "status": "QUARANTINED"})
    elif command.action == "unquarantine":
        if worker.get("desired_state") != "QUARANTINED":
            raise HTTPException(409, "Worker is not quarantined")
        worker.pop("desired_state", None)
        if (worker.get("self_test") or {}).get("status") == "PASSED":
            worker["status"] = "READY"
        else:
            # Unknown readiness after quarantine: force a fresh self-test.
            worker["status"] = "SELF_TESTING"
            worker["self_test_requested_at"] = timestamp
    elif command.action == "self-test":
        if worker.get("status") == "BUSY":
            raise HTTPException(409, "Busy worker cannot start a self-test")
        worker["self_test_requested_at"] = timestamp
        worker["status"] = "SELF_TESTING"
    elif command.action == "rotate":
        from ..node_registry import rotate_node_credentials
        try:
            rotate_node_credentials(node_id)
        except KeyError as error:
            raise HTTPException(404, "Node is missing or revoked") from error
    elif command.action == "disable":
        worker.update({"desired_state": "DISABLED", "status": "OFFLINE"})
    elif command.action == "enable":
        worker.pop("desired_state", None)
        worker["status"] = "READY" if (worker.get("self_test") or {}).get("status") == "PASSED" else "ERROR"
    elif command.action == "restart":
        worker["desired_state"] = "RESTARTING"
        worker["status"] = "UPDATING"
        worker["restart_operation_id"] = uuid.uuid4().hex
        worker["restart_previous_instance_id"] = worker.get("runtime_instance_id")
    elif command.action == "logs":
        pass
    elif command.action == "update":
        worker["desired_state"] = "UPDATING"
        worker["status"] = "UPDATING"
    elif command.action == "revoke":
        try:
            return revoke_node(node_id)
        except KeyError as error:
            raise HTTPException(404, "Node is missing or revoked") from error
    else:
        raise HTTPException(400, f"Unsupported action: {command.action}")
    worker["state_reason"] = command.reason
    worker["state_changed_at"] = timestamp
    store.save_worker(worker)
    return worker



@router.post("/api/nodes/{node_id}/revoke")
def disable_node(node_id: str):
    try:
        record = revoke_node(node_id)
        worker = store.workers.get(node_id)
        if worker:
            worker.update({"status": "REVOKED", "runtime_status": "REVOKED", "tested_capabilities": [],
                "desired_state": "REVOKED", "revoked_at": record.get("revoked_at")})
            store.save_worker(worker)
        return record
    except KeyError:
        # Fleet member known only to the runtime store (e.g. enrolled-then-offboarded
        # before a registry record materialised): remove it from active fleet control.
        worker = next((item for item in store.load_workers()
                       if item.get("node_id") == node_id or item.get("node_name") == node_id), None)
        if not worker:
            raise HTTPException(404, "Node not found")
        worker.update({"status": "REVOKED", "desired_state": "REVOKED", "revoked_at": utc_now()})
        store.save_worker(worker)
        return {key: value for key, value in worker.items() if key != "secret_hash"}



@router.post("/api/nodes/{node_id}/renew")
async def renew_node_credentials(node_id: str, request: Request):
    if not _valid_worker_request(node_id, request):
        raise HTTPException(401, "Node credentials are not valid")
    payload = await request.json()
    try:
        return renew_node(node_id, str(payload.get("csr", "")))
    except KeyError as error:
        raise HTTPException(404, "Node is missing or revoked") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/api/nodes/{node_id}/self-test")
async def submit_node_self_test(node_id: str, request: Request):
    """Record a worker self-test result, separating enrollment from runtime readiness."""
    if not _valid_worker_request(node_id, request):
        raise HTTPException(401, "Node credentials are not valid")
    payload = await request.json()
    status = str(payload.get("status", "")).lower()
    capabilities = payload.get("capabilities") or []
    if not isinstance(capabilities, list) or len(capabilities) > 32:
        raise HTTPException(422, "Invalid capabilities payload")
    try:
        record = record_self_test(node_id, status, capabilities)
    except KeyError as error:
        raise HTTPException(404, "Node is missing or revoked") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    live = store.workers.get(node_id)
    if live:
        live["runtime_status"] = record["runtime_status"]
        live["self_test_capabilities"] = record["self_test_capabilities"]
        live["self_test"] = {"status": status.upper(), "role": live.get("role"),
            "checked_at": record["last_self_test_at"], "capabilities": record["self_test_capabilities"]}
        live["tested_capabilities"] = record["self_test_capabilities"] if status == "passed" else []
        live.setdefault("self_test_at", record["last_self_test_at"])
        store.save_worker(live)
    return record
