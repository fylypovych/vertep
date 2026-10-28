"""Real System API routes: update, recovery, roles, models, certificates,
operations and test (Issue #110 Phase C).

Extracted verbatim from ``core/app.py``: the same paths, methods and response
codes. The actual logic lives here; ``core/app.py`` only re-registers the
router and delegates its old symbols to these implementations.
"""
from __future__ import annotations

import json
import os
import secrets
import re
import uuid
import base64 as basic64
import binascii
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..certificates import list_certificates
from ..deployment_plan import create_plan
from ..first_run import config_root, installation
from ..models import (JobStatus, RollingUpdateRequest, CoordinatorUpdateRequest,
                      StageStatus, utc_now)
from ..node_registry import node_roles, registered_nodes
from ..operations import audit_entry
from ..pull_executor import PlacementError, cancel_pull, start_delete, start_pull
from ..rolling_update import (cancel_rollout, is_core_node, promote_rollout,
                              reconcile_rollout, rollback_ready_nodes,
                              rollout_status, start_rollout,
                              update_core_coordinator)
from ..runtime_identity import CORE_RUNTIME_INSTANCE_ID
from ..security import (_authenticate_user, _valid_session,
                        _valid_worker_request)
from ..state import store, task_queue
from ..system_state import (SystemState, dispatch_allowed, get_system_state,
                            set_system_state)
from ..update_manager import request_update, update_status
from ..version import application_version
from adapters.providers import provider_matrix

router = APIRouter()


def _read_optional_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


@router.get("/api/system/update")
def web_update_status():
    return {**update_status(), "system": get_system_state()}


@router.post("/api/system/recovery/normal")
def recover_normal_operation():
    from .status import system_status as _full_status
    status = _full_status()
    failed = [name for name in ("core", "postgres", "redis") if status.get(name) != "OK"]
    if failed:
        raise HTTPException(409, "Recovery health checks failed: " + ", ".join(failed))
    return set_system_state(SystemState.NORMAL, "Administrator confirmed healthy runtime recovery")


@router.get("/api/system/state")
def web_system_state():
    """Issue #168.2: the immutable API method/path contract for system state.

    GET returns the current state; POST sets it.  Both paths are pinned by an
    executable request so the route classification matrix cannot drift.
    """
    return get_system_state()


@router.post("/api/system/state")
def system_state(payload: dict, request: Request):
    """Set the system state (used by the Backup Node to enter EMERGENCY)."""
    reason = payload.get("reason", "manual update")
    target = payload.get("state", "EMERGENCY")
    if target not in {"EMERGENCY", "MAINTENANCE", "RECOVERING", "NORMAL"}:
        raise HTTPException(422, "Unsupported restore state")
    if target != "EMERGENCY":
        key = os.getenv("INTERNAL_API_KEY", "")
        if not key or not secrets.compare_digest(request.headers.get("x-vertep-internal-key", ""), key):
            raise HTTPException(403, "Restore state requires internal authentication")
    if target == "NORMAL":
        from ..health_checks import run_checks, health_status
        if health_status(run_checks("core")) != "HEALTHY":
            raise HTTPException(409, "Post-restore health checks failed")
    return set_system_state(SystemState(target), reason, payload.get("operation_id"))


@router.get("/api/system/installation-manifest")
def installation_manifest():
    """Public installation manifest, without the administrator block."""
    manifest_path = config_root() / "installation-manifest.json"
    if manifest_path.is_file():
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise HTTPException(503, "Installation manifest is unreadable") from error
    return {key: item for key, item in installation().items() if key != "administrator"}


