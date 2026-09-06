"""First-run / setup routes for the Vertep CORE web application."""
import os
import re
import socket
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request

from ..first_run import (complete_setup, config_root, set_integration_secret,
                        setup_status)
from ..node_registry import create_node_csr, create_registration_token, node_roles
from ..state import store, task_queue
from ..version import application_version

router = APIRouter()


@router.get("/api/setup")
def first_run_status():
    return {**setup_status(), "roles": node_roles()}


@router.get("/api/setup/health")
def first_run_health():
    checks = {"core": "OK", "api": "OK", "web_ui": "OK"}
    try:
        with socket.create_connection((os.getenv("POSTGRES_HOST", "postgres"), 5432), timeout=1):
            checks["postgresql"] = "OK"
    except OSError:
        checks["postgresql"] = "OFFLINE"
    checks["redis"] = "OK" if task_queue.backend == "redis" else "OFFLINE"
    checks["worker"] = "OK" if any(item.get("status") not in {"OFFLINE", "ERROR"}
                                      for item in store.workers.values()) else "OPTIONAL"
    hardware = setup_status()["hardware"]
    gpu = hardware.get("gpu") or {}
    checks["gpu"] = "OPTIONAL" if gpu.get("vendor") in {None, "none"} else (
        "OK" if gpu.get("driver") not in {None, "unavailable"} else "DRIVER_REQUIRED")
    checks["cuda"] = "OPTIONAL" if gpu.get("vendor") != "nvidia" else (
        "OK" if gpu.get("cuda") not in {None, "unavailable"} else "UNAVAILABLE")
    checks["ollama"] = "CONFIGURED" if os.getenv("OLLAMA_URL") else "OPTIONAL"
    checks["docker"] = "OK" if hardware.get("docker_version") else "UNKNOWN"
    return {"ready": all(value not in {"OFFLINE", "UNAVAILABLE"} for value in checks.values()),
            "checks": checks}


@router.post("/api/setup/complete")
async def first_run_complete(request: Request):
    payload = await request.json()
    try:
        role = str(payload.get("node_role", "core"))
        core_url = str(payload.get("core_url") or "").rstrip("/")
        credentials = None
        if role != "core":
            role_definition = node_roles().get(role)
            if not role_definition or not core_url.startswith("https://"):
                raise ValueError("A valid HTTPS Core URL is required")
            node_id = re.sub(r"[^a-z0-9-]", "-", str(payload.get("installation_name", "")).lower()).strip("-")
            csr = create_node_csr(node_id)
            core_certificate = str(payload.get("core_certificate") or "")
            verify: str | bool = True
            if core_certificate:
                if len(core_certificate) > 32768 or "BEGIN CERTIFICATE" not in core_certificate:
                    raise ValueError("Core certificate must be PEM encoded")
                pinned = config_root() / "core-onboarding.crt"
                pinned.write_text(core_certificate, encoding="utf-8")
                os.chmod(pinned, 0o600)
                verify = str(pinned)
            async with httpx.AsyncClient(timeout=30, verify=verify) as enrollment_client:
                response = await enrollment_client.post(f"{core_url}/api/nodes/register", json={
                    "registration_token": payload.get("registration_token"), "node_id": node_id,
                    "capabilities": role_definition["capabilities"], "hardware": setup_status()["hardware"],
                    "version": application_version(), "csr": csr})
            if response.status_code != 200:
                raise ValueError(f"Core registration failed: {response.text[:300]}")
            credentials = response.json()
        backend = str(payload.get("ai_backend", "skip"))
        backend_url = str(payload.get("backend_url") or "").rstrip("/") or None
        backend_model = str(payload.get("backend_model") or "").strip() or None
        backend_key = str(payload.get("backend_api_key") or "")
        await _validate_ai_backend(backend, backend_url, backend_model, backend_key)
        registration_token = create_registration_token("gpu", 900) if role == "core" else None
        if backend_key:
            set_integration_secret("external_ai_api_key", backend_key)
        completed = complete_setup(str(payload.get("installation_name", "")), str(payload.get("username", "")),
                                   str(payload.get("password", "")), str(payload.get("password_confirmation", "")),
                                   backend, backend_url,
                                   role, core_url or None, credentials,
                                   str(payload.get("web_domain") or "").strip() or None, backend_model)
        if role == "core":
            completed["core_url"] = os.getenv("PUBLIC_URL") or os.getenv("WEB_DOMAIN") or str(request.base_url).rstrip("/")
            completed["core_certificate"] = (Path(os.getenv("CORE_CERTIFICATE_PATH", "/data/config/pki/ca.crt"))
                                               .read_text(encoding="utf-8")
                                               if Path(os.getenv("CORE_CERTIFICATE_PATH", "/data/config/pki/ca.crt")).is_file()
                                               else None)
            completed["registration_token"] = registration_token
        return completed
    except FileExistsError as error:
        raise HTTPException(409, str(error)) from error
    except httpx.HTTPError as error:
        raise HTTPException(502, f"Core registration is unavailable: {error}") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


async def _validate_ai_backend(backend: str, url: str | None, model: str | None,
                               api_key: str) -> None:
    if backend == "skip":
        return
    if backend not in {"ollama", "openai", "external"}:
        raise ValueError("Unsupported AI backend")
    if not model:
        raise ValueError("AI model is required")
    if backend == "ollama" and url is None:
        if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._:/+-]{0,127}", model):
            raise ValueError("Invalid Ollama model name")
        # The appliance-managed Ollama container is started only after the
        # wizard has committed the selected node role. Deployment validates
        # the container and pulls the model once that service is available.
        return
    base_url = url or (os.getenv("OLLAMA_URL", "http://ollama:11434") if backend == "ollama"
                       else "https://api.openai.com/v1")
    if backend != "ollama" and not api_key:
        raise ValueError("AI backend API key is required")
    if backend != "ollama" and not base_url.startswith("https://"):
        raise ValueError("External AI backend must use HTTPS")
    endpoint = f"{base_url}/api/tags" if backend == "ollama" else f"{base_url}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(endpoint, headers=headers)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise ValueError(f"AI backend validation failed: {error}") from error
    rows = body.get("models", body.get("data", [])) if isinstance(body, dict) else []
    names = {str(item.get("name") or item.get("model") or item.get("id")) for item in rows
             if isinstance(item, dict)}
    available = model in names or any(name.split(":", 1)[0] == model for name in names)
    if not available and backend == "ollama":
        try:
            async with httpx.AsyncClient(timeout=3600) as client:
                response = await client.post(f"{base_url}/api/pull",
                                             json={"name": model, "stream": False})
                response.raise_for_status()
            return
        except httpx.HTTPError as error:
            raise ValueError(f"AI model '{model}' could not be installed: {error}") from error
    if not available:
        raise ValueError(f"AI model '{model}' is not available from the selected backend")