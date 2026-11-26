"""CORE gateway for module APIs, module secrets and signed UI extensions.

* ``/api/modules/{namespace}/v{major}/{path}`` forwards to the READY owner
  instance of the module that declares that API namespace, only for routes the
  manifest declares and only for the declared major version. Users are
  authenticated and role-checked by the platform middleware; a module calling
  another module authenticates with its module token and may use only the
  APIs its manifest ``requires``.
* ``/api/modules/self/secrets`` returns, to a module token, only that
  module's declared secret references from the platform secret store.
* ``/api/modules/ui/...`` serves a READY module's UI files after checking the
  bytes against the sha256 digests in its signed manifest, with a sandbox CSP.
* Every request, including denials and UI probes, is appended to the module
  audit trail.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

router = APIRouter()
_audit_lock = threading.Lock()
TOKEN_KEY_NAME = "module_token_key"
UI_CSP = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
          "img-src data:; frame-ancestors 'self'")
_FORWARD_HEADERS = {"content-type", "accept", "accept-language", "idempotency-key"}


# -- audit -----------------------------------------------------------------------

def audit_path() -> Path:
    configured = os.getenv("VERTEP_MODULE_AUDIT_FILE")
    if configured:
        return Path(configured)
    from core.first_run import config_root
    return config_root() / "module-audit.jsonl"


def audit(action: str, outcome: str, **details) -> None:
    """Append one event; probes and denials are recorded like any other action."""
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "action": action, "outcome": outcome,
             **{key: value for key, value in details.items() if value is not None}}
    path = audit_path()
    with _audit_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def read_audit(limit: int = 500) -> list[dict]:
    path = audit_path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    return [json.loads(line) for line in lines if line.strip()]


# -- module tokens -------------------------------------------------------------------

def _token_key() -> bytes:
    from core.first_run import _secret_lock, _write_encrypted_secrets, ensure_secret_store
    with _secret_lock:
        stored = ensure_secret_store()
        key = stored.get(TOKEN_KEY_NAME)
        if not key:
            key = secrets.token_urlsafe(48)
            stored[TOKEN_KEY_NAME] = key
            _write_encrypted_secrets(stored)
    return key.encode()


def issue_token(module_id: str, node_id: str) -> str:
    payload = f"{module_id}|{node_id}"
    signature = hmac.new(_token_key(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{signature}".encode()).decode()


USER_CONTEXT_SECONDS = 120


def issue_user_context(user: str, role: str, module_id: str, now: float | None = None) -> str:
    """#502: a short-lived, tamper-proof statement of the signed-in user, bound to the
    one module the Gateway forwards the request to. The module may hand it back to
    CORE with an action; it never carries rights of its own."""
    payload = json.dumps({"user": user, "role": role, "aud": module_id,
                          "exp": int((now if now is not None else time.time()) + USER_CONTEXT_SECONDS),
                          "nonce": secrets.token_hex(8)}, sort_keys=True, separators=(",", ":"))
    signature = hmac.new(_token_key(), b"user-context|" + payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{signature}".encode()).decode()


def verify_user_context(token: str, module_id: str, now: float | None = None) -> tuple[str, str]:
    """(user, role) of a valid context for ``module_id``; raises PermissionError otherwise."""
    try:
        payload, signature = base64.urlsafe_b64decode(token.encode()).decode().rsplit("|", 1)
        expected = hmac.new(_token_key(), b"user-context|" + payload.encode(), hashlib.sha256).hexdigest()
        data = json.loads(payload)
    except (ValueError, UnicodeError) as error:
        raise PermissionError("Invalid user context") from error
    if not hmac.compare_digest(signature, expected):
        raise PermissionError("Invalid user context")
    if data.get("aud") != module_id or int(data.get("exp", 0)) <= (now if now is not None else time.time()):
        raise PermissionError("Expired or foreign user context")
    from core.security import _account_role
    user = str(data.get("user") or "")
    # The role is re-derived from the account now, never taken from the module.
    return user, _account_role(user, str(data.get("role") or ""))


def token_identity(token: str) -> tuple[str, str] | None:
    try:
        module_id, node_id, signature = base64.urlsafe_b64decode(token.encode()).decode().split("|")
    except (ValueError, UnicodeError):
        return None
    expected = hmac.new(_token_key(), f"{module_id}|{node_id}".encode(), hashlib.sha256).hexdigest()
    return (module_id, node_id) if hmac.compare_digest(signature, expected) else None


# -- helpers -------------------------------------------------------------------------

def _modules_or_none():
    """The module registry; it exists in both workflow modes (static is the default)."""
    from core.workflow.runtime import module_runtime
    runtime = module_runtime()
    return None if runtime is None else runtime.modules


def _registry():
    registry = _modules_or_none()
    if registry is None:
        raise HTTPException(409, "Workflow templates are not configured")
    return registry


def _ready_instance(registry, module_id: str, node_id: str | None = None):
    for instance in registry.ready_instances(module_id):
        if node_id is None or instance.node_id == node_id:
            return instance
    return None


def call_module(module_id: str, method: str, route: str, params: dict | None = None,
                json_body: object | None = None,
                extra_headers: dict[str, str] | None = None) -> tuple[int, object] | None:
    """CORE-internal call to a READY module's declared API route.

    None when the module has no READY instance (optional module not in use);
    raises httpx.HTTPError when a READY instance does not answer (fail closed).
    """
    registry = _modules_or_none()
    if registry is None:
        return None
    instance = _ready_instance(registry, module_id)
    if instance is None or not instance.endpoint:
        return None
    if not instance.manifest.api or not _route_matches(instance.manifest.api["routes"], route.split("?", 1)[0]):
        raise ValueError(f"Route {route} is not declared by module {module_id}")
    headers = {"x-vertep-module-token": issue_token(instance.module_id, instance.node_id),
               "x-vertep-caller": "core"}
    if extra_headers:
        headers.update(extra_headers)
    response = httpx.request(method, f"{instance.endpoint}{route}", params=params, json=json_body, timeout=10,
                             headers=headers)
    audit("module.core_call", "allowed", module=module_id, path=route, status=response.status_code)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


def _caller_module(request: Request, registry):
    """Module identity of a module-token caller; only while its instance is READY."""
    token = request.headers.get("x-vertep-module-token", "")
    if not token:
        return None
    identity = token_identity(token)
    instance = _ready_instance(registry, identity[0], identity[1]) if identity else None
    if instance is None:
        audit("module.token", "denied", path=request.url.path)
        raise HTTPException(401, "Module token is not valid")
    return instance


def _route_matches(declared: list[str], path: str) -> bool:
    for route in declared:
        pattern = "".join(r"[^/]+" if part.startswith("{") else re.escape(part)
                          for part in re.split(r"(\{[^}]+\})", route))
        if re.fullmatch(pattern, path):
            return True
    return False


def _version_tuple(value: str) -> tuple[int, int]:
    major, minor = value.split(".")
    return int(major), int(minor)


# -- worker token issue ------------------------------------------------------------

@router.post("/api/modules/token")
async def module_token(request: Request):
    """A Worker obtains the token for an application container it is about to start."""
    from core.security import _valid_worker_request
    payload = await request.json()
    node_name, module_id = str(payload.get("node_name", "")), str(payload.get("module_id", ""))
    if not node_name or not _valid_worker_request(node_name, request):
        audit("module.token.issue", "denied", node=node_name, module=module_id, reason="worker_auth")
        raise HTTPException(401, "Token is not valid for this worker")
    registry = _registry()
    placed = registry.placement.placement.get(module_id, [])
    declared = registry.declared_permissions(module_id)
    if node_name not in placed or not declared <= set(registry.placement.approvals.get(module_id, [])):
        audit("module.token.issue", "denied", node=node_name, module=module_id, reason="placement_or_approval")
        raise HTTPException(403, "Module is not placed on this node or not approved")
    audit("module.token.issue", "allowed", node=node_name, module=module_id)
    return {"module_id": module_id, "token": issue_token(module_id, node_name)}


# -- secrets ---------------------------------------------------------------------------

def secret_key(module_id: str, name: str) -> str:
    return f"module:{module_id}:{name}"


@router.get("/api/modules/self/secrets")
def module_secrets(request: Request):
    registry = _registry()
    caller = _caller_module(request, registry)
    if caller is None:
        audit("module.secrets", "denied", reason="no_module_token")
        raise HTTPException(401, "Module token is not valid")
    from core.first_run import ensure_secret_store
    stored = ensure_secret_store()
    result = {}
    for item in caller.manifest.secret_refs:
        # Own scope only (enforced by the manifest); never other modules' or platform secrets.
        value = stored.get(secret_key(caller.module_id, item["ref"].split("/", 3)[3]))
        if value:
            result[item["name"]] = value
    audit("module.secrets", "allowed", module=caller.module_id, node=caller.node_id, names=sorted(result))
    return {"secrets": result}


def _storyboard_summary(job) -> dict:
    active = next((item for item in job.storyboards if item.version == job.active_storyboard_version), None)
    return {"job_id": job.job_id, "topic": job.topic, "brand_id": job.brand_id, "character_id": job.character_id,
            "character_owner_brands": sorted(set(job.character_owner_brands
                                                 + ([job.character_owner_brand] if job.character_owner_brand else []))),
            "status": str(getattr(job.status, "value", job.status)), "active_version": job.active_storyboard_version,
            "active_status": active.status if active else None, "versions": len(job.storyboards),
            "scenes": len(active.scenes) if active else 0,
            "scenario_source": job.scenario_source}


def _require_storyboard_module(request: Request):
    registry = _registry()
    caller = _caller_module(request, registry)
    if caller is None or caller.module_id != "storyboard" or "storyboard.read" not in caller.manifest.permissions:
        audit("module.storyboards", "denied", module=caller.module_id if caller else None)
        raise HTTPException(403, "Module may call only APIs it declares")
    return caller


@router.get("/api/modules/self/storyboards")
def module_storyboards(request: Request):
    """#501: the Storyboard module reads the storyboards that live in the jobs (CORE
    stays their owner: versions, revisions, approvals, Worker generation, backup)."""
    from core.state import store
    caller = _require_storyboard_module(request)
    audit("module.storyboards", "allowed", module=caller.module_id)
    return [_storyboard_summary(job) for job in sorted(store.jobs.values(), key=lambda j: j.job_id) if job.storyboards]


@router.get("/api/modules/self/storyboards/{job_id}")
def module_storyboard(job_id: str, request: Request):
    from core.state import store
    caller = _require_storyboard_module(request)
    job = store.jobs.get(job_id)
    if job is None or not job.storyboards:
        raise HTTPException(404, "Storyboard not found")
    artifacts = {item.artifact_id: item for item in job.artifacts}
    versions = []
    for version in job.storyboards:
        scenes = []
        for scene in sorted(version.scenes, key=lambda item: item.index):
            image = artifacts.get(scene.image_artifact_id) if scene.image_artifact_id else None
            scenes.append({"index": scene.index, "duration": scene.duration, "prompt": scene.prompt,
                           "voiceover": scene.voiceover, "image_version": scene.image_version,
                           "image": {"artifact_id": image.artifact_id, "sha256": image.sha256} if image else None})
        versions.append({"version": version.version, "title": version.title, "status": version.status,
                         "image_status": version.image_status, "revision_request": version.revision_request,
                         "decided_by": version.decided_by, "decided_at": version.decided_at, "scenes": scenes})
    audit("module.storyboards", "allowed", module=caller.module_id, job=job_id)
    return {**_storyboard_summary(job), "storyboards": versions}


@router.post("/api/modules/self/storyboards/jobs")
async def module_storyboard_create_job(request: Request):
    """#508: Create an independent CORE Job from a saved Scenario Library version.

    CORE verifies the Gateway-signed user context, re-derives the caller's brands,
    verifies access to the scenario, target brand and referenced characters, and
    creates the Job with an immutable snapshot and idempotency support.
    """
    from core.api.jobs import create_job_from_scenario_snapshot
    from core.security import user_brands
    caller = _require_storyboard_module(request)
    try:
        user, role = verify_user_context(request.headers.get("x-vertep-user-context", ""), caller.module_id)
    except PermissionError as error:
        audit("module.storyboards.create_job", "denied", module=caller.module_id, reason=str(error))
        raise HTTPException(401, "Invalid user context") from error
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, "Request body must be a JSON object")
    scenario_id = str(body.get("scenario_id") or "")
    try:
        version = int(body.get("version") or 0)
    except (TypeError, ValueError) as error:
        raise HTTPException(422, "version must be an integer") from error
    brand_id = str(body.get("brand_id") or "")
    if not scenario_id or version < 1 or not brand_id:
        raise HTTPException(422, "scenario_id, version and brand_id are required")
    # The same bounds as POST /api/jobs/from-scenario: the module is not trusted with the values.
    from core.api.jobs import ScenarioJobCreate
    from pydantic import ValidationError
    try:
        ScenarioJobCreate(**{key: body[key] for key in ScenarioJobCreate.model_fields if key in body})
    except ValidationError as error:
        raise HTTPException(422, "Invalid scenario job request") from error
    brands = user_brands(user, role)
    try:
        job = create_job_from_scenario_snapshot(
            scenario_id=scenario_id,
            version=version,
            snapshot=body.get("snapshot"),
            brand_id=brand_id,
            allowed_brands=brands,
            actor=user,
            character_id=body.get("character_id"),
            topic=body.get("topic"),
            priority=int(body.get("priority", 5)),
            task_type=str(body.get("task_type") or "image"),
            aspect_ratio=str(body.get("aspect_ratio") or "16:9"),
            output_preset=str(body.get("output_preset") or "youtube"),
            idempotency_key=body.get("idempotency_key"),
        )
    except HTTPException as error:
        audit("module.storyboards.create_job", "refused", module=caller.module_id, actor=user,
              scenario=scenario_id, version=version, brand=brand_id, status=error.status_code)
        raise
    audit("module.storyboards.create_job", "allowed", module=caller.module_id, actor=user,
          scenario=scenario_id, version=version, brand=brand_id, job=job.job_id)
    return job.model_dump()


def _publications(job, brand_store_channels=None) -> dict:
    return {"job_id": job.job_id, "topic": job.topic, "brand_id": job.brand_id,
            "status": str(getattr(job.status, "value", job.status)), "active_video_version": job.active_video_version,
            "shared_source": job.shared_source,
            "publications": {channel: {"status": (result or {}).get("status"), "url": (result or {}).get("url"),
                                       "error": (result or {}).get("error")}
                             for channel, result in (job.publication_results or {}).items()},
            "sending": sorted(set((job.publish_task_ids or {}).values()))}


def _require_publishing_module(request: Request, permission: str):
    registry = _registry()
    caller = _caller_module(request, registry)
    if caller is None or caller.module_id != "publishing" or permission not in caller.manifest.permissions:
        audit("module.publications", "denied", module=caller.module_id if caller else None)
        raise HTTPException(403, "Module may call only APIs it declares")
    return caller


@router.get("/api/modules/self/publications")
def module_publications(request: Request):
    """#502: what the Publishing module shows; CORE stays the owner of tasks/statuses/receipts."""
    from core.state import store
    caller = _require_publishing_module(request, "publishing.read")
    audit("module.publications", "allowed", module=caller.module_id)
    return [_publications(job) for job in sorted(store.jobs.values(), key=lambda j: j.job_id)
            if job.active_video_version is not None or job.publication_results]