@router.get("/api/system/roles")
def local_roles_status():
    definitions = node_roles()
    plan = _read_optional_json(config_root() / "deployment-plan.json")
    deployment = _read_optional_json(config_root() / "deployment-status.json")
    request = _read_optional_json(config_root() / "deployment-request.json")
    active = plan.get("additional_roles", []) if plan.get("role") == "core" else []
    queued = bool(request)
    # Measured runtime status per role contract, not a static declaration.
    from ..role_runtime import role_runtime_matrix
    runtime = {item["role"]: item for item in role_runtime_matrix()}
    return {
        "node_role": plan.get("role") or installation().get("node_role") or "core",
        "active_roles": active,
        "available_roles": [
            {"id": role, "label": definition.get("label", role),
             "services": [service for service in definition.get("services", [])
                          if service not in {"worker", "update-agent"}],
             "capabilities": definition.get("capabilities", []),
             "modules": definition.get("modules", []),
             "runtime_status": (runtime.get(role) or {}).get("runtime_status", "UNKNOWN"),
             "runtime_evidence": (runtime.get(role) or {}).get("evidence", {})}
            for role, definition in definitions.items() if role != "core"
            and isinstance(definition, dict)
        ],
        "role_runtime_status": {role: item["runtime_status"] for role, item in runtime.items()},
        "deployment": deployment,
        "queued": queued,
    }


@router.post("/api/system/roles")
def configure_local_roles(payload: dict):
    requested = payload.get("roles")
    if not isinstance(requested, list) or any(not isinstance(role, str) for role in requested):
        raise HTTPException(422, "roles must be a list of role identifiers")
    definitions = node_roles()
    try:
        plan = create_plan(definitions, "core", application_version(), requested)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    current = _read_optional_json(config_root() / "deployment-plan.json")
    if current.get("sha256") == plan["sha256"]:
        return {"state": "UNCHANGED", "active_roles": plan.get("additional_roles", []),
                "message": "Вибрані ролі вже активні"}
    setup = installation()
    backend = setup.get("ai_backend", {})
    request_value = {
        "schema": 1, "role": "core", "additional_roles": plan.get("additional_roles", []),
        "version": application_version(), "ai_backend": backend.get("type") or "ollama",
        "core_url": None, "plan_sha256": plan["sha256"],
        "ollama_model": backend.get("model") or os.getenv("OLLAMA_MODEL", "llama3.2"),
    }
    path = config_root() / "deployment-request.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(request_value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return {"state": "QUEUED", "active_roles": current.get("additional_roles", []),
            "requested_roles": plan.get("additional_roles", []),
            "message": "Зміни передано системному виконавцю"}


@router.get("/api/system/update/readiness")
def update_readiness():
    persisted_jobs_by_id = {job.job_id: job for job in store.repository.load_jobs()}
    persisted_jobs_by_id.update(store.jobs)
    persisted_jobs = list(persisted_jobs_by_id.values())
    active = [job.job_id for job in persisted_jobs if job.status not in {
        JobStatus.NEW, JobStatus.WAITING_FOR_SYSTEM, JobStatus.READY, JobStatus.PUBLISHED, JobStatus.FAILED,
        JobStatus.PAUSED, JobStatus.CANCELLED}]
    persisted_workers_by_name = {worker["node_name"]: worker for worker in store.load_workers()}
    persisted_workers_by_name.update(store.workers)
    persisted_workers = list(persisted_workers_by_name.values())
    system = get_system_state()
    operation_id = system.get("operation_id")
    acknowledged = []
    unacknowledged = []
    known = {worker.get("node_id") or worker.get("node_name"): worker
             for worker in persisted_workers}
    registered = [node for node in registered_nodes() if not node.get("revoked_at")]
    required_nodes = {node.get("node_id") for node in registered if node.get("node_id")}
    if not required_nodes:
        required_nodes.update(known)
    busy = [node_id for node_id in sorted(required_nodes) if known.get(node_id)
            and (known[node_id].get("status") == "BUSY" or known[node_id].get("current_task"))]
    for node_id in sorted(required_nodes):
        worker = known.get(node_id)
        if worker is None:
            unacknowledged.append(node_id)
            continue
        worker.update({"desired_state": "DRAINING", "drain_operation_id": operation_id})
        store.save_worker(worker)
        drained = (worker.get("status") == "DRAINING" and not worker.get("current_task"))
        (acknowledged if drained else unacknowledged).append(node_id)
    ready = (not active and not busy and task_queue.inflight_depth() == 0
             and not unacknowledged and not dispatch_allowed())
    return {"ready": ready,
            "active_jobs": active, "busy_workers": busy,
            "queue_paused": not dispatch_allowed(), "inflight": task_queue.inflight_depth(),
            "drain_operation_id": operation_id, "acknowledged_workers": acknowledged,
            "unacknowledged_workers": unacknowledged}


