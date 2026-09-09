import json
import os
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request
from ..models import NodeAction, utc_now
from ..node_registry import (create_registration_token, enroll_node, registered_nodes,
                            renew_node, revoke_node, verify_node_certificate, node_roles)
from ..security import _valid_worker_request
from ..state import store
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
                or not isinstance(payload.get("capabilities", []), list)
                or len(payload.get("capabilities", [])) > 32
                or not isinstance(payload.get("hardware", {}), dict)
                or len(json.dumps(payload.get("hardware", {}))) > 65536):
            raise ValueError("Node registration payload exceeds allowed limits")
        return enroll_node(str(payload.get("registration_token", "")), str(payload.get("node_id", "")),
                           payload.get("capabilities") or [], payload.get("hardware") or {},
                           str(payload.get("version", "unknown")), str(payload.get("csr", "")))
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
    merged.setdefault("status", "OFFLINE")
    merged.setdefault("capabilities", [])
    merged.setdefault("hardware", {})
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
        worker["status"] = "SELF_TESTING"
        worker["self_test_requested_at"] = timestamp
    elif command.action == "self-test":
        if worker.get("status") == "BUSY":
            raise HTTPException(409, "Busy worker cannot start a self-test")
        worker["self_test_requested_at"] = timestamp
        worker["status"] = "SELF_TESTING"
    elif command.action == "rotate":
        from ..node_registry import renew_node
        try:
            renew_node(node_id, "")
        except KeyError as error:
            raise HTTPException(404, "Node is missing or revoked") from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
    elif command.action == "disable":
        worker.update({"desired_state": "DISABLED", "status": "OFFLINE"})
    elif command.action == "enable":
        worker.pop("desired_state", None)
        worker["status"] = "READY" if (worker.get("self_test") or {}).get("status") == "PASSED" else "ERROR"
    elif command.action == "restart":
        worker["desired_state"] = "RESTARTING"
        worker["status"] = "UPDATING"
    elif command.action == "logs":
        pass
    elif command.action == "update":
        worker["desired_state"] = "UPDATING"
        worker["status"] = "UPDATING"
    else:
        raise HTTPException(400, f"Unsupported action: {command.action}")
    worker["state_reason"] = command.reason
    worker["state_changed_at"] = timestamp
    store.save_worker(worker)
    return worker



@router.post("/api/nodes/{node_id}/revoke")
def disable_node(node_id: str):
    try:
        return revoke_node(node_id)
    except KeyError as error:
        raise HTTPException(404, "Node not found") from error



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
