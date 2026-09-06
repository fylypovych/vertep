"""Settings, integrations and logo routes for the Vertep CORE web application."""
import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ..first_run import config_root, integration_secret_status, set_integration_secret
from ..models import IntegrationSecretUpdate

router = APIRouter()


@router.get("/api/integrations")
def integrations():
    endpoints = {
        "ollama": os.getenv("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/tags",
        "comfyui": os.getenv("COMFYUI_URL", "http://127.0.0.1:8188") + "/system_stats",
    }
    result = {}
    for name, endpoint in endpoints.items():
        try:
            response = httpx.get(endpoint, timeout=3)
            result[name] = {"status": "ONLINE", "http_status": response.status_code}
        except httpx.HTTPError as error:
            result[name] = {"status": "OFFLINE", "error": str(error)}
    return result


@router.get("/api/settings/secrets")
def secret_settings():
    return {"secrets": integration_secret_status(), "values_exposed": False}


@router.put("/api/settings/secrets/{name}")
def update_secret_setting(name: str, payload: IntegrationSecretUpdate):
    try:
        return {"secrets": set_integration_secret(name, payload.value), "values_exposed": False}
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.delete("/api/settings/secrets/{name}")
def delete_secret_setting(name: str):
    try:
        return {"secrets": set_integration_secret(name, None), "values_exposed": False}
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/api/settings/logo")
def get_logo():
    logo_path = config_root() / "dashboard-logo.png"
    if not logo_path.is_file():
        raise HTTPException(404, "Logo not found")
    return FileResponse(logo_path, media_type="image/png")


@router.put("/api/settings/logo")
async def put_logo(request: Request):
    content_type = request.headers.get("content-type", "")
    if "image/png" not in content_type and "image/" not in content_type:
        raise HTTPException(422, "Expected image upload")
    body = await request.body()
    if len(body) > 2 * 1024 * 1024:
        raise HTTPException(413, "Logo too large")
    logo_path = config_root() / "dashboard-logo.png"
    logo_path.write_bytes(body)
    return {"saved": True}


@router.delete("/api/settings/logo")
def delete_logo():
    logo_path = config_root() / "dashboard-logo.png"
    if logo_path.is_file():
        logo_path.unlink()
    return {"deleted": True}