@router.post("/api/modules/self/publications/{job_id}/publish")
async def module_publish(job_id: str, request: Request):
    """An editor's action from the Publishing module. CORE verifies the Gateway-signed
    user context, re-derives that user's rights on the job's brand and audits the
    real user; the module's own token grants nothing by itself."""
    from core.security import user_brands
    from core.state import store
    caller = _require_publishing_module(request, "publishing.publish")
    try:
        user, role = verify_user_context(request.headers.get("x-vertep-user-context", ""), caller.module_id)
    except PermissionError as error:
        audit("module.publications.publish", "denied", module=caller.module_id, job=job_id, reason=str(error))
        raise HTTPException(401, "Invalid user context") from error
    job = store.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    brands = user_brands(user, role)
    if brands is not None and job.brand_id not in brands:
        audit("module.publications.publish", "denied", module=caller.module_id, job=job_id, actor=user, reason="brand")
        raise HTTPException(403, "Brand access required")
    body = await request.json()
    channels = body.get("channels")
    if not isinstance(channels, list) or not channels or any(not isinstance(item, str) for item in channels):
        raise HTTPException(422, "channels must be a non-empty list")
    from core.api.jobs import publish_job
    try:
        result = publish_job(job_id, channels)
    except HTTPException as error:
        audit("module.publications.publish", "refused", module=caller.module_id, job=job_id, actor=user,
              channels=channels, status=error.status_code)
        raise
    audit("module.publications.publish", "allowed", module=caller.module_id, job=job_id, actor=user, channels=channels)
    return _publications(result)


