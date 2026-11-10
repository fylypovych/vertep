"""First-run / setup routes for the Vertep CORE web application."""
import asyncio
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
from ..health_checks import REQUIRED_HEALTH_CHECKS

router = APIRouter()

# Readiness services that are mandatory for each node role.  A required service
# is ready only when its check reports exactly ``OK``; a ``None`` (not-applicable
# / not configured) or ``UNKNOWN`` readiness never substitutes readiness for a
# required service, so it blocks the wizard's ready gate (i.0.0.1.52).

# Status prefixes that block ready regardless of whether the service is
# required (an explicit failure must never read as ready).
_BLOCKING_PREFIXES = ("OFFLINE", "UNAVAILABLE", "FAIL", "DRIVER_REQUIRED", "UNKNOWN")


def _ensure_enrollment_id(pki: Path) -> str:
    """Return a stable local enrollment id used as the lost-response retry key."""
    path = pki / "enrollment.id"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", existing):
            return existing
    except OSError:
        pass
    import secrets
    pki.mkdir(parents=True, exist_ok=True)
    value = f"onboarding-{secrets.token_hex(8)}"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return value


async def _enroll_with_retry(core_url: str, body: dict, verify, attempts: int = 3,
                             base_delay: float = 0.5) -> httpx.Response:
    """Register a non-Core node, retrying transport failures and lost responses.

    A single-use registration token must not be burned by a dropped connection
    that happened after Core already committed the enrollment, so the request
    carries a stable ``enrollment_id`` that makes the retry idempotent on the
    Core side.  Permanent 4xx answers are surfaced at once.
    """
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            async with httpx.AsyncClient(timeout=30, verify=verify) as enrollment_client:
                return await enrollment_client.post(f"{core_url}/api/nodes/register", json=body)
        except (httpx.HTTPError, httpx.TimeoutException) as error:
            last_error = error
            if attempt >= max(1, attempts):
                raise
            await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
    raise last_error if last_error else RuntimeError("enrollment produced no response")  # pragma: no cover


@router.get("/api/setup")
def first_run_status():
    from ..health_checks import effective_health_role, health_selection
    role = effective_health_role()
    roles, _ = health_selection(role)
    return {**setup_status(), "roles": node_roles(),
            "selected_additional_roles": sorted(roles - {role})}


@router.get("/api/setup/health")
def first_run_health(role: str | None = None, backend: str | None = None,
                     additional_roles: str | None = None):
    """Role-aware readiness report for the First Run Wizard.

    Shared selection-aware probes: native Docker or fresh host observation,
    real Redis PING and selected service HTTP probes. Missing or failed required
    checks block completion; services outside the selection are OPTIONAL.
    """
    from ..health_checks import run_checks, effective_health_role

    role = role or effective_health_role()
    if role not in REQUIRED_HEALTH_CHECKS:
        raise HTTPException(422, "Непідтримувана роль вузла")
    try:
        raw = run_checks(role) if backend is None and additional_roles is None else run_checks(
            role, backend=backend,
            additional_roles=additional_roles.split(',') if additional_roles else ([] if additional_roles == '' else None))
    except ValueError as error:
        raise HTTPException(422, "Непідтримувана конфігурація перевірки") from error
    required = raw.get("required", REQUIRED_HEALTH_CHECKS.get(role, frozenset()))
    checks: dict[str, str] = {}
    for name, value in raw.items():
        if name in {"role", "checked_at", "required"}:
            continue
        if isinstance(value, tuple):
            ok, message = value[0], value[1]
            if ok is True:
                checks[name] = "OK"
            elif ok is False:
                checks[name] = f"OFFLINE: {message}"
            elif name in required:
                # A required service that is absent/unconfigured/unknown must
                # not be downgraded to OPTIONAL — it fails the ready gate.
                checks[name] = f"UNKNOWN: {message}"
            else:
                checks[name] = "OPTIONAL"
        else:
            checks[name] = str(value)
    ready = bool(required) and all(checks.get(name) == "OK" for name in required)
    return {"ready": ready, "checks": checks, "required": sorted(required),
            "details": {name: {"required": name in required,
                               "code": value[1] if isinstance(value, tuple) else "health_probe_failed"}
                        for name, value in raw.items() if isinstance(value, tuple)},
            "status": "HEALTHY" if ready else "UNHEALTHY", "role": role}


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
            enrollment_id = _ensure_enrollment_id(config_root() / "pki")
            response = await _enroll_with_retry(
                core_url,
                {"registration_token": payload.get("registration_token"), "node_id": node_id,
                 "enrollment_id": enrollment_id,
                 "capabilities": role_definition["capabilities"], "hardware": setup_status()["hardware"],
                 "version": application_version(), "csr": csr},
                verify)
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
            from ..post_setup import acknowledge_backup_key, read_backup_key
            from ..telegram_store import save_telegram_settings
            bot_token = str(payload.get("telegram_bot_token") or "").strip()
            if bot_token:
                set_integration_secret("telegram_bot_token", bot_token)
            admins = str(payload.get("telegram_admin_chat_ids") or "").strip()
            allowed = str(payload.get("telegram_allowed_chat_ids") or "").strip()
            if admins or allowed:
                save_telegram_settings(allowed_chat_ids=allowed, admin_chat_ids=admins)
            # The wizard shows the key once and will not continue until it is saved.
            completed["backup_key"] = read_backup_key()
            if completed["backup_key"]:
                acknowledge_backup_key()
        return completed
    except FileExistsError as error:
        raise HTTPException(409, str(error)) from error
    except httpx.HTTPError as error:
        raise HTTPException(502, f"Core registration is unavailable: {error}") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/api/setup/selection")