@router.post("/api/system/update/check")
def web_update_check():
    try:
        return request_update("check")
    except (RuntimeError, FileExistsError) as error:
        raise HTTPException(409, str(error)) from error
    except OSError as error:
        raise HTTPException(503, f"Update agent state directory is unavailable: {error}") from error


@router.post("/api/system/update/run")
def web_update_run():
    try:
        return request_update("update")
    except (RuntimeError, FileExistsError) as error:
        raise HTTPException(409, str(error)) from error
    except OSError as error:
        raise HTTPException(503, f"Update agent state directory is unavailable: {error}") from error


@router.post("/api/system/update/restart")
def web_server_restart():
    try:
        return request_update("restart")
    except (RuntimeError, FileExistsError) as error:
        raise HTTPException(409, str(error)) from error
    except OSError as error:
        raise HTTPException(503, f"Update agent state directory is unavailable: {error}") from error


@router.get("/api/system/update/rolling")
def rolling_update_status():
    return rollout_status()


@router.post("/api/system/update/rolling/cancel")
def cancel_rolling_update():
    return cancel_rollout()


@router.post("/api/system/update/rolling/rollback")
def rollback_canary():
    result = rollback_ready_nodes(store.workers)
    reconcile_rollout(store.workers)
    return result


@router.post("/api/system/update/rolling/promote")
def promote_canary():
    try:
        return promote_rollout()
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/api/system/update/rolling")
def begin_rolling_update(payload: RollingUpdateRequest):
    registered = {node["node_id"] for node in registered_nodes() if not node.get("revoked_at")}
    unknown = sorted(node for node in set(payload.node_ids) - registered
                     if not is_core_node(node))
    if unknown:
        raise HTTPException(422, f"Unknown or revoked nodes: {', '.join(unknown)}")
    try:
        rollout = start_rollout(payload.target_version, payload.node_ids, payload.order,
                                payload.update_timeout_seconds, payload.canary)
        reconcile_rollout(store.workers)
        return rollout
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/api/system/update/rolling/coordinator")
def rolling_update_coordinator(payload: CoordinatorUpdateRequest):
    try:
        return update_core_coordinator(payload.node_id, payload.updates)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@router.get("/api/node/status/{node_name}")
def node_system_status(node_name: str, request: Request):
    if not _valid_worker_request(node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    from .status import system_status as _full_status
    return _full_status()


@router.post("/api/system/restart")
async def system_restart(payload: dict | None = None):
    """Restart CORE services or a specific worker node.

    Telegram and Web UI both call this endpoint — no duplicated logic.
    """
    import asyncio
    import re as _re
    import subprocess
    import uuid
    from ..operations import audit_entry
    body = payload or {}
    target = str(body.get("target", "core"))
    if target == "core":
        previous_instance_id = CORE_RUNTIME_INSTANCE_ID
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(["systemctl", "restart", "vertep-core.service"],
                                       capture_output=True, timeout=30),
            )
            if result.returncode != 0:
                stderr = result.stderr.decode(errors="replace").strip() if result.stderr else "no stderr"
                raise HTTPException(500, f"systemctl restart failed (exit {result.returncode}): {stderr}")
            set_system_state(SystemState.NORMAL, "CORE restart requested via Telegram", None)
            return {"target": "core", "status": "restart_requested",
                    "previous_runtime_instance_id": previous_instance_id,
                    "message": "vertep-core.service restart initiated"}
        except FileNotFoundError:
            raise HTTPException(503, "systemctl is unavailable; CORE restart was not initiated")
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "CORE restart timed out")
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(500, f"CORE restart failed: {error}") from error
    node_id = str(body.get("node_id", "")).strip()
    if not node_id or not _re.fullmatch(r"[a-z0-9-]+", node_id):
        raise HTTPException(422, "node_id is required for worker restart")
    worker = store.workers.get(node_id)
    if not worker:
        raise HTTPException(404, f"Worker {node_id} is not registered or offline")
    worker["desired_state"] = "RESTARTING"
    worker["status"] = "UPDATING"
    worker["restart_operation_id"] = uuid.uuid4().hex
    worker["restart_previous_instance_id"] = worker.get("runtime_instance_id")
    worker["state_reason"] = "restart via Telegram API"
    worker["state_changed_at"] = utc_now()
    store.save_worker(worker)
    audit_entry(worker["desired_state"], "restart", f"node={node_id}", f"telegram:{body.get('requested_by', 'telegram')}")
    return {"target": "node", "node_id": node_id, "status": "restart_requested",
            "restart_operation_id": worker["restart_operation_id"],
            "message": f"Worker {node_id} marked for restart"}