@router.get("/api/modules/self/observability")
def module_observability(request: Request):
    """#503: what the Monitoring module ingests from the existing collectors.

    CORE stays the source of truth (metrics, the persistent alert store with its
    fire/acknowledge/resolve states, health checks); the module only keeps history.
    No new thresholds or alert rules are evaluated here.
    """
    from datetime import datetime, timezone
    from core.alert_store import get_alert_store
    from core.api import observability
    registry = _registry()
    caller = _caller_module(request, registry)
    if caller is None or caller.module_id != "monitoring" or "monitoring.read" not in caller.manifest.permissions:
        audit("module.observability", "denied", module=caller.module_id if caller else None)
        raise HTTPException(403, "Module may call only APIs it declares")
    observability._reconcile_alerts()
    health = observability.health()
    audit("module.observability", "allowed", module=caller.module_id, node=caller.node_id)
    return {"collected_at": datetime.now(timezone.utc).isoformat(), "metrics": observability.metrics(),
            "alerts": get_alert_store().list(limit=1000),
            "health": {"status": health["status"], "checks": health["checks"]}}


@router.get("/api/modules/self/database")
def module_database(request: Request):
    """Credentials of the module's own schema/role only (#494); LAB has none (#491)."""
    registry = _registry()
    caller = _caller_module(request, registry)
    if caller is None:
        audit("module.database", "denied", reason="no_module_token")
        raise HTTPException(401, "Module token is not valid")
    from core.first_run import ensure_secret_store
    from .service import DATABASE_SECRET
    stored = ensure_secret_store().get(secret_key(caller.module_id, DATABASE_SECRET))
    if caller.manifest.placement["target"] == "isolated_lab" or not stored:
        audit("module.database", "denied", module=caller.module_id, reason="no_module_schema")
        raise HTTPException(404, "Module database is not provisioned")
    credentials = json.loads(stored)
    audit("module.database", "allowed", module=caller.module_id, node=caller.node_id)
    # Address as seen from module containers (may differ from CORE's own DSN host).
    from urllib.parse import urlsplit
    dsn = urlsplit(os.getenv("VERTEP_MODULE_DATABASE_URL") or os.getenv("DATABASE_URL") or "")
    return {**{key: credentials[key] for key in ("role", "schema", "password", "database")},
            "host": os.getenv("VERTEP_MODULE_DATABASE_HOST") or dsn.hostname,
            "port": int(os.getenv("VERTEP_MODULE_DATABASE_PORT") or dsn.port or 5432)}