async def first_run_selection(request: Request):
    """Stage local services using the existing host deployment executor.

    This does not create an administrator, mark setup complete or store keys.
    The wizard can keep its entered values while deployment starts services.
    """
    from ..first_run import _read, _write, is_configured
    from ..deployment_plan import create_plan
    if is_configured():
        raise HTTPException(409, "First-run setup has already been completed")
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(422, "Непідтримувана конфігурація перевірки")
    role = payload.get("node_role", "core")
    backend = payload.get("ai_backend", "skip")
    if not isinstance(role, str) or not isinstance(backend, str) or role not in node_roles() or backend not in {"ollama", "openai", "external", "skip"}:
        raise HTTPException(422, "Непідтримувана конфігурація перевірки")
    from urllib.parse import urlsplit
    backend_url = payload.get("backend_url")
    if backend_url:
        if not isinstance(backend_url, str) or len(backend_url) > 2048:
            raise HTTPException(422, "Invalid backend URL")
        try:
            parsed = urlsplit(backend_url)
        except ValueError as error:
            raise HTTPException(422, "Invalid backend URL") from error
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise HTTPException(422, "Invalid backend URL")
    locked = os.getenv("NODE_ROLE")
    if locked not in {None, "unassigned", role}:
        raise HTTPException(422, "Непідтримувана роль вузла")
    previous = _read("deployment-plan.json")
    previous_selection = _read("setup-selection.json")
    extras = (set(previous.get("additional_roles", [])) if previous.get("role") == role else
              {item.strip() for item in os.getenv("NODE_ADDITIONAL_ROLES", "").split(",") if item.strip()}) if role == "core" else set()
    # Explicit manual selection replaces bootstrap roles, including an empty list.
    if "additional_roles" in payload:
        chosen = payload["additional_roles"]
        if (not isinstance(chosen, list)
                or any(not isinstance(item, str) or item == "core" or item not in node_roles() for item in chosen)
                or (role != "core" and chosen)):
            raise HTTPException(422, "Непідтримувана конфігурація перевірки")
        extras = set(chosen)
    # An implicit managed Text role belongs only to the selected local backend.
    elif _read("setup-selection.json").get("managed_text"):
        extras.discard("text")
    managed = role == "core" and backend == "ollama" and not payload.get("backend_url")
    managed_text_added = managed and "text" not in extras
    if managed:
        extras.add("text")
    model = payload.get("backend_model") or "llama3.2"
    if not isinstance(model, str) or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._:/+-]{0,127}", model):
        raise HTTPException(422, "Invalid Ollama model name")
    plan = create_plan(node_roles(), role, os.getenv("VERTEP_VERSION", application_version()), sorted(extras))
    # Provenance is separate from the signed plan hash.
    _write("setup-selection.json", {"role": role, "additional_roles": sorted(extras),
           "ai_backend": backend, "backend_url": payload.get("backend_url"), "managed_text": managed_text_added})
    if previous.get("sha256") != plan["sha256"] or previous_selection.get("ai_backend") != backend or not (config_root() / "runtime-inventory.json").exists():
        _write("deployment-request.json", {"schema": 1, "role": role, "version": plan["version"],
                "additional_roles": plan.get("additional_roles", []), "ai_backend": backend,
                "backend_url": payload.get("backend_url"), "ollama_model": model,
                "core_url": payload.get("core_url"), "plan_sha256": plan["sha256"]})
    return {"accepted": True}


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
    if backend == "external" and not url:
        raise ValueError("External AI backend requires a URL")
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