# ---------------------------------------------------------------------------
# System models (Ollama) routes
# ---------------------------------------------------------------------------


@router.get("/api/system/models")
async def system_models():
    """Local Ollama model list plus the per-node placement/readiness view."""
    try:
        local = await _internal_api("GET", "OLLAMA_URL", "/api/tags")
    except Exception:  # noqa: BLE001 - placement stays usable without local Ollama
        local = None
    if not isinstance(local, dict):
        local = {}
    local.setdefault("models", [])
    from ..pull_executor import model_placement
    local["nodes"] = model_placement()
    return local


@router.get("/api/system/models/nodes")
async def system_model_nodes():
    """Per-node model catalog placement, readiness and cache freshness."""
    from ..pull_executor import model_placement
    return {"nodes": model_placement()}


def _actor_from_request(request: Request) -> tuple[str, str]:
    """Return (actor, role) for the authenticated request, or ('unknown', '')."""
    identity = _valid_session(request.cookies.get("vertep_session", ""))
    if identity:
        return identity
    header = request.headers.get("authorization", "")
    try:
        scheme, encoded = header.split(" ", 1)
        user, supplied = basic64.b64decode(encoded).decode().split(":", 1)
        role = _authenticate_user(user, supplied) if scheme.lower() == "basic" else None
        if role:
            return user, role
    except (ValueError, UnicodeError, binascii.Error):
        pass
    return "unknown", ""


@router.post("/api/system/models/pull")
async def pull_system_model(request: Request):
    """Start an async model pull and return the durable operation record.

    The actual Ollama pull runs either locally (background thread) or on a
    placed Text node (heartbeat command channel); poll
    ``GET /api/operations/{operation_id}`` for progress.  Idempotent: a second
    request for the same ``(node, model)`` returns the existing operation.
    """
    payload = await request.json()
    name = str(payload.get("name", ""))
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._:/+-]{0,127}", name):
        raise HTTPException(422, "Invalid model name")
    node = str(payload.get("node") or "").strip() or None
    from ..pull_executor import PlacementError, start_pull
    actor, _role = _actor_from_request(request)
    try:
        return start_pull(name, requested_by=actor, node_name=node)
    except PlacementError as error:
        raise HTTPException(error.status_code, error.message) from error


@router.post("/api/system/models/pull/{operation_id}/cancel")
async def cancel_pull_model(operation_id: str):
    from ..pull_executor import cancel_pull
    operation = cancel_pull(operation_id, reason="cancelled via API")
    if operation is None:
        raise HTTPException(404, "Operation not found or already terminal")
    return operation


@router.delete("/api/system/models/{name:path}")
async def delete_system_model(name: str, request: Request, node: str | None = None):
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._:/+-]{0,127}", name):
        raise HTTPException(422, "Invalid model name")
    if node:
        from ..pull_executor import PlacementError, start_delete
        actor, _role = _actor_from_request(request)
        try:
            return start_delete(name, requested_by=actor, node_name=node)
        except PlacementError as error:
            raise HTTPException(error.status_code, error.message) from error
    return await _internal_api("DELETE", "OLLAMA_URL", "/api/delete", {"name": name})


# ---------------------------------------------------------------------------
# System test route
# ---------------------------------------------------------------------------