# -- shared capabilities for modules -------------------------------------------------

@router.post("/api/modules/self/tasks")
async def module_submit_task(request: Request):
    """Run one task on an existing shared Worker capability declared in ``requires``."""
    from core.workflow.runtime import workflow_runtime
    registry = _registry()
    caller = _caller_module(request, registry)
    if caller is None:
        raise HTTPException(401, "Module token is not valid")
    payload = await request.json()
    capability, task_type = str(payload.get("capability", "")), str(payload.get("task_type", ""))
    declared = {r["name"] for r in caller.manifest.requires if r["type"] == "capability"}
    if capability not in declared or not re.fullmatch(r"[a-z][a-z0-9_]*", task_type or "-"):
        audit("module.task", "denied", module=caller.module_id, capability=capability, reason="undeclared_capability")
        raise HTTPException(403, "Module may use only capabilities it declares")
    params = payload.get("input") or {}
    if not isinstance(params, dict) or any(key in params for key in ("module_owner", "owner", "acl")):
        raise HTTPException(422, "Invalid module task input")
    runtime = workflow_runtime()
    if runtime is None:
        # Shared Worker capabilities run through the workflow engine; Workers take
        # those tasks only when dynamic workflows are on.
        raise HTTPException(409, "Shared Worker capabilities need the dynamic workflow mode")
    definition = {"name": f"module:{caller.module_id}", "version": "1.0.0",
                  "tasks": [{"id": "task", "type": task_type, "capabilities_required": [capability],
                             "timeout_minutes": int(payload.get("timeout_minutes", 10))}]}
    from core.system_state import dispatch_allowed
    with runtime.engine._lock:
        job_id = runtime.engine.create_job(definition, {**params, "module_owner": caller.module_id})
        runtime.engine.start_job(job_id, allow_dispatch=dispatch_allowed())
    audit("module.task", "allowed", module=caller.module_id, capability=capability, job=job_id)
    return {"task_id": job_id}


