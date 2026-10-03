"""Settings, integrations and logo routes for the Vertep CORE web application."""
import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ..first_run import config_root, integration_secret_status, set_integration_secret
from ..logging_config import secret_redact
from ..models import IntegrationSecretUpdate
from ..provider_switch import ProviderSwitchError, switch_provider
from adapters.providers import provider_matrix

router = APIRouter()


@router.get("/api/settings/providers")
def provider_backends():
    """Active backend matrix (Settings → Движки обробки)."""
    return {"matrix": provider_matrix()}


@router.get("/api/settings/video-engine")
def effective_video_engine(probe: bool = False):
    """Effective video-engine configuration (Issue #122 P7).

    ``selected``, ``effective`` and ``config_revision`` are returned together so a
    caller can prove they describe one and the same engine. No secret value is ever
    exposed: only the reference of the secret the engine uses. ``probe=true``
    additionally verifies readiness on the actual runtime.
    """
    from ..engine_config import effective_engine_config

    return effective_engine_config(probe=probe)


@router.post("/api/settings/providers/{slot}")
def switch_provider_backend(slot: str, payload: dict):
    """Switch one provider slot with persist → apply → verify → rollback."""
    actor = str(payload.get("actor") or "").strip()[:120] or f"web:settings:{slot}"
    try:
        return switch_provider(slot, str(payload.get("backend") or ""), actor=actor)
    except ProviderSwitchError as error:
        raise HTTPException(error.status_code, error.message) from error


@router.get("/api/integrations")
def integrations():
    endpoints = {
        "ollama": os.getenv("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/tags",
        "comfyui": os.getenv("COMFYUI_URL", "http://127.0.0.1:8188") + "/system_stats",
    }
    result = {}
    for name, endpoint in endpoints.items():
        try:
            # These are node-local probes; inherited host proxy settings can
            # both misroute them and require optional SOCKS dependencies.
            with httpx.Client(timeout=3, trust_env=False) as client:
                response = client.get(endpoint)
            result[name] = {"status": "ONLINE", "http_status": response.status_code}
        except (httpx.HTTPError, OSError, ValueError, ImportError) as error:
            # Issue #84: transport errors can embed credentials (proxy URLs with
            # auth, tokens in query strings), so the API must not echo them raw.
            result[name] = {"status": "OFFLINE", "error": secret_redact(str(error))}
    matrix = provider_matrix()
    result["publisher"] = matrix.get("publisher", {}).get("platforms", {})
    return result


@router.get("/api/settings/secrets")
def secret_settings():
    return {"secrets": integration_secret_status(), "values_exposed": False}


@router.put("/api/settings/secrets/{name}")
def update_secret_setting(name: str, payload: IntegrationSecretUpdate):
    try:
        result = {"secrets": set_integration_secret(name, payload.value), "values_exposed": False}
        if name == "telegram_bot_token":
            import core.app as core_app
            core_app._restart_telegram_polling()
        return result
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.delete("/api/settings/secrets/{name}")
def delete_secret_setting(name: str):
    try:
        result = {"secrets": set_integration_secret(name, None), "values_exposed": False}
        if name == "telegram_bot_token":
            import core.app as core_app
            core_app._restart_telegram_polling()
        return result
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
