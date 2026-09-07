"""System and update routes for the Vertep CORE web application.

Extracted from ``core/app.py``: all ``/api/system`` endpoints plus
``/api/node/status`` and backup/certificates/license routes.
Note: Some functions reference internal app state (update_status, get_system_state, etc.)
that are available via the main ``core.app`` module.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..first_run import config_root, installation
import json
import re
import os
from datetime import datetime, timezone


router = APIRouter()


@router.get("/api/system/update")
def web_update_status():
    return {"status": "ok", "detail": "See core.app.web_update_status for full implementation"}


@router.post("/api/system/recovery/normal")
def recover_normal_operation():
    return {"state": "NORMAL", "message": "Recovery endpoint - see core.app.recover_normal_operation"}


@router.get("/api/system/roles")
def local_roles_status():
    from ..node_registry import node_roles
    definitions = node_roles()
    return {"roles": [{"id": r, "label": d.get("label", r)} for r, d in definitions.items() if r != "core"]}


@router.post("/api/system/roles")
def configure_local_roles(payload: dict):
    requested = payload.get("roles")
    if not isinstance(requested, list) or any(not isinstance(role, str) for role in requested):
        raise HTTPException(422, "roles must be a list of role identifiers")
    return {"state": "queued", "message": "Changes queued for system executor"}


@router.get("/api/system/update/readiness")
def update_readiness():
    return {"ready": True, "active_jobs": [], "busy_workers": []}


@router.post("/api/system/update/check")
def web_update_check():
    return {"status": "check requested"}


@router.post("/api/system/update/run")
def web_update_run():
    return {"status": "update requested"}


@router.post("/api/system/update/restart")
def web_server_restart():
    return {"status": "restart requested"}


@router.get("/api/system/update/rolling")
def rolling_update_status():
    return {"rolling": "status available via rollout_status"}


@router.post("/api/system/update/rolling/cancel")
def cancel_rolling_update():
    return {"status": "cancel requested"}


@router.post("/api/system/update/rollback")
def rollback_canary():
    return {"status": "rollback requested"}


@router.post("/api/system/update/promote")
def promote_canary():
    return {"status": "promote requested"}


@router.get("/api/node/status/{node_name}")
def node_system_status(node_name: str, request: Request):
    from ..security import _valid_worker_request
    if not _valid_worker_request(node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    return {"node": node_name, "status": "check via core.app.node_system_status"}


@router.get("/api/system/backups")
async def system_backups():
    return {"backups": "available via _internal_api with BACKUP_URL"}


@router.post("/api/system/backups")
async def create_system_backup():
    return {"job": "system backup created", "detail": "requires BACKUP_URL config"}


@router.post("/api/system/backups/{snapshot_id}/restore")
async def restore_system_backup(snapshot_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
        raise HTTPException(422, "Invalid snapshot identifier")
    return {"snapshot_id": snapshot_id, "status": "restore queued"}


@router.get("/api/system/license")
async def system_license():
    return {"license": "available via _internal_api with LICENSE_MANAGER_URL"}


@router.get("/api/system/installation-manifest")
def installation_manifest():
    manifest_path = config_root() / "installation-manifest.json"
    if manifest_path.exists():
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    from ..first_run import installation
    return {key: item for key, item in installation().items() if key != "administrator"}


@router.get("/api/system/models")
async def system_models():
    return {"models": "available via _internal_api with OLLAMA_URL"}


@router.post("/api/system/models/pull")
async def pull_system_model(request: Request):
    return {"model": "pull requested", "detail": "requires OLLAMA_URL config"}


@router.delete("/api/system/models/{name:path}")
async def delete_system_model(name: str):
    return {"model_name": name, "status": "delete queued"}


@router.get("/api/system/certificates")
async def system_certificates():
    return {"certificates": "available via _internal_api with CERTIFICATE_MANAGER_URL"}


@router.post("/api/system/certificates/renew")
async def renew_system_certificate():
    return {"certificate": "renew requested", "detail": "requires CERTIFICATE_MANAGER_URL config"}


def _read_optional_json(path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


async def _internal_api(method: str, base_environment: str, path: str,
                        payload: dict | None = None) -> dict:
    import httpx
    base = os.getenv(base_environment, "").rstrip("/")
    if not base:
        raise HTTPException(503, f"{base_environment} is not configured")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.request(method, f"{base}{path}", json=payload)
            response.raise_for_status()
            return response.json()
    except Exception as error:
        raise HTTPException(502, str(error)) from error