@router.get("/api/modules/self/tasks/{task_id}")
def module_task_status(task_id: str, request: Request):
    from core.workflow.runtime import workflow_runtime
    registry = _registry()
    caller = _caller_module(request, registry)
    if caller is None:
        raise HTTPException(401, "Module token is not valid")
    runtime = workflow_runtime()
    job = runtime.engine._jobs.get(task_id) if runtime is not None else None
    if job is None or job["params"].get("module_owner") != caller.module_id:
        audit("module.task.read", "denied", module=caller.module_id, job=task_id)
        raise HTTPException(404, "Module task not found")
    runtime.tick()
    task = job["tasks"]["task"]
    return {"task_id": task_id, "status": job["status"], "output": task.get("output"), "error": task.get("error")}


# -- node assignments for Worker supervisors ---------------------------------------------

@router.post("/api/modules/node-assignments")
async def node_assignments(request: Request):
    """Signed packages of application modules placed and approved for this node."""
    from core.security import _valid_worker_request
    payload = await request.json()
    node_name = str(payload.get("node_name", ""))
    if not node_name or not _valid_worker_request(node_name, request):
        audit("module.assignments", "denied", node=node_name, reason="worker_auth")
        raise HTTPException(401, "Token is not valid for this worker")
    from core.workflow.runtime import module_runtime
    runtime = module_runtime()
    if runtime is None:
        return {"modules": []}
    registry = runtime.modules
    from core.module.update_manager import RolloutGates
    gates = RolloutGates(runtime.lifecycle.root / "rollout-gates.json")
    result = []
    for module_id, record in runtime.lifecycle.records().items():
        if node_name not in registry.placement.placement.get(module_id, []):
            continue
        manifest = runtime.lifecycle._manifest(record)
        if manifest.kind != "application" or record["state"] not in ("INSTALLED", "ACTIVE", "INACTIVE"):
            continue
        if not set(manifest.permissions) <= set(registry.placement.approvals.get(module_id, [])):
            continue
        package, version = record["package"], record["version"]
        if not gates.allowed(module_id, node_name) and record.get("previous"):
            # #512 sequential rollout: this node keeps the previous version until released.
            package, version = record["previous"]["package"], record["previous"]["version"]
        result.append({"module_id": module_id, "version": version, "package": package})
    audit("module.assignments", "allowed", node=node_name, modules=[item["module_id"] for item in result])
    return {"modules": result}


