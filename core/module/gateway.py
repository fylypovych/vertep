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
_FORWARD_HEADERS = {"content-type", "accept", "accept-language"}


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


def token_identity(token: str) -> tuple[str, str] | None:
    try:
        module_id, node_id, signature = base64.urlsafe_b64decode(token.encode()).decode().split("|")
    except (ValueError, UnicodeError):
        return None
    expected = hmac.new(_token_key(), f"{module_id}|{node_id}".encode(), hashlib.sha256).hexdigest()
    return (module_id, node_id) if hmac.compare_digest(signature, expected) else None


# -- helpers -------------------------------------------------------------------------

def _registry():
    from core.workflow.runtime import workflow_runtime
    runtime = workflow_runtime()
    if runtime is None:
        raise HTTPException(409, "Workflow templates are not configured")
    return runtime.modules


def _ready_instance(registry, module_id: str, node_id: str | None = None):
    for instance in registry.ready_instances(module_id):
        if node_id is None or instance.node_id == node_id:
            return instance
    return None


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
    declared = {p for i in registry.instances(module_id) for p in i.manifest.permissions}
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
    return {key: credentials[key] for key in ("role", "schema", "password", "database")}


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
    from core.workflow.runtime import workflow_runtime
    runtime = workflow_runtime()
    if runtime is None:
        # No module registry configured means no extensions, not an error for the panel.
        return {"extensions": []}
    registry = runtime.modules
    result = []
    for instance in registry.instances():
        manifest = instance.manifest
        if instance.state != "READY" or not manifest.ui or not manifest.ui["files"]:
            continue
        result.append({"module_id": manifest.module_id, "version": manifest.version,
                       "navigation": manifest.ui["navigation"],
                       "url": f"/api/modules/ui/{manifest.module_id}/{manifest.version}/{manifest.ui['entry']}"})
    return {"extensions": sorted(result, key=lambda item: item["module_id"])}


@router.get("/api/modules/ui/{module_id}/{version}/{path:path}")
def ui_asset(module_id: str, version: str, path: str):
    registry = _registry()
    instance = _ready_instance(registry, module_id)
    manifest = instance.manifest if instance else None
    if manifest is None or manifest.version != version or not manifest.ui or path not in manifest.ui["files"]:
        audit("module.ui", "denied", module=module_id, path=path, reason="not_ready_or_undeclared")
        raise HTTPException(404, "Module UI is not available")
    try:
        upstream = httpx.get(f"{instance.endpoint}/ui/{path}", timeout=10)
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