@router.post("/api/system/test")
async def system_test(payload: dict | None = None):
    """Run system self-test (Quick or Full).

    Quick test covers CORE API, PostgreSQL, Redis, Dispatcher, storage.
    Full test additionally verifies Text/Voice/GPU Workers, Backup, Monitoring,
    provider matrix, certificates, storage, task results.
    """
    body = payload or {}
    scope = str(body.get("scope", "quick")).lower()
    if scope not in {"quick", "full"}:
        raise HTTPException(422, "scope must be 'quick' or 'full'")
    from ..health_checks import health_status, run_checks
    checks = run_checks(role=os.getenv("NODE_ROLE", "core"))
    result = {"scope": scope, "result": health_status(checks), "checks": checks}
    if scope == "full":
        try:
            nodes = registered_nodes()
            result["nodes"] = [{"node_id": n.get("node_id"), "role": n.get("role"),
                                "status": n.get("status")} for n in nodes]
        except Exception as error:
            result["nodes_error"] = str(error)

        # Enhanced full self-test checks
        try:
            from adapters.providers import provider_matrix
            result["provider_matrix"] = provider_matrix()
        except Exception as error:
            result["provider_matrix_error"] = str(error)

        try:
            from ..certificates import list_certificates
            certs = list_certificates()
            result["certificates"] = {
                "total": len(certs),
                "expiring_soon": sum(1 for c in certs if c.get("expires_in_days", 999) < 30),
                "expired": sum(1 for c in certs if c.get("expires_in_days", 999) < 0)
            }
        except Exception as error:
            result["certificates_error"] = str(error)

        try:
            from ..first_run import default_storage_root
            storage_root = default_storage_root()
            storage_root.mkdir(parents=True, exist_ok=True)
            probe = storage_root / f".self-test-{uuid.uuid4().hex}.tmp"
            with probe.open("wb") as handle:
                handle.write(b"vertep-storage-self-test")
                handle.flush()
                os.fsync(handle.fileno())
            probe.unlink()
            result["storage"] = {
                "writable": True,
                "execution_probe": "write-fsync-delete",
                "path": str(storage_root)
            }
        except Exception as error:
            result["storage_error"] = str(error)

        try:
            from core.queue import TaskQueue
            task_queue = TaskQueue()
            from core.state import store
            recent_jobs = list(store.jobs.values())[-50:] if store.jobs else []
            task_stats = {"completed": 0, "failed": 0, "pending": 0}
            for job in recent_jobs:
                scenes = job.scenes if hasattr(job, "scenes") else []
                for scene in scenes:
                    status = scene.status.value if scene.status else ""
                    if status == StageStatus.READY.value:
                        task_stats["completed"] += 1
                    elif status == StageStatus.FAILED.value:
                        task_stats["failed"] += 1
                    elif status in {StageStatus.PENDING.value, StageStatus.RUNNING.value,
                                    StageStatus.PAUSED.value}:
                        task_stats["pending"] += 1
            result["task_results"] = task_stats
        except Exception as error:
            result["task_results_error"] = str(error)

        # Align final result with actual provider/certificate/storage/task failures:
        # inventory zeros (0 providers/certs), errors, unwritable storage,
        # and task result failures all downgrade the result to UNHEALTHY.
        full_failures = []
        if "provider_matrix_error" in result:
            full_failures.append("provider_matrix")
        elif isinstance(result.get("provider_matrix"), dict):
            pm = result["provider_matrix"]
            if isinstance(pm, dict) and len(pm) == 0:
                full_failures.append("provider_matrix(empty)")
            else:
                unavailable = [name for name, provider in pm.items()
                               if isinstance(provider, dict)
                               and provider.get("configured") is False]
                if unavailable:
                    full_failures.append("providers(unavailable: " + ", ".join(unavailable) + ")")
        if "certificates_error" in result:
            full_failures.append("certificates")
        elif isinstance(result.get("certificates"), dict):
            certs = result["certificates"]
            if isinstance(certs, dict) and (certs.get("total", 0) == 0):
                full_failures.append("certificates(zero)")
            elif isinstance(certs, dict) and certs.get("expired", 0) > 0:
                full_failures.append(f"certificates({certs['expired']} expired)")
        if "storage_error" in result:
            full_failures.append("storage")
        elif isinstance(result.get("storage"), dict) and not result["storage"].get("writable", False):
            full_failures.append("storage(unwritable)")
        if "task_results_error" in result:
            full_failures.append("task_results")
        elif isinstance(result.get("task_results"), dict):
            ts = result["task_results"]
            if isinstance(ts, dict) and ts.get("failed", 0) > 0:
                full_failures.append(f"task_results({ts['failed']} failed)")
        if full_failures:
            result["result"] = "UNHEALTHY"
            result["full_test_failures"] = full_failures

    return result