def store_module_secrets(registry, module_id: str, values: dict) -> list[str]:
    from core.first_run import _secret_lock, _write_encrypted_secrets, ensure_secret_store
    declared = {item["ref"].split("/", 3)[3] for i in registry.instances(module_id) for item in i.manifest.secret_refs}
    declared.discard("__database__")  # platform-managed, never set through this route
    if not isinstance(values, dict) or not values or set(values) - declared or any(not isinstance(v, str) for v in values.values()):
        raise ValueError("Only declared secret references of this module may be stored")
    with _secret_lock:
        stored = ensure_secret_store()
        stored.update({secret_key(module_id, name): value for name, value in values.items()})
        _write_encrypted_secrets(stored)
    return sorted(values)


# -- UI extensions -----------------------------------------------------------------

@router.get("/api/modules/ui-extensions")
def ui_extensions():
    registry = _modules_or_none()
    if registry is None:
        # No module registry configured means no extensions, not an error for the panel.
        return {"extensions": []}
    result = []
    for instance in registry.instances():
        manifest = instance.manifest
        if instance.state != "READY" or not manifest.ui or not manifest.ui["files"]:
            continue
        api = manifest.api or {}
        result.append({"module_id": manifest.module_id, "version": manifest.version,
                       "navigation": manifest.ui["navigation"],
                       "url": f"/api/modules/ui/{manifest.module_id}/{manifest.version}/{manifest.ui['entry']}",
                       # The only API the panel relays for this UI (it has no network of its own).
                       "api_base": (f"/api/modules/{api['namespace']}/v{str(api['version']).split('.')[0]}"
                                    if api.get("namespace") else None)})
    return {"extensions": sorted(result, key=lambda item: item["module_id"])}


@router.get("/api/modules/navigation")
def module_navigation():
    """Menu entries of READY modules; the panel shows nothing else for module pages."""
    from .navigation import navigation
    return navigation()


@router.get("/api/modules/ui/{module_id}/{version}/{path:path}")
def ui_asset(module_id: str, version: str, path: str):
    registry = _registry()
    instance = _ready_instance(registry, module_id)
    manifest = instance.manifest if instance else None
    if manifest is None or manifest.version != version or not manifest.ui or path not in manifest.ui["files"]:
        audit("module.ui", "denied", module=module_id, path=path, reason="not_ready_or_undeclared")
        raise HTTPException(404, "Module UI is not available")
    try:
        # The module answers only CORE: the asset request carries its module token.
        upstream = httpx.get(f"{instance.endpoint}/ui/{path}", timeout=10,
                             headers={"x-vertep-module-token": issue_token(instance.module_id, instance.node_id)})
    except httpx.HTTPError:
        audit("module.ui", "failed", module=module_id, path=path, reason="unreachable")
        raise HTTPException(502, "Module UI is unavailable")
    if upstream.status_code != 200 or hashlib.sha256(upstream.content).hexdigest() != manifest.ui["files"][path]:
        audit("module.ui", "denied", module=module_id, path=path, reason="digest_mismatch")
        raise HTTPException(502, "Module UI does not match its signed digest")
    audit("module.ui", "allowed", module=module_id, path=path)
    media = "text/html; charset=utf-8" if path.endswith(".html") else (
        "text/javascript" if path.endswith(".js") else "text/css" if path.endswith(".css") else "application/octet-stream")
    return Response(upstream.content, media_type=media,
                    headers={"Content-Security-Policy": UI_CSP, "X-Frame-Options": "SAMEORIGIN"})


# -- API gateway -------------------------------------------------------------------

@router.api_route("/api/modules/{namespace}/v{major}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def module_api(namespace: str, major: int, path: str, request: Request):
    registry = _registry()
    caller = _caller_module(request, registry)
    actor = getattr(request.state, "vertep_identity", None)
    who = {"caller_module": caller.module_id} if caller else {"actor": actor[0] if actor else None}
    target = None
    for instance in registry.instances():
        if instance.state == "READY" and instance.manifest.api and instance.manifest.api["namespace"] == namespace:
            target = instance
            break
    route = "/" + path
    if target is None:
        audit("module.api", "denied", namespace=namespace, path=route, method=request.method, reason="not_ready", **who)
        raise HTTPException(404, "Module API is not available")
    api = target.manifest.api
    if _version_tuple(api["version"])[0] != major:
        audit("module.api", "denied", namespace=namespace, path=route, method=request.method, reason="version", **who)
        raise HTTPException(404, "Module API version is not available")
    if not _route_matches(api["routes"], route):
        audit("module.api", "denied", namespace=namespace, path=route, method=request.method, reason="undeclared_route", **who)
        raise HTTPException(404, "Module API route is not declared")
    if caller is not None:
        have = _version_tuple(api["version"])
        allowed = any(r["type"] == "api" and r["name"] == namespace
                      and _version_tuple(r["min_version"]) <= have
                      and ("max_version" not in r or have <= _version_tuple(r["max_version"]))
                      for r in caller.manifest.requires)
        if not allowed or caller.module_id == target.module_id:
            audit("module.api", "denied", namespace=namespace, path=route, method=request.method, reason="undeclared_dependency", **who)
            raise HTTPException(403, "Module may call only APIs it declares")
    headers = {key: value for key, value in request.headers.items() if key.lower() in _FORWARD_HEADERS}
    # The target verifies these came from CORE with its own module token.
    headers["x-vertep-module-token"] = issue_token(target.module_id, target.node_id)
    if caller is not None:
        headers["x-vertep-caller-module"] = caller.module_id
    elif actor:
        headers["x-vertep-user"], headers["x-vertep-role"] = actor
        headers["x-vertep-user-context"] = issue_user_context(actor[0], actor[1], target.module_id)
        brands = getattr(request.state, "vertep_brands", set())
        # "*" = administrator (every brand); otherwise the moderator's own brands.
        headers["x-vertep-brands"] = "*" if brands is None else ",".join(sorted(brands or ()))
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            upstream = await client.request(request.method, f"{target.endpoint}{route}", params=request.query_params,
                                            content=await request.body(), headers=headers)
    except httpx.HTTPError:
        audit("module.api", "failed", namespace=namespace, path=route, method=request.method, reason="unreachable", **who)
        raise HTTPException(502, "Module API is unavailable")
    audit("module.api", "allowed", namespace=namespace, path=route, method=request.method, status=upstream.status_code, **who)
    return Response(upstream.content, status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "application/json"))
