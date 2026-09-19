import os
import json
import base64
import binascii
import socket
import secrets
import base64 as basic64
import asyncio
import time
import shutil
import hmac
import hashlib
import io
import zipfile
import threading
import re
import uuid
import subprocess
from functools import wraps
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import httpx
import psutil
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from .models import (Job, JobCreate, JobUpdate, JobStatus, StageName, StageStatus,
                     WorkerHeartbeat, TaskClaim, TaskRenew, TaskResult, worker_transition_allowed,
                     WorkerLogBatch, NodeAction, IntegrationSecretUpdate, TelegramSetup,
                     RollingUpdateRequest, utc_now, Channel, ChannelCreate, ChannelUpdate, CHANNEL_TYPES)
from .dispatcher import available_worker, can_retry
from .pipeline import JobStore, prepare_job_safe, finalize_job_safe
from .queue import TaskQueue
from .orchestration import (all_scenes_ready, cancel_scene, fail_scene, finish_scene,
                            initialize_plan, interrupt_scene, pending_scenes, start_scene,
                            transition_stage)
from .artifacts import register_artifact, verify_artifacts, write_manifest
from .file_validation import validate_signature
from .configuration import CharacterConfig, BrandConfig, SAFE_ID, load_character, save_character, read_json
from .logging_config import configure_logging, read_logs
from .workflows import WorkflowRegistry
from .maintenance import cleanup_jobs, cleanup_temporary_files
from .update_manager import request_update, update_status
from .system_state import (SystemState, dispatch_allowed, get_system_state,
                           jobs_may_be_created, operation_allowed, set_system_state)
from .health_checks import run_checks as _run_health_checks, health_status as _health_status
from .first_run import (complete_setup, configured_user, is_configured, session_secret,
                        setup_status, config_root, integration_secret_status,
                        set_integration_secret, installation, load_all_users,
                        save_user, user_store)
from .telegram_store import (get_admin_chat_ids, get_allowed_chat_ids, is_allowed_chat,
                             is_admin_chat, load_telegram_settings, save_telegram_settings)
from .operations import (create_operation, get_operation, list_operations,
                         get_or_create_operation,
                         begin_operation, advance_operation, complete_operation, fail_operation,
                         is_operation_in_progress, audit_entry, OperationStatus)
from .update_manager import request_update, update_status
from .pipeline import regenerate_script
from .node_registry import (create_node_csr, create_registration_token, enroll_node, node_roles,
                            registered_nodes, renew_node, revoke_node, verify_node_certificate,
                            verify_node_token, write_node_crl)
from .version import application_version
from .deployment_plan import create_plan
from .rolling_update import (cancel_rollout, promote_rollout, reconcile_rollout,
                             rollout_status, rollback_ready_nodes, start_rollout)
from worker.role_executor import delete_text_model, list_text_models, list_voices, pull_text_model, synthesize_voice
from adapters.telegram import TelegramAdapter, TelegramPollingService, _integration_secret
from adapters.providers import providers, provider_matrix
from .api.workflows import router as workflows_router
from .api.resources import router as resources_router
from .api.settings import router as settings_router
from .api.models import router as models_router
from .api.setup import router as setup_router, _validate_ai_backend, first_run_complete
from .api.storyboards import router as storyboards_router
from .storyboard import StoryboardConflict, StoryboardService

from .api.jobs import (router as jobs_router, cancel_job, retry_job,
                       approve_job, publish_job)
from .api.tasks import router as tasks_router
from .api.workers import router as workers_router, workers
from .api.job_helpers import (_job_is_due, _prepare_and_dispatch,
                              _recover_stale_workers, _scene_for_task)
from .api.nodes import (router as nodes_router,
    registration_token, register_node, nodes, control_node, disable_node, renew_node_credentials)
from .api.observability import (router as observability_router,
    health, watchdog_report, health_history, security_check, logs, ingest_logs, metrics, prometheus_metrics, alerts, maintenance_cleanup)
from .api.real_tests import router as real_tests_router
from .security import (_authenticate_user, _hash_secret, _session_token, _valid_session,
                       _valid_worker_request, _valid_worker_token, _verify_hash, _worker_tokens)


@asynccontextmanager
async def lifespan(_app):
    global telegram_polling_service
    # Issue 32: migrate ephemeral characters/brands/workflows to persistent storage on startup.
    try:
        from .persistent_data import ensure_persistent_user_data, persistent_characters_root, persistent_brands_root, workflows_persistent_root
        res = ensure_persistent_user_data()
        os.environ.setdefault("CHARACTERS_ROOT", str(persistent_characters_root()))
        os.environ.setdefault("BRANDS_ROOT", str(persistent_brands_root()))
        os.environ.setdefault("WORKFLOWS_ROOT", str(workflows_persistent_root()))
        # Re-point the already-imported registry if it was created with the old fallback.
        try:
            from .state import workflow_registry as _wr
            env_wf = os.getenv("WORKFLOWS_ROOT")
            if env_wf and str(_wr.root) != env_wf:
                _wr.root = env_wf
        except Exception:
            pass
        try:
            from .state import logger as _lg
            _lg.info("Persistent user-data ensured at startup: %s", res)
        except Exception:
            pass
    except Exception as _e:
        try:
            from .state import logger as _lg2
            _lg2.warning("Persistent user-data ensure failed: %s", _e)
        except Exception:
            pass
    if os.getenv("NODE_MTLS_REQUIRED", "false").lower() == "true":
        write_node_crl()
    for recovered_job in list(store.jobs.values()):
        if (recovered_job.status in {JobStatus.NEW, JobStatus.WAITING_FOR_SYSTEM}
                and dispatch_allowed() and _job_is_due(recovered_job)):
            if recovered_job.status == JobStatus.WAITING_FOR_SYSTEM:
                store.update(recovered_job, JobStatus.NEW, "SYSTEM RETURNED TO NORMAL")
            executor.submit(_prepare_and_dispatch, recovered_job)
    watchdog_task = asyncio.create_task(_watchdog())
    _start_telegram_polling()
    yield
    watchdog_task.cancel()
    _stop_telegram_polling()

app = FastAPI(title="Vertep CORE", version=application_version(), lifespan=lifespan)
from .state import (store, executor, task_queue, logger, workflow_registry,
                    request_windows, setup_request_windows, result_locks,
                    _telegram_pending_brands, _telegram_pending_character)
last_maintenance = 0.0
telegram_polling_service: TelegramPollingService | None = None
_telegram_system_callbacks: set[str] = set()
_telegram_system_restore: dict[str, dict] = {}


# Authentication and credential helpers were extracted to core/security.py.

class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        setup_route = (request.url.path == "/setup" or
                       request.url.path.startswith(("/api/setup", "/setup.html", "/v1/setup.html", "/api/health")))
        if not is_configured() and not setup_route:
            if request.url.path == "/":
                return Response(status_code=307, headers={"Location": "/v1/setup.html"})
            return Response("Complete the First Run Wizard", 503,
                            {"Retry-After": "30", "X-Vertep-Setup": "required"})
        if request.method == "GET" and is_configured() and request.url.path in ("/", "/index.html"):
            # Design choice: v2 (default) vs v1 (classic). Choice is persisted in the cookie.
            if request.cookies.get("vertep_ui") == "v1":
                return Response(status_code=307, headers={"Location": "/v1/"})
        if not is_configured() and setup_route:
            expected_setup = os.getenv("SETUP_TOKEN_HASH", "")
            if expected_setup and request.url.path.startswith("/api/setup"):
                client = request.client.host if request.client else "unknown"
                window = setup_request_windows[client]
                timestamp = time.time()
                while window and window[0] < timestamp - 600:
                    window.popleft()
                expires = os.getenv("SETUP_TOKEN_EXPIRES_AT", "")
                try:
                    expired = bool(expires) and datetime.fromisoformat(
                        expires.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
                except ValueError:
                    return Response("Setup token expiry configuration is invalid", 503)
                if expired:
                    return Response("Setup code has expired", 410)
                valid_setup_code = secrets.compare_digest(
                    hashlib.sha256(request.headers.get("x-vertep-setup-token", "").encode()).hexdigest(),
                    expected_setup)
                if not valid_setup_code:
                    if len(window) >= int(os.getenv("SETUP_RATE_LIMIT_PER_10_MINUTES", "10")):
                        return Response("Setup attempts are temporarily locked", 429, {"Retry-After": "600"})
                    window.append(timestamp)
                    return Response("Invalid setup code", 401)
            return self._secure(await call_next(request))
        if request.method != "GET":
            path = request.url.path
            operation = None
            if path == "/api/jobs" and request.method == "POST":
                operation = "create_job"
            elif path.startswith("/api/jobs"):
                operation = "mutate_job"
            elif (path.startswith("/api/nodes") and path != "/api/nodes/register"
                  and not re.fullmatch(r"/api/nodes/[a-z0-9-]+/renew", path)):
                operation = "node_control"
            elif path.startswith("/api/settings"):
                operation = "configuration"
            elif path.startswith("/api/system/roles"):
                operation = "configuration"
            elif path.startswith("/api/system/update"):
                operation = "update"
            elif path.startswith("/api/system/recovery"):
                operation = "recovery"
            elif path.startswith("/api/system/restart"):
                operation = "restart"
            elif path.startswith("/api/system/test"):
                operation = "test"
            elif path.startswith("/api/system/backups") and request.method == "POST":
                operation = "backup"
            elif path.startswith("/api/system/backups/") and "/restore" in path and request.method == "POST":
                operation = "restore"
            if operation and not operation_allowed(operation):
                return Response(f"Operation {operation} is blocked by system state", 423)
        client = request.client.host if request.client else "unknown"
        window = request_windows[client]
        now = time.time()
        while window and window[0] < now - 60:
            window.popleft()
        if len(window) >= int(os.getenv("RATE_LIMIT_PER_MINUTE", "300")):
            return Response("Rate limit exceeded", 429)
        window.append(now)
        if int(request.headers.get("content-length", "0") or 0) > int(os.getenv("MAX_REQUEST_BYTES", "30000000")):
            return Response("Request too large", 413)
        password = os.getenv("ADMIN_PASSWORD", "")
        machine = ("/api/workers/heartbeat", "/api/tasks/claim", "/api/tasks/renew",
                   "/api/tasks/cancellations", "/api/tasks/result", "/api/logs/ingest",
                   "/api/node/status")
        if request.url.path.startswith(machine) or re.fullmatch(r"/api/nodes/[a-z0-9-]+/renew", request.url.path):
            # Machine routes validate and bind the token to node_name themselves.
            # Keeping that check in one place also supports hashed per-worker tokens.
            response = await call_next(request)
            return self._secure(response)
        internal_key = os.getenv("INTERNAL_API_KEY", "")
        internal_update_routes = {"/api/status", "/api/system/update/check", "/api/system/update/run",
                                  "/api/system/update/readiness", "/api/system/recovery/normal",
                                  "/api/system/restart", "/api/system/test",
                                  "/api/system/backups"}
        internal_actions = request.url.path.startswith("/api/nodes/") and request.url.path.endswith("/actions")
        if ((request.url.path in internal_update_routes or internal_actions)
                and internal_key
                and secrets.compare_digest(request.headers.get("x-vertep-internal-key", ""), internal_key)):
            response = await call_next(request)
            return self._secure(response)
        public = ("/api/health", "/api/telegram/webhook")
        if ((not configured_user() and not password and not os.getenv("USERS_JSON", "").strip(" {}"))
                or request.url.path.startswith(public) or request.url.path == "/api/nodes/register"):
            response = await call_next(request)
            return self._secure(response)
        expected_user = os.getenv("ADMIN_USER", "admin")
        header = request.headers.get("authorization", "")
        try:
            scheme, encoded = header.split(" ", 1)
            user, supplied = basic64.b64decode(encoded).decode().split(":", 1)
        except (ValueError, UnicodeError, binascii.Error):
            scheme, user, supplied = "", "", ""
        session_identity = _valid_session(request.cookies.get("vertep_session", ""))
        basic_role = _authenticate_user(user, supplied) if scheme.lower() == "basic" else None
        if not session_identity and not basic_role:
            return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Vertep"'})
        actor, role = session_identity or (user, basic_role)
        if request.method != "GET" and role == "viewer" and not request.url.path.startswith("/api/session"):
            return Response("Insufficient role", 403)
        if request.method in {"PUT", "DELETE"} and request.url.path.startswith(("/api/characters", "/api/brands", "/api/workflows")) and role != "admin":
            return Response("Administrator role required", 403)
        if request.method != "GET" and request.url.path.startswith(("/api/system/update", "/api/system/roles", "/api/system/recovery", "/api/system/restart", "/api/system/test")) and role != "admin":
            return Response("Administrator role required", 403)
        if request.method != "GET" and request.url.path.startswith("/api/system/backups") and role != "admin":
            return Response("Administrator role required", 403)
        if request.method != "GET" and request.url.path.startswith("/api/settings") and role != "admin":
            return Response("Administrator role required", 403)
        if (request.method != "GET" and request.url.path.startswith("/api/nodes")
                and request.url.path != "/api/nodes/register" and role != "admin"):
            return Response("Administrator role required", 403)
        if session_identity and not basic_role and request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path != "/api/session":
            csrf = request.cookies.get("vertep_csrf", "")
            if not csrf or not secrets.compare_digest(csrf, request.headers.get("x-csrf-token", "")):
                return Response("Invalid CSRF token", 403)
        response = await call_next(request)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            logger.info("Administrative action", extra={"action": f"{request.method} {request.url.path}",
                                                         "actor": actor})
        return self._secure(response)

    @staticmethod
    def _secure(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data: blob:"
        return response

app.add_middleware(AdminAuthMiddleware)
app.include_router(workflows_router)
app.include_router(resources_router)
app.include_router(settings_router)
app.include_router(models_router)
app.include_router(setup_router)
app.include_router(jobs_router)
app.include_router(tasks_router)
app.include_router(workers_router)
app.include_router(nodes_router)
app.include_router(observability_router)
app.include_router(storyboards_router)
app.include_router(real_tests_router)

@app.get("/setup", include_in_schema=False)
def setup_page(request: Request):
    query = f"?{request.url.query}" if request.url.query else ""
    return Response(status_code=307, headers={"Location": f"/v1/setup.html{query}"})



def handle_expired_storyboard_lease(store, task: dict) -> bool:
    """Issue #64 T4: requeue an expired storyboard lease within budget.

    Returns ``True`` when the task was handled here (the caller must NOT
    apply the generic ASSET_GENERATION path), ``False`` when the task does
    not match the storyboard recovery rule.
    """
    job = store.jobs.get(task.get("job_id"))
    if not job:
        return False
    if not (job.status == JobStatus.STORYBOARD_GENERATING and job.storyboard_task_id == task.get("task_id")):
        return False
    attempts = max(1, int(os.getenv("OLLAMA_STORYBOARD_MAX_RETRIES", "3")))
    current = getattr(job, "storyboard_attempt", 0) + 1
    job.storyboard_attempt = current
    job.assigned_worker = None
    if current < attempts:
        job.storyboard_task_id = None
        job.active_task_id = None
        store.update(job, JobStatus.STORYBOARD_QUEUED,
                     f"STORYBOARD TASK {task['task_id']} LEASE EXPIRED; REQUEUED ({current}/{attempts})")
        store.event(job, f"TASK {task['task_id']} LEASE EXPIRED; REQUEUED")
        # Re-enqueue without triggering StoryboardService.queue's executor:
        # that path runs _process_queue synchronously, which would immediately
        # claim the very task we just requeued and strand it in flight.  The
        # ready queue is the right home here; a live worker claims it through
        # the normal /api/tasks/claim.
        from .storyboard import build_storyboard_prompt
        from .configuration import load_character, read_json
        import os as _os
        from pathlib import Path as _Path
        character = load_character(_Path(_os.getenv("CHARACTERS_ROOT", "characters")), job.character_id).model_dump()
        brand = read_json(_Path(_os.getenv("BRANDS_ROOT", "brands")) / job.brand_id / "brand.json")
        prompt = build_storyboard_prompt(job, character, brand, job.storyboards[-1].revision_request if job.storyboards else None)
        version = (job.storyboards[-1].version + 1 if job.storyboards else 1)
        image_version = job.storyboards[-1].image_version if job.storyboards else 1
        new_task = task_queue.enqueue({
            "job_id": job.job_id, "task": "storyboard", "priority": job.priority,
            "topic": job.topic, "prompt": prompt, "storyboard_version": version,
            "image_version": image_version,
            "revision": job.storyboards[-1].revision_request if job.storyboards else None,
            "timeout": int(_os.getenv("OLLAMA_STORYBOARD_TIMEOUT", "300")),
        })
        job.storyboard_task_id = new_task["task_id"]
        job.active_task_id = new_task["task_id"]
        store.repository.record_task(new_task, "QUEUED")
        store.event(job, f"STORYBOARD TASK {new_task['task_id']} QUEUED for version {version}")
    else:
        job.storyboard_task_id = None
        job.active_task_id = None
        store.update(job, JobStatus.STORYBOARD_FAILED,
                     f"STORYBOARD FAILED after {attempts} attempts: task lease expired repeatedly")
        store.event(job, f"TASK {task['task_id']} LEASE EXPIRED; FAILED ({current}/{attempts})")
    return True


async def _watchdog() -> None:
    global last_maintenance
    while True:
        await asyncio.sleep(float(os.getenv("WATCHDOG_INTERVAL", "5")))
        if not dispatch_allowed():
            continue
        if not task_queue.acquire_watchdog_lock(max(2, int(float(os.getenv("WATCHDOG_INTERVAL", "5")) * 2))):
            continue
        _recover_stale_workers()
        for job in list(store.jobs.values()):
            if job.status == JobStatus.WAITING_FOR_SYSTEM:
                store.update(job, JobStatus.NEW, "SYSTEM RETURNED TO NORMAL")
            if job.status == JobStatus.NEW and _job_is_due(job):
                store.update(job, JobStatus.SCRIPTING, "SCHEDULED JOB STARTED")
                job.status = JobStatus.NEW
                executor.submit(_prepare_and_dispatch, job)
        for task in task_queue.requeue_expired():
            if handle_expired_storyboard_lease(store, task):
                continue
            job = store.jobs.get(task.get("job_id"))
            if not job:
                continue
            if job.status == JobStatus.ASSET_GENERATION:
                scene = _scene_for_task(job, task["task_id"])
                if scene:
                    interrupt_scene(scene, "Task lease expired")
                job.assigned_worker = None
                store.event(job, f"TASK {task['task_id']} LEASE EXPIRED; REQUEUED")
        if os.getenv("AUTO_CLEANUP", "false").lower() == "true" and time.time() - last_maintenance > 3600:
            cleanup_jobs(store, dry_run=False)
            cleanup_temporary_files(store.root, dry_run=False)
            last_maintenance = time.time()







@app.post("/api/session")
def create_session(response: Response, request: Request):
    header = request.headers.get("authorization", "")
    try:
        _, encoded = header.split(" ", 1)
        user, supplied = basic64.b64decode(encoded).decode().split(":", 1)
    except (ValueError, UnicodeError, binascii.Error):
        user, supplied = "admin", os.getenv("ADMIN_PASSWORD", "")
    role = _authenticate_user(user, supplied) or "admin"
    token = _session_token(user, role)
    csrf = hmac.new(os.getenv("ADMIN_PASSWORD", "").encode(), token.encode(), hashlib.sha256).hexdigest()
    response.set_cookie("vertep_session", token, httponly=True, samesite="strict",
                        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
                        max_age=int(os.getenv("SESSION_TTL", "28800")))
    response.set_cookie("vertep_csrf", csrf, httponly=False, samesite="strict",
                        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true")
    return {"authenticated": True, "user": user, "role": role}

@app.delete("/api/session")
def logout(response: Response):
    response.delete_cookie("vertep_session")
    response.delete_cookie("vertep_csrf")
    return {"authenticated": False}

@app.get("/api/session")
def session_info(request: Request):
    identity = _valid_session(request.cookies.get("vertep_session", ""))
    return {"authenticated": bool(identity), "user": identity[0] if identity else None,
            "role": identity[1] if identity else None}

@app.put("/api/session/password")
def change_password(request: Request, payload: dict):
    identity = _valid_session(request.cookies.get("vertep_session", ""))
    if not identity:
        return Response(content=json.dumps({"detail": "Unauthorized"}), status_code=401, media_type="application/json")
    user, role = identity
    old_password = payload.get("old_password", "")
    new_password = payload.get("new_password", "")
    if not old_password or not new_password:
        return Response(content=json.dumps({"detail": "Old and new passwords required"}), status_code=400, media_type="application/json")
    if len(new_password) < 12:
        return Response(content=json.dumps({"detail": "Password must be at least 12 characters"}), status_code=400, media_type="application/json")
    if old_password == new_password:
        return Response(content=json.dumps({"detail": "New password must be different from old"}), status_code=400, media_type="application/json")
    configured = configured_user()
    if configured and configured[0] == user:
        if not _verify_hash(old_password, configured[1]["password_hash"]):
            return Response(content=json.dumps({"detail": "Current password is incorrect"}), status_code=400, media_type="application/json")
        admin_data = dict(configured[1])
        admin_data["password_hash"] = password_hash(new_password)
        inst = installation()
        inst["administrator"] = admin_data
        _write("installation.json", inst)
    else:
        record = load_all_users().get(user)
        if not isinstance(record, dict):
            return Response(content=json.dumps({"detail": "User not found"}), status_code=404, media_type="application/json")
        if not isinstance(record.get("password_hash"), str) or not _verify_hash(old_password, record["password_hash"]):
            return Response(content=json.dumps({"detail": "Current password is incorrect"}), status_code=400, media_type="application/json")
        updated = dict(record)
        updated["password_hash"] = password_hash(new_password)
        save_user(user, updated)
    return {"ok": True, "message": "Password changed successfully"}

@app.get("/api/events")
async def event_stream():
    async def generate():
        snapshots: dict[str, int] = {}
        while True:
            changes = []
            for job in store.jobs.values():
                count = len(job.events)
                if snapshots.get(job.job_id, 0) < count:
                    changes.append({"job_id": job.job_id, "status": job.status.value,
                                    "event": job.events[-1] if job.events else ""})
                    snapshots[job.job_id] = count
            yield ("data: " + json.dumps(changes, ensure_ascii=False) + "\n\n") if changes else ": keepalive\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(generate(), media_type="text/event-stream")








# Workflow CRUD routes are defined in core/api/workflows.py
# Job CRUD routes are defined in core/api/jobs.py
# Worker and Task routes are defined in core/api/workers.py / core/api/tasks.py
@app.post("/api/telegram/webhook")
def telegram_webhook(update: dict, request: Request):
    if os.getenv("TELEGRAM_WEBHOOK_ENABLED", "").lower() not in {"1", "true", "yes"}:
        raise HTTPException(404, "Telegram webhook is disabled; use polling as the primary integration")
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if webhook_secret and not secrets.compare_digest(request.headers.get("x-telegram-bot-api-secret-token", ""), webhook_secret):
        raise HTTPException(401, "Invalid Telegram webhook secret")
    callback = update.get("callback_query")
    if callback:
        return _handle_telegram_callback(callback)
    message = update.get("message", {})
    text = str(message.get("text") or message.get("caption") or "").strip()
    if not text:
        raise HTTPException(400, "Telegram update has no text")
    chat_id = str(message.get("chat", {}).get("id", "unknown"))
    allowed = get_allowed_chat_ids()
    admin_ids = set(get_admin_chat_ids())
    if allowed and chat_id not in allowed and chat_id not in admin_ids:
        raise HTTPException(403, "Telegram chat is not allowed")
    source_id = str(message.get("message_id", ""))
    return _handle_telegram_message(chat_id, source_id, text, message)


def _admin_chat_ids() -> list[str]:
    return get_admin_chat_ids()


def _is_admin_chat(chat_id: str) -> bool:
    return is_admin_chat(chat_id)


def _format_operation(operation: dict) -> str:
    """Format a persisted system-operation record as readable text for Telegram."""
    status = operation.get("status", "UNKNOWN")
    op_type = operation.get("type", "?")
    op_id = str(operation.get("operation_id", ""))[:8]
    progress = operation.get("progress", 0)
    phase = operation.get("current_phase") or "—"
    error = operation.get("error")
    lines = [
        f"Операція {op_type} (#{op_id})",
        f"Статус: {status} · прогрес {progress}% · фаза: {phase}",
    ]
    if error:
        lines.append(f"Помилка: {error}")
    return "\n".join(lines)


def _send_system_menu(chat_id: str) -> dict:
    menu = {
        "inline_keyboard": [
            [
                {"text": "📊 Статус", "callback_data": "sys_status:menu"},
                {"text": "🔄 Оновлення", "callback_data": "sys_update:menu"},
                {"text": "🔁 Рестарт", "callback_data": "sys_restart:menu"},
            ],
            [
                {"text": "💾 Резервна копія", "callback_data": "sys_backup:menu"},
                {"text": "♻️ Відновлення", "callback_data": "sys_restore:menu"},
                {"text": "🧪 Тест", "callback_data": "sys_test:menu"},
            ],
        ]
    }
    return TelegramAdapter().send_message(chat_id, "🛠 Керування системою:", reply_markup=menu)


def _sync_internal_api(method: str, base_environment: str, path: str,
                       payload: dict | None = None, timeout: float = 30.0) -> dict | None:
    """Synchronous wrapper around an internal service API.

    Returns the parsed JSON body, or ``None`` when the service is not
    configured / unreachable.  Telegram handlers must never raise on a
    missing backend — they degrade to an error message instead.
    """
    base = os.getenv(base_environment, "").rstrip("/")
    if not base:
        return None
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(method, f"{base}{path}", json=payload)
            if response.status_code >= 400:
                return {"_error": f"HTTP {response.status_code}: {response.text[:200]}"}
            return response.json()
    except (httpx.HTTPError, ValueError):
        return None


def _format_workers_summary() -> str:
    """One-line summary of registered nodes for Telegram status."""
    nodes = _sync_internal_api("GET", "CORE_API_URL", "/api/nodes") \
        or _sync_internal_api("GET", "BACKUP_URL", "/api/nodes")
    if not nodes or not isinstance(nodes, list):
        return "Вузли: 0/0 online"
    online = sum(1 for n in nodes if (n.get("runtime_status") or n.get("status")) == "ONLINE")
    return f"Вузли: {online}/{len(nodes)} online"


def _format_jobs_summary() -> str:
    """One-line summary of active/queued/failed jobs for Telegram status."""
    data = _sync_internal_api("GET", "CORE_API_URL", "/api/jobs",
                              {"per_page": 100, "page": 1}) or {}
    jobs = data if isinstance(data, list) else data.get("jobs", [])
    if not isinstance(jobs, list):
        return "Jobs: 0 active / 0 queued / 0 failed"
    active = {"PROCESSING", "SCRIPTING", "ASSET_GENERATION", "VIDEO_GENERATION",
              "ASSEMBLY", "PUBLISHING"}
    queued = {"NEW", "WAITING_FOR_SYSTEM"}
    failed = {"FAILED"}
    counts = {"active": 0, "queued": 0, "failed": 0}
    for job in jobs:
        status = str(job.get("status", "")).upper()
        if status in active:
            counts["active"] += 1
        elif status in queued:
            counts["queued"] += 1
        elif status in failed:
            counts["failed"] += 1
    return (f"Jobs: {counts['active']} active / {counts['queued']} queued "
            f"/ {counts['failed']} failed")


def _format_last_backup() -> str:
    """Last backup line for Telegram status."""
    data = _sync_internal_api("GET", "BACKUP_URL", "/snapshots")
    snapshots = (data or {}).get("snapshots", []) if isinstance(data, dict) else []
    if not snapshots:
        return "Останній backup: unavailable"
    latest = snapshots[0]
    return (f"Останній backup: {latest.get('snapshot_id', '?')} "
            f"({latest.get('created_at', '?')})")


def _system_status_text() -> str:
    state = get_system_state()
    update = {}
    try:
        update = update_status() or {}
    except Exception:
        pass
    state_name = state.get("state", "NORMAL")
    update_state = update.get("state", "IDLE")
    current = update.get("current_version") or "?"

    # Uptime
    try:
        import psutil
        boot_time = psutil.boot_time()
        import time
        uptime_sec = int(time.time() - boot_time)
        days, rem = divmod(uptime_sec, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        uptime_str = f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"
    except Exception:
        uptime_str = "?"

    # Core services health (quick checks)
    from .health_checks import check_postgres, check_redis, check_core_api
    pg_ok, pg_msg = check_postgres()
    redis_ok, redis_msg = check_redis()
    core_url = os.getenv("CORE_ADDRESS", "")
    core_ok, core_msg = check_core_api(core_url) if core_url else (None, "not configured")

    # Dispatcher status
    dispatcher_ok = "running"
    try:
        from .dispatcher import available_worker
        workers = list(store.workers.values())
        if workers:
            _ = available_worker(workers, Job(job_id="_health", topic="_health",
                                               character_id="", priority=1,
                                               status=JobStatus.NEW, created_at=utc_now()))
    except Exception:
        dispatcher_ok = "error"

    lines = [
        f"Система: {state_name}",
        f"Uptime: {uptime_str}",
        f"PostgreSQL: {'✅' if pg_ok else '❌' if pg_ok is False else '⚠️'}",
        f"Redis: {'✅' if redis_ok else '❌' if redis_ok is False else '⚠️'}",
        f"CORE API: {'✅' if core_ok else '❌' if core_ok is False else '⚠️'}",
        f"Dispatcher: {dispatcher_ok}",
        f"Оновлення: {update_state} (поточна {current})"
    ]
    available = update.get("available_version")
    if available:
        lines.append(f"Доступна версія: {available}")
    lines.append(_format_workers_summary())
    lines.append(_format_jobs_summary())
    lines.append(_format_last_backup())
    operations = list_operations(5)
    if operations:
        lines.append("Останні операції:")
        for op in operations:
            lines.append(f"  • {_format_operation(op)}")
    return "\n".join(lines)


def _send_confirmation(chat_id: str, message: str, confirm_data: str,
                       cancel_data: str = "sys_cancel") -> dict:
    """Send a two-step confirmation prompt with Confirm / Cancel inline keyboard."""
    keyboard = {"inline_keyboard": [
        [{"text": "✅ Підтвердити", "callback_data": confirm_data},
         {"text": "❌ Скасувати", "callback_data": cancel_data}],
    ]}
    return TelegramAdapter().send_message(chat_id, message, reply_markup=keyboard)


def _send_backups_list(chat_id: str, backups: list[dict]) -> dict:
    """Show available backups for restore selection."""
    if not backups:
        return TelegramAdapter().send_message(
            chat_id, "Недоступні резервні копії. Перевірте Backup Node.")
    keyboard = {"inline_keyboard": [
        [{"text": f"💾 {b.get('snapshot_id', '?')[:18]} ({b.get('created_at', '?')[:16]})",
          "callback_data": f"sys_restore_select:{b.get('snapshot_id')}"}]
        for b in backups[:10]
    ]}
    keyboard["inline_keyboard"].append(
        [{"text": "❌ Скасувати", "callback_data": "sys_restore_cancel"}])
    return TelegramAdapter().send_message(
        chat_id, "Оберіть резервну копію для відновлення:", reply_markup=keyboard)


def _send_nodes_list(chat_id: str, nodes: list[dict], prefix: str) -> dict:
    """Show registered nodes for target selection (restart / test node)."""
    if not nodes:
        return TelegramAdapter().send_message(chat_id, "Немає зареєстрованих вузлів.")
    keyboard = {"inline_keyboard": [
        [{"text": f"🖥 {n.get('node_id', '?')} [{n.get('role', '?')}]",
          "callback_data": f"{prefix}:{n.get('node_id')}"}]
        for n in nodes[:10]
    ]}
    keyboard["inline_keyboard"].append(
        [{"text": "❌ Скасувати", "callback_data": "sys_cancel"}])
    return TelegramAdapter().send_message(
        chat_id, "Оберіть вузол:", reply_markup=keyboard)


def _call_backup_api(method: str, path: str, payload: dict | None = None,
                     timeout: float = 120.0) -> dict | None:
    """Call the Backup Service directly (synchronous)."""
    return _sync_internal_api(method, "BACKUP_URL", path, payload, timeout=timeout)


def _call_core_api(method: str, path: str, payload: dict | None = None,
                   timeout: float = 30.0) -> dict | None:
    """Call the CORE API directly (synchronous).

    Passes the ``INTERNAL_API_KEY`` so that admin-only endpoints invoked by
    Telegram handlers (restart, test, backups, node actions) authenticate
    correctly without requiring user credentials.
    """
    return _sync_internal_api(method, "CORE_API_URL", path, payload, timeout=timeout,
                              headers=_internal_auth_headers())


def _internal_auth_headers() -> dict:
    """Build headers carrying the internal API key for self-calls."""
    key = os.getenv("INTERNAL_API_KEY", "")
    return {"x-vertep-internal-key": key} if key else {}


def _sync_internal_api(method: str, base_environment: str, path: str,
                       payload: dict | None = None, timeout: float = 30.0,
                       headers: dict | None = None) -> dict | None:
    """Synchronous wrapper around an internal service API.

    Returns the parsed JSON body, or ``None`` when the service is not
    configured / unreachable.  Telegram handlers must never raise on a
    missing backend — they degrade to an error message instead.
    """
    base = os.getenv(base_environment, "").rstrip("/")
    if not base:
        return None
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(method, f"{base}{path}", json=payload, headers=headers)
            if response.status_code >= 400:
                return {"_error": f"HTTP {response.status_code}: {response.text[:200]}"}
            return response.json()
    except (httpx.HTTPError, ValueError):
        return None


def _handle_system_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))

    if action in {"sys_status", "sys_status:menu"}:
        return TelegramAdapter().answer_callback(callback_id, _system_status_text())

    if action == "sys_update":
        existing = is_operation_in_progress("update")
        if existing:
            return TelegramAdapter().answer_callback(
                callback_id, f"Оновлення вже в процесі (#{existing['operation_id'][:8]})."
            )
        update_info = {}
        try:
            update_info = update_status() or {}
        except Exception:
            pass
        current = update_info.get("current_version") or application_version()
        available = update_info.get("available_version")
        message = f"🔄 Поточна версія: {current}\n"
        if available:
            message += f"Доступна: {available}\n\nОновити Vertep?"
        else:
            message += "Доступних оновлень немає.\n\nПеревірети доступність оновлення?"
        return _send_confirmation(chat_id, message, "sys_update_check", "sys_cancel")

    if action == "sys_update_check":
        try:
            op, created = get_or_create_operation("update", f"telegram:{chat_id}")
            if not created:
                return TelegramAdapter().answer_callback(
                    callback_id, f"Перевірка оновлення вже в процесі (#{op['operation_id'][:8]})."
                )
            audit_entry(op["operation_id"], "INTERNAL_CALL", "request_update check", f"telegram:{chat_id}")
            request_update("check")
            begin_operation(op["operation_id"], "Перевірка доступного оновлення")
            return TelegramAdapter().answer_callback(
                callback_id,
                f"Перевірка оновлення запущена. ID операції: {op['operation_id'][:8]}"
            )
        except Exception as error:
            return TelegramAdapter().answer_callback(callback_id, f"❌ Перевірка не запущена: {error}")

    if action == "sys_update_confirm":
        try:
            op, created = get_or_create_operation("update", f"telegram:{chat_id}")
            if not created:
                return TelegramAdapter().answer_callback(
                    callback_id, f"Оновлення вже в процесі (#{op['operation_id'][:8]})."
                )
            audit_entry(op["operation_id"], "INTERNAL_CALL", "request_update update", f"telegram:{chat_id}")
            request_update("update")
            begin_operation(op["operation_id"], "Запит на оновлення прийнято")
            audit_entry(op["operation_id"], "RUNNING", "Update started via Telegram", f"telegram:{chat_id}")
            return TelegramAdapter().answer_callback(
                callback_id, f"Оновлення заплановано. ID операції: {op['operation_id'][:8]}"
            )
        except Exception as error:
            return TelegramAdapter().answer_callback(callback_id, f"❌ Оновлення не запущено: {error}")

    if action == "sys_update_cancel":
        return TelegramAdapter().answer_callback(
            callback_id, "Оновлення скасовано."
        )

    if action == "sys_restart":
        existing = is_operation_in_progress("restart")
        if existing:
            return TelegramAdapter().answer_callback(
                callback_id, f"Перезапуск вже в процесі (#{existing['operation_id'][:8]})."
            )
        keyboard = {"inline_keyboard": [
            [{"text": "🔁 Core", "callback_data": "sys_restart_core"},
             {"text": "🔁 Worker", "callback_data": "sys_restart_worker"}],
            [{"text": "❌ Скасувати", "callback_data": "sys_cancel"}],
        ]}
        return TelegramAdapter().send_message(
            chat_id, "Оберіть що перезапустити:", reply_markup=keyboard
        )

    if action == "sys_restart_core":
        return _send_confirmation(
            chat_id, "🔄 Перезапустити CORE сервіси?\nВсі активні задачі будуть продовжені.",
            "sys_restart_core_confirm", "sys_cancel"
        )

    if action == "sys_restart_core_confirm":
        op, created = get_or_create_operation("restart", f"telegram:{chat_id}", target="core")
        if not created:
            return TelegramAdapter().answer_callback(
                callback_id, f"Перезапуск вже в процесі (#{op['operation_id'][:8]})."
            )
        begin_operation(op["operation_id"], "Перезапуск CORE")
        audit_entry(op["operation_id"], "INTERNAL_CALL", "POST /api/system/restart target=core", f"telegram:{chat_id}")
        try:
            result = _call_core_api("POST", "/api/system/restart", {"target": "core"}, timeout=15)
            if result and "_error" in result:
                fail_operation(op["operation_id"], result["_error"])
                audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"error: {result['_error'][:200]}", f"telegram:{chat_id}")
                return TelegramAdapter().answer_callback(
                    callback_id, f"❌ Перезапуск CORE не вдався: {result['_error'][:200]}"
                )
            advance_operation(op["operation_id"], "Очікування здоров'я CORE", 50, "Перезапуск ініційовано, очікуємо HEALTHY")
            # Poll /api/health until healthy or timeout
            healthy = False
            for _ in range(30):  # up to 60 seconds
                time.sleep(2)
                health = _call_core_api("GET", "/api/health", timeout=5)
                if health and health.get("status") == "healthy":
                    healthy = True
                    break
            if not healthy:
                fail_operation(op["operation_id"], "CORE не став HEALTHY після перезапуску (timeout 60s)")
                audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", "GET /api/health timeout", f"telegram:{chat_id}")
                return TelegramAdapter().answer_callback(
                    callback_id, f"❌ Перезапуск CORE: сервіс не став HEALTHY за 60s. ID: {op['operation_id'][:8]}"
                )
            complete_operation(op["operation_id"], {"status": "healthy", "message": "CORE перезапущено успішно"})
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", "CORE healthy", f"telegram:{chat_id}")
            return TelegramAdapter().answer_callback(
                callback_id, f"✅ CORE перезапущено і HEALTHY. ID: {op['operation_id'][:8]}"
            )
        except Exception as error:
            fail_operation(op["operation_id"], str(error))
            return TelegramAdapter().answer_callback(callback_id, f"❌ Перезапуск CORE не вдався: {error}")

    if action == "sys_restart_worker":
        nodes = _sync_internal_api("GET", "CORE_API_URL", "/api/nodes") or []
        if not nodes or not isinstance(nodes, list):
            return TelegramAdapter().answer_callback(
                callback_id, "Немає зареєстрованих вузлів."
            )
        return _send_nodes_list(chat_id, nodes, "sys_restart_worker_select")

    if action == "sys_restart_worker_select":
        node_id = payload
        if not node_id or not re.fullmatch(r"[a-z0-9-]+", node_id):
            return TelegramAdapter().answer_callback(callback_id, "Некоректний вузол.")
        return _send_confirmation(
            chat_id, f"🔄 Перезапустити Worker {node_id}?\nАктивні задачі будуть передані іншим вузлам.",
            f"sys_restart_worker_confirm:{node_id}", "sys_cancel"
        )

    if action == "sys_restart_worker_confirm":
        node_id = payload
        if not node_id:
            return TelegramAdapter().answer_callback(callback_id, "Некоректний вузол.")
        op, created = get_or_create_operation("restart", f"telegram:{chat_id}", target=node_id)
        if not created:
            return TelegramAdapter().answer_callback(
                callback_id, f"Перезапуск вже в процесі (#{op['operation_id'][:8]})."
            )
        begin_operation(op["operation_id"], f"Перезапуск вузла {node_id}")
        audit_entry(op["operation_id"], "INTERNAL_CALL", f"POST /api/nodes/{node_id}/actions action=restart", f"telegram:{chat_id}")
        try:
            result = _call_core_api("POST", f"/api/nodes/{node_id}/actions",
                                    {"action": "restart", "reason": f"telegram restart {op['operation_id'][:8]}"},
                                    timeout=15)
            if result and "_error" in result:
                fail_operation(op["operation_id"], result["_error"])
                audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"error: {result['_error'][:200]}", f"telegram:{chat_id}")
                return TelegramAdapter().answer_callback(
                    callback_id, f"❌ Перезапуск {node_id} не вдався: {result['_error'][:200]}"
                )
            advance_operation(op["operation_id"], f"Очікування READY {node_id}", 50, "Перезапуск ініційовано, очікуємо READY")
            # Poll /api/nodes/{node_id} until status == READY or timeout
            ready = False
            for _ in range(30):  # up to 60 seconds
                time.sleep(2)
                node = _call_core_api("GET", f"/api/nodes/{node_id}", timeout=5)
                if node and node.get("status") == "READY":
                    ready = True
                    break
            if not ready:
                fail_operation(op["operation_id"], f"Worker {node_id} не став READY після перезапуску (timeout 60s)")
                audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"GET /api/nodes/{node_id} timeout", f"telegram:{chat_id}")
                return TelegramAdapter().answer_callback(
                    callback_id, f"❌ Перезапуск {node_id}: вузол не став READY за 60s. ID: {op['operation_id'][:8]}"
                )
            complete_operation(op["operation_id"], {"status": "ready", "node_id": node_id, "message": f"Worker {node_id} перезапущено успішно"})
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"Worker {node_id} READY", f"telegram:{chat_id}")
            return TelegramAdapter().answer_callback(
                callback_id, f"✅ Вузол {node_id} перезапущено і READY. ID: {op['operation_id'][:8]}"
            )
        except Exception as error:
            fail_operation(op["operation_id"], str(error))
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"exception: {error}", f"telegram:{chat_id}")
            return TelegramAdapter().answer_callback(callback_id, f"❌ Перезапуск {node_id} не вдався: {error}")

    if action == "sys_backup":
        return _send_confirmation(
            chat_id, "💾 Створити резервну копію зараз?",
            "sys_backup_confirm", "sys_cancel"
        )

    if action == "sys_backup_confirm":
        op, created = get_or_create_operation("backup", f"telegram:{chat_id}")
        if not created:
            return TelegramAdapter().answer_callback(
                callback_id, f"Резервна копія вже в процесі (#{op['operation_id'][:8]})."
            )
        begin_operation(op["operation_id"], "Створення резервної копії")
        advance_operation(op["operation_id"], "snapshot", 10, "Snapshot заплановано")
        audit_entry(op["operation_id"], "INTERNAL_CALL", "POST /api/system/backups", f"telegram:{chat_id}")
        audit_entry(op["operation_id"], "snapshot", "Backup started via Telegram", f"telegram:{chat_id}")
        try:
            # Use CORE API endpoint for backup (via _call_core_api with INTERNAL_API_KEY)
            result = _call_core_api("POST", "/api/system/backups", {}, timeout=120)
            if result and "_error" in result:
                fail_operation(op["operation_id"], result["_error"])
                audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"error: {result['_error'][:200]}", f"telegram:{chat_id}")
                return TelegramAdapter().answer_callback(
                    callback_id, f"❌ Backup не вдався: {result['_error'][:200]}"
                )
            if result and result.get("snapshot_id"):
                complete_operation(op["operation_id"], result)
                audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"snapshot_id={result['snapshot_id']}", f"telegram:{chat_id}")
                return TelegramAdapter().answer_callback(
                    callback_id,
                    f"✅ Backup створено. Snapshot: {result['snapshot_id'][:18]} ID операції: {op['operation_id'][:8]}"
                )
            advance_operation(op["operation_id"], "snapshot", 20, "Snapshot створюється")
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", "backup scheduled", f"telegram:{chat_id}")
            return TelegramAdapter().answer_callback(
                callback_id, f"✅ Backup заплановано. ID операції: {op['operation_id'][:8]}"
            )
        except Exception as error:
            fail_operation(op["operation_id"], str(error))
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"exception: {error}", f"telegram:{chat_id}")
            return TelegramAdapter().answer_callback(callback_id, f"❌ Backup не вдався: {error}")

    if action == "sys_backup_cancel":
        return TelegramAdapter().answer_callback(callback_id, "Операцію скасовано.")

    if action == "sys_restore":
        # Use CORE API endpoint for listing backups (via _call_core_api with INTERNAL_API_KEY)
        backups = _call_core_api("GET", "/api/system/backups", timeout=15)
        if backups is None:
            return TelegramAdapter().answer_callback(
                callback_id, "Backup Node недоступний. Спочатку налаштуйте Backup Node."
            )
        snapshots = (backups or {}).get("snapshots", []) if isinstance(backups, dict) else []
        if not snapshots:
            return TelegramAdapter().answer_callback(callback_id, "Немає доступних резервних копій.")
        return _send_backups_list(chat_id, snapshots)

    if action == "sys_restore_select":
        snapshot_id = payload
        if not snapshot_id or not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
            return TelegramAdapter().answer_callback(callback_id, "Некоректний ID snapshot.")
        existing = is_operation_in_progress("restore")
        if existing:
            return TelegramAdapter().answer_callback(
                callback_id, f"Відновлення вже в процесі (#{existing['operation_id'][:8]})."
            )
        # Use CORE API endpoint for backup detail
        backup_detail = _call_core_api("GET", f"/api/system/backups/{snapshot_id}", timeout=15) or {}
        audit_entry(snapshot_id, "INTERNAL_CALL", f"GET /api/system/backups/{snapshot_id}", f"telegram:{chat_id}")
        snapshot = backup_detail if "snapshot_id" in backup_detail else {"snapshot_id": snapshot_id}
        message = (f"⚠️ ПЕРШЕ підтвердження відновлення\n"
                   f"Snapshot: {snapshot.get('snapshot_id', snapshot_id)}\n"
                   f"Створено: {snapshot.get('created_at', '?')}\n"
                   f"Розмір: {snapshot.get('size', '?')} байт\n\n"
                   f"Це замінить поточні дані. Продовжити?")
        return _send_confirmation(
            chat_id, message, f"sys_restore_confirm:{snapshot_id}", "sys_cancel"
        )

    if action == "sys_restore_confirm":
        snapshot_id = payload
        if not snapshot_id or not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
            return TelegramAdapter().answer_callback(callback_id, "Некоректний ID snapshot.")
        message = (f"⚠️ ДРУГЕ явне підтвердження\n"
                   f"Ви впевнені, що хочете відновити snapshot {snapshot_id}? "
                   f"Ця дія незворотна.")
        return _send_confirmation(
            chat_id, message, f"sys_restore_execute:{snapshot_id}", "sys_cancel"
        )

    if action == "sys_restore_execute":
        snapshot_id = payload
        if not snapshot_id or not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
            return TelegramAdapter().answer_callback(callback_id, "Некоректний ID snapshot.")
        op, created = get_or_create_operation("restore", f"telegram:{chat_id}", target=snapshot_id)
        if not created:
            return TelegramAdapter().answer_callback(
                callback_id, f"Відновлення вже в процесі (#{op['operation_id'][:8]})."
            )
        begin_operation(op["operation_id"], "Відновлення")
        advance_operation(op["operation_id"], "decrypt", 10, f"Відновлення snapshot {snapshot_id[:18]}")
        audit_entry(op["operation_id"], "INTERNAL_CALL", f"POST /api/system/backups/{snapshot_id}/restore", f"telegram:{chat_id}")
        audit_entry(op["operation_id"], "restore", f"Restore started via Telegram for {snapshot_id}", f"telegram:{chat_id}")
        try:
            # Use CORE API endpoint for restore (via _call_core_api with INTERNAL_API_KEY)
            # This goes through CORE which then calls Backup Service
            result = _call_core_api("POST", f"/api/system/backups/{snapshot_id}/restore", {}, timeout=300)
            if result and "_error" in result:
                fail_operation(op["operation_id"], result["_error"])
                audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"error: {result['_error'][:200]}", f"telegram:{chat_id}")
                return TelegramAdapter().answer_callback(
                    callback_id, f"❌ Відновлення не вдався: {result['_error'][:200]}"
                )
            if result and "status" in result:
                status = result.get("status")
                if status == "done":
                    complete_operation(op["operation_id"], result)
                    audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", "restore completed", f"telegram:{chat_id}")
                    return TelegramAdapter().answer_callback(
                        callback_id, f"✅ Відновлення завершено. ID операції: {op['operation_id'][:8]}"
                    )
                fail_operation(op["operation_id"], f"Restore ended with status: {status}")
                audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"status={status}", f"telegram:{chat_id}")
                return TelegramAdapter().answer_callback(
                    callback_id, f"❌ Відновлення завершилось з помилкою. ID: {op['operation_id'][:8]}"
                )
            advance_operation(op["operation_id"], "in_progress", 50, "Відновлення виконується")
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", "restore in progress", f"telegram:{chat_id}")
            return TelegramAdapter().answer_callback(
                callback_id, f"✅ Відновлення запущено. ID операції: {op['operation_id'][:8]}"
            )
        except Exception as error:
            fail_operation(op["operation_id"], str(error))
            return TelegramAdapter().answer_callback(callback_id, f"❌ Відновлення не вдався: {error}")

    if action == "sys_restore_cancel":
        return TelegramAdapter().answer_callback(callback_id, "Операцю скасовано.")

    if action == "sys_test":
        existing = is_operation_in_progress("test")
        if existing:
            return TelegramAdapter().answer_callback(
                callback_id, f"Тест вже в процесі (#{existing['operation_id'][:8]})."
            )
        keyboard = {"inline_keyboard": [
            [{"text": "⚡ Швидкий", "callback_data": "sys_test_quick"},
             {"text": "📋 Повний", "callback_data": "sys_test_full"}],
            [{"text": "🖥 Тест вузла", "callback_data": "sys_test_node"}],
            [{"text": "❌ Скасувати", "callback_data": "sys_cancel"}],
        ]}
        return TelegramAdapter().send_message(
            chat_id, "Оберіть тип тесту:", reply_markup=keyboard
        )

    if action == "sys_test_quick":
        return _send_confirmation(
            chat_id, "⚡ Запустити швидкий тест (CORE API, PostgreSQL, Redis, Dispatcher, storage)?",
            "sys_test_quick_confirm", "sys_cancel"
        )

    if action == "sys_test_full":
        return _send_confirmation(
            chat_id, "📋 Запустити повний тест (все включно з Worker Node)?",
            "sys_test_full_confirm", "sys_cancel"
        )

    if action == "sys_test_quick_confirm":
        op, created = get_or_create_operation("test", f"telegram:{chat_id}", target="quick")
        if not created:
            return TelegramAdapter().answer_callback(
                callback_id, f"Тест вже в процесі (#{op['operation_id'][:8]})."
            )
        begin_operation(op["operation_id"], "Швидкий тест")
        audit_entry(op["operation_id"], "INTERNAL_CALL", "POST /api/system/test scope=quick", f"telegram:{chat_id}")
        try:
            result = _call_core_api("POST", "/api/system/test", {"scope": "quick"}, timeout=30)
            if result and "_error" in result:
                fail_operation(op["operation_id"], result["_error"])
                audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"error: {result['_error'][:200]}", f"telegram:{chat_id}")
                return TelegramAdapter().answer_callback(
                    callback_id, f"❌ Швидкий тест завершився помилкою: {result['_error'][:200]}"
                )
            checks = result or {} if isinstance(result, dict) else {}
            status = checks.get("result", "UNKNOWN")
            complete_operation(op["operation_id"], result)
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"result={status}", f"telegram:{chat_id}")
            return TelegramAdapter().answer_callback(
                callback_id, f"✅ Швидкий тест: {status}. ID: {op['operation_id'][:8]}"
            )
        except Exception as error:
            fail_operation(op["operation_id"], str(error))
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"exception: {error}", f"telegram:{chat_id}")
            return TelegramAdapter().answer_callback(callback_id, f"❌ Швидкий тест не вдався: {error}")

    if action == "sys_test_full_confirm":
        op, created = get_or_create_operation("test", f"telegram:{chat_id}", target="full")
        if not created:
            return TelegramAdapter().answer_callback(
                callback_id, f"Тест вже в процесі (#{op['operation_id'][:8]})."
            )
        begin_operation(op["operation_id"], "Повний тест")
        audit_entry(op["operation_id"], "INTERNAL_CALL", "POST /api/system/test scope=full", f"telegram:{chat_id}")
        try:
            result = _call_core_api("POST", "/api/system/test", {"scope": "full"}, timeout=60)
            if result and "_error" in result:
                fail_operation(op["operation_id"], result["_error"])
                audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"error: {result['_error'][:200]}", f"telegram:{chat_id}")
                return TelegramAdapter().answer_callback(
                    callback_id, f"❌ Повний тест завершився помилкою: {result['_error'][:200]}"
                )
            checks = result if isinstance(result, dict) else {}
            status = checks.get("result", "UNKNOWN")
            complete_operation(op["operation_id"], result)
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"result={status}", f"telegram:{chat_id}")
            return TelegramAdapter().answer_callback(
                callback_id, f"✅ Повний тест: {status}. ID: {op['operation_id'][:8]}"
            )
        except Exception as error:
            fail_operation(op["operation_id"], str(error))
            return TelegramAdapter().answer_callback(callback_id, f"❌ Повний тест не вдався: {error}")

    if action == "sys_test_node":
        nodes = _sync_internal_api("GET", "CORE_API_URL", "/api/nodes") or []
        if not nodes or not isinstance(nodes, list):
            return TelegramAdapter().answer_callback(callback_id, "Немає зареєстрованих вузлів.")
        return _send_nodes_list(chat_id, nodes, "sys_test_node_select")

    if action == "sys_test_node_select":
        node_id = payload
        if not node_id or not re.fullmatch(r"[a-z0-9-]+", node_id):
            return TelegramAdapter().answer_callback(callback_id, "Некоректний вузол.")
        return _send_confirmation(
            chat_id, f"🧪 Запустити self-test для вузла {node_id}?",
            f"sys_test_node_confirm:{node_id}", "sys_cancel"
        )

    if action == "sys_test_node_confirm":
        node_id = payload
        if not node_id:
            return TelegramAdapter().answer_callback(callback_id, "Некоректний вузол.")
        op, created = get_or_create_operation("test", f"telegram:{chat_id}", target=node_id)
        if not created:
            return TelegramAdapter().answer_callback(
                callback_id, f"Тест вже в процесі (#{op['operation_id'][:8]})."
            )
        begin_operation(op["operation_id"], f"Self-test вузла {node_id}")
        audit_entry(op["operation_id"], "INTERNAL_CALL", f"POST /api/nodes/{node_id}/actions action=self-test", f"telegram:{chat_id}")
        try:
            result = _call_core_api("POST", f"/api/nodes/{node_id}/actions",
                                    {"action": "self-test", "reason": f"telegram test {op['operation_id'][:8]}"},
                                    timeout=60)
            if result and "_error" in result:
                fail_operation(op["operation_id"], result["_error"])
                audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"error: {result['_error'][:200]}", f"telegram:{chat_id}")
                return TelegramAdapter().answer_callback(
                    callback_id, f"❌ Self-test {node_id} не вдався: {result['_error'][:200]}"
                )
            complete_operation(op["operation_id"], result)
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", "self-test completed", f"telegram:{chat_id}")
            return TelegramAdapter().answer_callback(
                callback_id, f"✅ Self-test вузла {node_id} завершено. ID: {op['operation_id'][:8]}"
            )
        except Exception as error:
            fail_operation(op["operation_id"], str(error))
            audit_entry(op["operation_id"], "INTERNAL_CALL_RESULT", f"exception: {error}", f"telegram:{chat_id}")
            return TelegramAdapter().answer_callback(callback_id, f"❌ Self-test {node_id} не вдався: {error}")

    if action == "sys_cancel":
        return TelegramAdapter().answer_callback(callback_id, "Операцію скасовано.")

    return TelegramAdapter().answer_callback(callback_id, "Невідома системна операція.")


def _handle_telegram_callback(callback: dict) -> dict:
    data = str(callback.get("data", ""))
    action, _, payload = data.partition(":")
    callback_id = str(callback.get("id", ""))
    chat_id = str(callback.get("message", {}).get("chat", {}).get("id", "unknown"))

    # System operations (admin only)
    if action in {"sys_status", "sys_update", "sys_update_check", "sys_update_confirm",
                  "sys_update_cancel",
                  "sys_restart", "sys_restart_core", "sys_restart_core_confirm",
                  "sys_restart_worker", "sys_restart_worker_select", "sys_restart_worker_confirm",
                  "sys_backup", "sys_backup_confirm", "sys_backup_cancel",
                  "sys_restore", "sys_restore_select", "sys_restore_confirm",
                  "sys_restore_execute", "sys_restore_cancel",
                  "sys_test", "sys_test_quick", "sys_test_quick_confirm",
                  "sys_test_full", "sys_test_full_confirm", "sys_test_node",
                  "sys_test_node_select", "sys_test_node_confirm", "sys_cancel"}:
        if not is_admin_chat(chat_id):
            return TelegramAdapter().answer_callback(callback_id, "Доступ заборонено: лише для адміністраторів.")
        if callback_id and callback_id in _telegram_system_callbacks:
            return TelegramAdapter().answer_callback(callback_id, "Операцію вже оброблено.")
        _telegram_system_callbacks.add(callback_id)
        if len(_telegram_system_callbacks) > 10000:
            _telegram_system_callbacks.clear()
        return _handle_system_callback(callback, chat_id, action, payload)

    if action == "select_brand":
        return _handle_brand_selection(callback, chat_id, payload)
    elif action == "select_character":
        return _handle_character_selection(callback, chat_id, payload)
    elif action in {"approve", "reject"}:
        if not is_admin_chat(chat_id):
            return TelegramAdapter().answer_callback(callback_id, "Доступ заборонено: лише для адміністраторів.")
        return _handle_approve_job(callback, chat_id, payload) if action == "approve" else _handle_reject_job(callback, chat_id, payload)
    elif action in {"publish_channel", "publish_all"}:
        if not is_admin_chat(chat_id):
            return TelegramAdapter().answer_callback(callback_id, "Доступ заборонено: лише для адміністраторів.")
        return _handle_publish_channel(callback, chat_id, payload) if action == "publish_channel" else _handle_publish_all(callback, chat_id, payload)
    elif action in {"sb_ok", "sb_regen", "sb_edit", "sb_reject",
                    "sb_img_ok", "sb_img_regen", "sb_img_edit", "sb_img_scene"}:
        if not is_admin_chat(chat_id):
            return TelegramAdapter().answer_callback(callback_id, "Доступ заборонено: лише для адміністраторів.")
        return _handle_storyboard_callback(callback, chat_id, action, payload)
    elif action in {"sc_ok", "sc_regen", "sc_edit", "sc_reject"}:
        if not is_admin_chat(chat_id):
            return TelegramAdapter().answer_callback(callback_id, "Доступ заборонено: лише для адміністраторів.")
        return _handle_script_callback(callback, chat_id, action, payload)
    elif action in {"vid_ok", "vid_regen", "vid_edit", "vid_reject"}:
        if not is_admin_chat(chat_id):
            return TelegramAdapter().answer_callback(callback_id, "Доступ заборонено: лише для адміністраторів.")
        return _handle_video_callback(callback, chat_id, action, payload)

    job_id = payload
    job = store.jobs.get(job_id)
    if job and action == "cancel":
        if not is_admin_chat(chat_id):
            return TelegramAdapter().answer_callback(callback_id, "Доступ заборонено: лише для адміністраторів.")
        cancel_job(job_id)
    text = f"{job_id}: {job.status.value}" if job else "Job not found"
    return TelegramAdapter().answer_callback(callback_id, text)


def _handle_brand_selection(callback: dict, chat_id: str, brand_id: str) -> dict:
    callback_id = str(callback.get("id", ""))
    pending = _telegram_pending_brands.get(chat_id)
    if not pending:
        return TelegramAdapter().answer_callback(callback_id, "Сесію закрито. Почніть спочатку.")
    TelegramAdapter().answer_callback(callback_id, f"Бренд обрано. Тепер оберіть персонажа.")
    _telegram_pending_brands.pop(chat_id, None)
    _telegram_pending_character[chat_id] = {"pending": pending, "brand_id": brand_id}
    return _send_character_selection(chat_id, brand_id)


def _send_character_selection(chat_id: str, brand_id: str) -> dict:
    characters_root = Path(os.getenv("CHARACTERS_ROOT", "characters"))
    character_dirs = sorted(characters_root.glob("*"), key=lambda p: p.name)
    characters = []
    for char_dir in character_dirs:
        char_config_path = char_dir / "character.json"
        if char_config_path.exists():
            try:
                config = read_json(char_config_path)
                characters.append({"id": char_dir.name, "name": config.get("name", char_dir.name)})
            except (ValueError, OSError):
                continue
    if not characters:
        character_id = os.getenv("TELEGRAM_DEFAULT_CHARACTER", "did_samogon")
        pending = _telegram_pending_character.get(chat_id, {}).get("pending", {})
        job = _create_job_from_telegram(pending, brand_id, character_id)
        _telegram_pending_character.pop(chat_id, None)
        _queue_storyboard(job, chat_id)
        TelegramAdapter().send_message(chat_id, f"JOB {job.job_id}\nSTATUS: {job.status.value}\nПерсонажів не знайдено, використовується {character_id}. Генерую розкадровку…")
        return job.model_dump(mode="json")
    keyboard = {"inline_keyboard": [
        [{"text": f"👤 {char['name']}", "callback_data": f"select_character:{char['id']}"}] for char in characters
    ]}
    TelegramAdapter().send_message(chat_id, "Оберіть персонажа для цього завдання:", keyboard)
    return {"status": "character_selection", "brand_id": brand_id, "characters": [c["id"] for c in characters]}


def _handle_character_selection(callback: dict, chat_id: str, character_id: str) -> dict:
    callback_id = str(callback.get("id", ""))
    pending_data = _telegram_pending_character.get(chat_id)
    if not pending_data:
        return TelegramAdapter().answer_callback(callback_id, "Сесію закрито. Почніть спочатку.")
    pending = pending_data["pending"]
    brand_id = pending_data["brand_id"]
    adapter = TelegramAdapter()
    # A callback acknowledgement is not confirmation that a Job was saved.
    try:
        adapter.answer_callback(callback_id, "Перевіряю персонажа…")
    except Exception:
        logger.warning("Telegram callback acknowledgement failed")
    try:
        load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), character_id)
        job = _create_job_from_telegram(pending, brand_id, character_id)
    except Exception as error:
        logger.exception("Telegram Job creation failed", extra={"character_id": character_id})
        error_detail = str(error)[:200] if str(error) else "невідома помилка"
        adapter.send_message(chat_id, f"❌ Не вдалося завершити створення завдання: {error_detail}\nПеревірте конфігурацію персонажа та сховище. Вибір збережено — можна повторити спробу.")
        return {"status": "creation_failed", "error": error_detail}
    _telegram_pending_character.pop(chat_id, None)
    try:
        adapter.send_message(chat_id, f"✅ Завдання {job.job_id} збережено. Воно доступне в адмінці у списку «Завдання».")
    except Exception:
        logger.warning("Telegram Job confirmation failed", extra={"job_id": job.job_id})
    try:
        _queue_storyboard(job, chat_id)
    except Exception:
        logger.exception("Telegram storyboard queue failed", extra={"job_id": job.job_id})
        adapter.send_message(chat_id, f"❌ Завдання {job.job_id} збережено, але обробку не вдалося запустити. Перевірте його стан в адмінці.")
    return job.model_dump(mode="json")


def _queue_storyboard(job, chat_id: str, revision: str | None = None) -> None:
    service = StoryboardService(store)
    service.store.update(job, JobStatus.STORYBOARD_QUEUED, "STORYBOARD QUEUED")
    executor.submit(_generate_storyboard_and_notify, job.job_id, chat_id, revision)


def _generate_storyboard_and_notify(job_id: str, chat_id: str, revision: str | None = None) -> None:
    try:
        StoryboardService(store).generate(job_id, revision)
    except Exception as error:
        logger.error("Storyboard generation failed: %s", error, extra={"job_id": job_id})
        try:
            TelegramAdapter().send_message(chat_id, f"❌ Не вдалося створити розкадровку {job_id}: {error}")
        except Exception:
            pass


def _regenerate_script_and_notify(job_id: str, chat_id: str, revision: str | None = None) -> None:
    """Regenerate script after revision request from Telegram."""
    try:
        from .pipeline import regenerate_script
        job = store.jobs.get(job_id)
        if not job:
            return
        regenerate_script(store, job)
    except Exception as error:
        logger.error("Script regeneration failed: %s", error, extra={"job_id": job_id})
        try:
            TelegramAdapter().send_message(chat_id, f"❌ Помилка перегенерації сценарію {job_id}: {error}")
        except Exception:
            pass


def _regenerate_storyboard_and_notify(job_id: str, version: int, chat_id: str,
                                      revision: str | None = None) -> None:
    try:
        StoryboardService(store).regenerate(
            job_id, version, f"telegram:{chat_id}", revision
        )
    except Exception as error:
        logger.error("Storyboard regeneration failed: %s", error, extra={"job_id": job_id})
        try:
            TelegramAdapter().send_message(chat_id, f"❌ Не вдалося оновити розкадровку {job_id}: {error}")
        except Exception:
            pass


def _handle_storyboard_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    # Image callbacks carry the reviewed image_version (and, for sb_img_scene, the
    # scene index) so that stale image callbacks are rejected server-side (Issue #36).
    raw = payload
    job_id, separator, rest = raw.partition(":")
    if not separator:
        return TelegramAdapter().answer_callback(callback_id, "Некоректна версія розкадровки")
    parts = rest.split(":")
    if not parts[0].isdigit():
        return TelegramAdapter().answer_callback(callback_id, "Некоректна версія розкадровки")
    version = int(parts[0])
    image_version = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    scene_index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    service = StoryboardService(store)
    try:
        if action == "sb_ok":
            job = service.approve(job_id, version, f"telegram:{chat_id}",
                                  expected_image_version=image_version)
            executor.submit(_prepare_and_dispatch, job)
            text = f"✅ Розкадровку {version} схвалено. Pipeline продовжено."
        elif action == "sb_reject":
            service.reject(job_id, version, f"telegram:{chat_id}")
            text = f"❌ Розкадровку {version} відхилено."
        elif action == "sb_edit":
            job = store.jobs.get(job_id)
            if not job or job.active_storyboard_version != version:
                raise StoryboardConflict("Версія розкадровки вже неактуальна")
            if job.storyboard_revision_chat_id and job.storyboard_revision_version:
                raise StoryboardConflict("Правки до розкадровки вже очікують на повідомлення")
            job.storyboard_revision_chat_id = chat_id
            job.storyboard_revision_version = version
            job.storyboard_image_revision_pending = False
            store.event(job, f"STORYBOARD {version} AWAITS REVISION TEXT")
            text = "Надішліть одним повідомленням, що потрібно змінити."
        elif action == "sb_img_ok":
            job = service.approve_images(job_id, version, f"telegram:{chat_id}",
                                         expected_image_version=image_version)
            executor.submit(_prepare_and_dispatch, job)
            text = f"🖼️ Превʼю розкадровки {version} схвалено. Відео розблоковано."
        elif action == "sb_img_regen":
            service.request_image_revision(job_id, version, f"telegram:{chat_id}", None, None,
                                           expected_image_version=image_version)
            text = "🔁 Перегенеровую всі превʼю розкадровки."
            job = store.jobs.get(job_id)
            if job:
                sb = next((s for s in job.storyboards if s.version == version), None)
                if sb and all(s.image_artifact_id for s in sb.scenes):
                    sb.image_status = "ready"
        elif action == "sb_img_edit":
            job = store.jobs.get(job_id)
            if not job or job.active_storyboard_version != version:
                raise StoryboardConflict("Версія розкадровки вже неактуальна")
            if job.storyboard_revision_chat_id and job.storyboard_image_revision_pending:
                raise StoryboardConflict("Правки до превʼю вже очікують на повідомлення")
            job.storyboard_revision_chat_id = chat_id
            job.storyboard_revision_version = version
            job.storyboard_image_revision_pending = True
            store.event(job, f"IMAGE STORYBOARD {version} AWAITS REVISION TEXT")
            text = "Надішліть правки до превʼю одним повідомленням."
        elif action == "sb_img_scene":
            service.request_image_revision(job_id, version, f"telegram:{chat_id}",
                                           [scene_index] if scene_index else None, None,
                                           expected_image_version=image_version)
            text = f"🔁 Перегенеровую превʼю сцени {scene_index or ''}."
        else:
            executor.submit(_regenerate_storyboard_and_notify, job_id, version, chat_id)
            text = "🔄 Генерую нову версію розкадровки."
        return TelegramAdapter().answer_callback(callback_id, text)
    except KeyError:
        return TelegramAdapter().answer_callback(callback_id, "Job або розкадровку не знайдено")
    except StoryboardConflict as error:
        return TelegramAdapter().answer_callback(callback_id, str(error))


def _handle_script_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    job_id = payload
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job не знайдено")
    from .pipeline import approve_script, request_script_revision, regenerate_script
    from .storyboard_telegram import render_script, script_keyboard
    try:
        if action == "sc_ok":
            job = approve_script(store, job, f"telegram:{chat_id}")
            executor.submit(_prepare_and_dispatch, job)
            text = f"✅ Сценарій {job_id} схвалено."
        elif action == "sc_edit":
            if job.status not in {JobStatus.SCRIPT_PENDING_APPROVAL,
                                  JobStatus.SCRIPT_REVISION_REQUESTED}:
                raise ValueError(f"Сценарій не очікує правок (status={job.status.value})")
            if job.script_revision_pending and job.script_revision_chat_id:
                raise ValueError("Правки до сценарію вже очікують на повідомлення")
            job.script_revision_chat_id = chat_id
            job.script_revision_pending = True
            store.event(job, f"SCRIPT AWAITS REVISION TEXT")
            text = "Надішліть одним повідомленням, що потрібно змінити у сценарії."
        elif action == "sc_regen":
            if job.status == JobStatus.SCRIPT_REVISION_REQUESTED:
                job = regenerate_script(store, job)
                executor.submit(_prepare_and_dispatch, job)
                text = "🔄 Перегенеровую сценарій…"
            elif job.status == JobStatus.SCRIPT_PENDING_APPROVAL:
                job.script = None
                job.version += 1
                store.transition(job, JobStatus.SCRIPT_REVISION_REQUESTED,
                                 f"SCRIPT REGENERATION REQUESTED by telegram:{chat_id}")
                executor.submit(_prepare_and_dispatch, job)
                text = "🔄 Перегенеровую сценарій…"
            else:
                text = f"Неможливо перегенерувати (status={job.status.value})"
        elif action == "sc_reject":
            store.update(job, JobStatus.CANCELLED, "SCRIPT REJECTED via Telegram")
            text = f"❌ Сценарій {job_id} відхилено."
        else:
            text = f"Невідома дія: {action}"
        return TelegramAdapter().answer_callback(callback_id, text)
    except ValueError as error:
        return TelegramAdapter().answer_callback(callback_id, str(error))


def _handle_video_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    # Payload may be "job_id" (legacy) or "job_id:version" (version-bound preview)
    parts = payload.split(":", 1)
    job_id = parts[0]
    expected_version = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job не знайдено")
    if job.source and not job.source.endswith(":" + chat_id):
        if action in {"vid_ok", "vid_reject"}:
            return TelegramAdapter().answer_callback(callback_id, "Доступ заборонено: chat не є власником job")
    from .pipeline import approve_video, request_video_revision
    try:
        if action == "vid_ok":
            job = approve_video(store, job, f"telegram:{chat_id}",
                                expected_version=expected_version)
            text = f"✅ Відео {job_id} схвалено."
        elif action == "vid_edit":
            if job.status != JobStatus.VIDEO_PENDING_APPROVAL:
                raise ValueError(f"Відео не очікує правок (status={job.status.value})")
            if job.video_revision_pending and job.video_revision_chat_id:
                raise ValueError("Правки до відео вже очікують на повідомлення")
            job.video_revision_chat_id = chat_id
            job.video_revision_pending = True
            store.event(job, "VIDEO AWAITS REVISION TEXT")
            text = "Надішліть одним повідомленням, що потрібно змінити у відео."
        elif action == "vid_regen":
            if job.video_regenerating:
                return TelegramAdapter().answer_callback(callback_id, "🔄 Відео вже генерується")
            if job.status == JobStatus.VIDEO_PENDING_APPROVAL:
                job = request_video_revision(store, job, "regenerate", f"telegram:{chat_id}")
            elif job.status != JobStatus.VIDEO_REVISION_REQUESTED:
                text = f"Неможливо перегенерувати (status={job.status.value})"
                return TelegramAdapter().answer_callback(callback_id, text)
            executor.submit(_finalize_video_regenerate, job)
            text = "🔄 Перегенеровую відео…"
        elif action == "vid_reject":
            store.update(job, JobStatus.CANCELLED, "VIDEO REJECTED via Telegram")
            text = f"❌ Відео {job_id} відхилено."
        else:
            text = f"Невідома дія: {action}"
        return TelegramAdapter().answer_callback(callback_id, text)
    except ValueError as error:
        return TelegramAdapter().answer_callback(callback_id, str(error))


def _finalize_video_regenerate(job) -> None:
    """Re-run assembly for video revision using active storyboard artifacts."""
    try:
        from .pipeline import finalize_job_safe
        job = store.jobs.get(job.job_id)
        if not job:
            return
        if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
            logger.info("Video regeneration skipped: job is %s", job.status.value,
                        extra={"job_id": job.job_id})
            return
        from pathlib import Path
        images = _collect_active_storyboard_images(job)
        if images:
            with store.lock:
                job.status = JobStatus.ASSETS_READY
                job.video_regenerating = True
                store._save(job)
            finalize_job_safe(store, job, images)
        else:
            with store.lock:
                job.video_regenerating = False
                store.update(job, JobStatus.VIDEO_FAILED, "NO IMAGES FOR RE-ASSEMBLY")
            TelegramAdapter().send_message(
                job.source.split(":", 2)[1],
                f"❌ Не знайдено кадрів для перезбірки {job.job_id}.",
            )
    except Exception as error:
        logger.error("Video regeneration failed: %s", error, extra={"job_id": job.job_id})
        try:
            with store.lock:
                job.video_regenerating = False
            chat_id = job.source.split(":", 2)[1]
            TelegramAdapter().send_message(chat_id, f"❌ Помилка перегенерування {job.job_id}: {error}")
        except Exception:
            pass


def _collect_active_storyboard_images(job) -> list:
    """Collect ordered images from the active storyboard version only.

    Uses registered artifact paths when available; falls back to
    the active storyboard's image directory without globbing across
    all storyboard versions.
    """
    from pathlib import Path
    from .artifacts import _digest
    root = store.root / job.job_id
    active_version = job.active_storyboard_version
    storyboard = None
    if active_version and job.storyboards:
        storyboard = next((s for s in job.storyboards if s.version == active_version), None)
    if storyboard and hasattr(storyboard, "scenes") and storyboard.scenes:
        images = []
        for scene in storyboard.scenes:
            scene_id = getattr(scene, "scene_id", None)
            if scene_id:
                for ext in ("png", "jpg", "jpeg", "webp"):
                    candidate = root / "images" / f"{scene_id}.{ext}"
                    if candidate.exists():
                        images.append(candidate)
                        break
        if images:
            return sorted(images, key=lambda p: p.name)
    image_dir = root / "images"
    if image_dir.exists():
        return sorted(image_dir.glob("*.*"), key=lambda p: p.name)
    storyboard_dir = root / "storyboard"
    if storyboard_dir.exists():
        return sorted(storyboard_dir.glob("**/*.png"), key=lambda p: p.name)
    return []


def _handle_approve_job(callback: dict, chat_id: str, job_id: str) -> dict:
    callback_id = str(callback.get("id", ""))
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job not found")
    if job.approval_status == "approved":
        return TelegramAdapter().answer_callback(callback_id, f"{job_id} вже схвалено.")
    job.approval_status = "approved"
    job.status = JobStatus.READY
    store.update(job, JobStatus.READY, "APPROVED via Telegram")
    channels = store.repository.list_channels(job.brand_id)
    active_channels = [ch for ch in channels if ch.enabled]
    if not active_channels:
        TelegramAdapter().send_message(chat_id, f"⚠️ {job_id} схвалено, але для бренду {job.brand_id} немає активних каналів.")
        return TelegramAdapter().answer_callback(callback_id, f"{job_id} схвалено (каналів немає)")
    keyboard = [[{"text": f"📤 {ch.channel_type}: {ch.target}", "callback_data": f"publish_channel:{ch.channel_id}:{job_id}"}] for ch in active_channels]
    keyboard.append([{"text": "📢 Опублікувати всюди", "callback_data": f"publish_all:{job_id}"}])
    for admin_chat in _admin_chat_ids():
        try:
            TelegramAdapter().send_message(admin_chat, f"✅ {job_id} схвалено. Оберіть канали для публікації:",
                                          {"inline_keyboard": keyboard})
        except Exception:
            pass
    return TelegramAdapter().answer_callback(callback_id, f"{job_id} схвалено")


def _handle_reject_job(callback: dict, chat_id: str, job_id: str) -> dict:
    callback_id = str(callback.get("id", ""))
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job not found")
    job.approval_status = "rejected"
    store.update(job, JobStatus.CANCELLED, "REJECTED via Telegram")
    return TelegramAdapter().answer_callback(callback_id, f"{job_id} відхилено")


def _handle_publish_channel(callback: dict, chat_id: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    channel_id, _, job_id = payload.partition(":")
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job not found")
    channel = store.repository.get_channel(channel_id)
    if not channel:
        return TelegramAdapter().answer_callback(callback_id, "Канал не знайдено")
    result = _publish_to_channel(job, channel)
    status_text = "✅ Опубліковано" if result.get("status") == "PUBLISHED" else f"❌ Помилка: {result.get('error', 'невідомо')}"
    return TelegramAdapter().answer_callback(callback_id, f"{channel.channel_type} ({channel.target}): {status_text}")


def _handle_publish_all(callback: dict, chat_id: str, job_id: str) -> dict:
    callback_id = str(callback.get("id", ""))
    job = store.jobs.get(job_id)
    if not job:
        return TelegramAdapter().answer_callback(callback_id, "Job not found")
    channels = [ch for ch in store.repository.list_channels(job.brand_id) if ch.enabled]
    results = []
    for channel in channels:
        result = _publish_to_channel(job, channel)
        status = "✅" if result.get("status") == "PUBLISHED" else "❌"
        results.append(f"{status} {channel.channel_type}: {channel.target}")
    summary = "\n".join(results) or "Немає активних каналів"
    for admin_chat in _admin_chat_ids():
        try:
            TelegramAdapter().send_message(admin_chat, f"📊 Підсумок публікації {job_id}:\n{summary}")
        except Exception:
            pass
    return TelegramAdapter().answer_callback(callback_id, "Публікацію завершено")


def _publish_to_channel(job, channel) -> dict:
    if job.approval_status != "approved":
        return {"status": "REJECTED", "error": "Job не схвалено для публікації"}
    from .api.job_helpers import _has_publisher_worker
    if not _has_publisher_worker(store):
        return {"status": "NOT_CONFIGURED", "error": "Publisher Worker не зареєстровано; публікація неможлива без Publisher Worker"}
    from .api.job_helpers import _enqueue_publish_task
    queued = _enqueue_publish_task(store, job, channel.channel_type)
    if queued is None:
        return {"status": "NOT_CONFIGURED", "error": f"Publisher для {channel.channel_type} не налаштовано"}
    return {"status": "QUEUED", "task_id": queued["task_id"], "channel": channel.channel_type}


def _send_approval_request(job) -> None:
    text = (
        f"📋 Новий Job потребує затвердження\n"
        f"ID: {job.job_id}\n"
        f"Бренд: {job.brand_id}\n"
        f"Тема: {job.topic[:200]}\n"
        f"Джерело: {job.source}"
    )
    keyboard = {"inline_keyboard": [
        [{"text": "✅ Схвалити", "callback_data": f"approve:{job.job_id}"},
         {"text": "❌ Відхилити", "callback_data": f"reject:{job.job_id}"}]
    ]}
    for admin_chat in _admin_chat_ids():
        try:
            TelegramAdapter().send_message(admin_chat, text, keyboard)
        except Exception as error:
            logger.warning("Failed to send approval to %s: %s", admin_chat, error)


def _handle_telegram_message(chat_id: str, source_id: str, text: str, message: dict) -> dict:
    if text == "/start":
        return TelegramAdapter().send_message(chat_id, "Vertep Bot підключений.\nНадішліть тему для створення контенту.")
    if text in {"/system", "/admin"}:
        if not is_admin_chat(chat_id):
            return TelegramAdapter().send_message(chat_id, "Доступ заборонено.")
        return _send_system_menu(chat_id)
    if text.startswith("/operation "):
        operation_id = text.split(maxsplit=1)[1].strip()
        operation = get_operation(operation_id)
        if not operation:
            return TelegramAdapter().send_message(chat_id, "Операцію не знайдено.")
        return TelegramAdapter().send_message(chat_id, _format_operation(operation))
    if text.startswith("/status"):
        active = sum(job.status not in {JobStatus.READY, JobStatus.PUBLISHED, JobStatus.FAILED, JobStatus.CANCELLED}
                     for job in store.jobs.values())
        return TelegramAdapter().send_message(chat_id, f"Jobs: {len(store.jobs)}\nActive: {active}\nQueue: {task_queue.depth()}")
    if text.startswith("/jobs"):
        latest = list(store.jobs.values())[-10:]
        summary = "\n".join(f"{job.job_id} {job.status.value} — {job.topic[:50]}" for job in latest) or "No jobs"
        return TelegramAdapter().send_message(chat_id, summary)
    if text.startswith("/workers"):
        summary = "\n".join(f"{worker['node_name']} {worker['status']} {worker.get('gpu_name','')}"
                             for worker in workers()) or "No workers"
        return TelegramAdapter().send_message(chat_id, summary)
    if text.startswith("/job "):
        job = store.jobs.get(text.split(maxsplit=1)[1].strip())
        if not job:
            return TelegramAdapter().send_message(chat_id, "Job not found")
        scenes = "\n".join(f"{scene.scene_id}: {scene.status.value} ({len(scene.attempts)} attempt(s))"
                            for scene in job.scenes) or "Scenes are not planned yet"
        return TelegramAdapter().send_message(chat_id, f"{job.job_id}: {job.status.value}\n{scenes}")
    if any(text.startswith(command + " ") for command in ("/retry", "/cancel", "/approve", "/publish")):
        command, job_id = text.split(maxsplit=1)
        job = store.jobs.get(job_id.strip())
        if not job:
            return TelegramAdapter().send_message(chat_id, "Job not found")
        if command == "/retry":
            retry_job(job.job_id)
        elif command == "/cancel":
            cancel_job(job.job_id)
        elif command == "/approve":
            approve_job(job.job_id)
        else:
            publish_job(job.job_id)
        return TelegramAdapter().send_message(chat_id, f"{job.job_id}: {job.status.value}")
    revision_job = next((job for job in store.jobs.values()
                         if job.storyboard_revision_chat_id == chat_id), None)
    if revision_job and revision_job.storyboard_revision_version:
        version = revision_job.storyboard_revision_version
        revision_job.storyboard_revision_chat_id = None
        revision_job.storyboard_revision_version = None
        executor.submit(_regenerate_storyboard_and_notify,
                        revision_job.job_id, version, chat_id, text)
        return TelegramAdapter().send_message(chat_id, "✍️ Правки прийнято. Генерую нову версію…")
    # --- Script revision text ---
    script_rev_job = next((job for job in store.jobs.values()
                           if job.script_revision_chat_id == chat_id
                           and job.script_revision_pending), None)
    if script_rev_job:
        script_rev_job.script_revision_chat_id = None
        script_rev_job.script_revision_pending = False
        from .pipeline import request_script_revision, regenerate_script
        try:
            request_script_revision(store, script_rev_job, text, f"telegram:{chat_id}")
            executor.submit(_regenerate_script_and_notify,
                            script_rev_job.job_id, chat_id, text)
            return TelegramAdapter().send_message(chat_id, "✍️ Правки до сценарію прийнято. Генерую нову версію…")
        except Exception as error:
            return TelegramAdapter().send_message(chat_id, f"❌ Помилка: {error}")
    # --- Video revision text ---
    video_rev_job = next((job for job in store.jobs.values()
                          if job.video_revision_chat_id == chat_id
                          and job.video_revision_pending), None)
    if video_rev_job:
        video_rev_job.video_revision_chat_id = None
        video_rev_job.video_revision_pending = False
        from .pipeline import request_video_revision
        try:
            request_video_revision(store, video_rev_job, text, f"telegram:{chat_id}")
            executor.submit(_finalize_video_regenerate, video_rev_job)
            return TelegramAdapter().send_message(chat_id, "✍️ Правки до відео прийнято. Перезбираю…")
        except Exception as error:
            return TelegramAdapter().send_message(chat_id, f"❌ Помилка: {error}")
    attachments = {key: message.get(key) for key in ("photo", "video", "document", "audio") if message.get(key)}
    _telegram_pending_brands[chat_id] = {"text": text, "source_id": source_id, "message": message, "attachments": attachments}
    brands_dir = Path(os.getenv("BRANDS_ROOT", "brands"))
    brands = []
    for path in brands_dir.glob("*/brand.json"):
        try:
            brands.append(BrandConfig.model_validate(read_json(path)))
        except ValueError:
            continue
    if not brands:
        return _send_character_selection(chat_id, None)
    keyboard = {"inline_keyboard": [
        [{"text": f"📁 {brand.name}", "callback_data": f"select_brand:{brand.id}"}] for brand in brands
    ]}
    TelegramAdapter().send_message(chat_id, "Оберіть бренд для цього завдання:", keyboard)
    return {"status": "brand_selection", "brands": [b.id for b in brands]}


def _save_telegram_attachments(job, message: dict, root: Path) -> None:
    attachments = {key: message.get(key) for key in ("photo", "video", "document", "audio") if message.get(key)}
    if attachments:
        reference = root / job.job_id / "references" / "telegram.json"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_text(json.dumps(attachments, ensure_ascii=False, indent=2), encoding="utf-8")
        register_artifact(job, root, reference, "input", workflow="telegram:webhook")
        store.event(job, "TELEGRAM REFERENCES RECORDED")
    adapter = TelegramAdapter()
    file_ids = []
    if message.get("photo"):
        file_ids.append(message["photo"][-1].get("file_id"))
    for key in ("video", "document", "audio"):
        if message.get(key):
            file_ids.append(message[key].get("file_id"))
    if adapter.configured():
        for file_id in filter(None, file_ids):
            try:
                data, filename = adapter.download_file(file_id, int(os.getenv("TELEGRAM_MAX_FILE_BYTES", "26214400")))
                downloaded = root / job.job_id / "references" / Path(filename).name
                if downloaded.exists():
                    downloaded = downloaded.with_name(f"{downloaded.stem}-{hashlib.sha256(data).hexdigest()[:8]}{downloaded.suffix}")
                downloaded.write_bytes(data)
                register_artifact(job, root, downloaded, "input", workflow="telegram:download")
            except Exception as error:
                store.event(job, f"TELEGRAM DOWNLOAD FAILED: {error}")


def _create_job_from_telegram(pending: dict, brand_id: str, character_id: str | None = None):
    text = pending["text"]
    source_id = pending["source_id"]
    message = pending["message"]
    chat_id = message.get("chat", {}).get("id", "unknown")
    if character_id is None:
        character_id = os.getenv("TELEGRAM_DEFAULT_CHARACTER", "did_samogon")
    source = f"telegram:{chat_id}:{source_id or 'unknown'}"
    if source_id and store.repository.has_telegram_update(chat_id, source_id):
        existing = next((job for job in store.jobs.values() if job.source == source), None)
        if existing:
            return existing
    for existing in store.jobs.values():
        if existing.source == source:
            return existing
    try:
        load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), character_id)
    except Exception as error:
        raise HTTPException(400, f"Unknown character: {character_id}") from error
    job = store.create(text, character_id, int(os.getenv("TELEGRAM_DEFAULT_PRIORITY", "5")), source)
    job.brand_id = brand_id
    job.status = JobStatus.STORYBOARD_QUEUED
    job.approval_status = "pending"
    store.update(job, JobStatus.STORYBOARD_QUEUED, f"STORYBOARD QUEUED for brand {brand_id or 'default'}")
    if source_id:
        store.repository.record_telegram_update(chat_id, source_id, message)
    if pending.get("attachments"):
        _save_telegram_attachments(job, message, store.root)
    return job


@app.post("/api/telegram/setup")
def telegram_setup(body: TelegramSetup):
    from core.first_run import ensure_secret_store
    secrets = ensure_secret_store()
    token = secrets.get("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise HTTPException(400, "TELEGRAM_BOT_TOKEN is not configured")
    webhook_secret = body.webhook_secret or os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if webhook_secret:
        os.environ["TELEGRAM_WEBHOOK_SECRET"] = webhook_secret
    save_telegram_settings(allowed_chat_ids=body.allowed_chat_ids, admin_chat_ids=body.admin_chat_ids)
    return {"status": "saved", "message": "Telegram polling configuration saved; webhook is legacy"}


def _start_telegram_polling() -> None:
    global telegram_polling_service
    token = _integration_secret("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN") or ""
    if not token:
        return
    if os.getenv("TELEGRAM_POLLING_ENABLED", "true").lower() != "true":
        return
    telegram_polling_service = TelegramPollingService(
        token=token,
        on_update=_process_telegram_update,
    )
    telegram_polling_service.start()
    logger.info("Telegram polling started")


def _restart_telegram_polling() -> None:
    _stop_telegram_polling()
    _start_telegram_polling()


def _stop_telegram_polling() -> None:
    global telegram_polling_service
    if telegram_polling_service is not None:
        telegram_polling_service.stop()
        telegram_polling_service = None
        logger.info("Telegram polling stopped")


def _process_telegram_update(update: dict) -> None:
    callback = update.get("callback_query")
    if callback:
        _handle_telegram_callback(callback)
        return
    message = update.get("message") or {}
    text = str(message.get("text") or message.get("caption") or "").strip()
    if not text:
        logger.debug("Telegram update skipped: no text")
        return
    chat_id = str(message.get("chat", {}).get("id", "unknown"))
    allowed = get_allowed_chat_ids()
    admin_ids = set(get_admin_chat_ids())
    if allowed and chat_id not in allowed and chat_id not in admin_ids:
        logger.warning("Telegram message rejected: chat_id not allowed", extra={"chat_id": chat_id})
        try:
            TelegramAdapter().send_message(chat_id, "Доступ заборонено.")
        except Exception:
            pass
        return
    source_id = str(message.get("message_id", ""))
    if text == "/start":
        try:
            TelegramAdapter().send_message(chat_id, "Vertep Bot підключений.\nНадішліть тему для створення контенту.")
        except Exception:
            pass
        return
    _handle_telegram_message(chat_id, source_id, text, message)


@app.get("/api/telegram/status")
def telegram_status():
    from core.first_run import ensure_secret_store
    secrets = ensure_secret_store()
    token_configured = bool(secrets.get("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN"))
    public_url = os.getenv("PUBLIC_URL", "")
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    settings = load_telegram_settings()
    allowed_chat_ids = settings.get("allowed_chat_ids", "")
    admin_chat_ids = settings.get("admin_chat_ids", "")
    polling_enabled = os.getenv("TELEGRAM_POLLING_ENABLED", "true").lower() == "true"
    polling_status = "stopped"
    bot_username = None
    last_update_id = None
    last_message_at = None
    if telegram_polling_service is not None:
        polling_status = "running" if telegram_polling_service.running else "stopped"
        bot_username = _get_bot_username()
        last_update_id = telegram_polling_service.last_update_id
        last_message_at = telegram_polling_service.last_message_at
    return {
        "configured": token_configured,
        "webhook_url": f"{public_url.rstrip('/')}/api/telegram/webhook" if public_url else "",
        "public_url": public_url,
        "webhook_secret_configured": bool(webhook_secret),
        "allowed_chat_ids": allowed_chat_ids,
        "admin_chat_ids": admin_chat_ids,
        "polling_enabled": polling_enabled,
        "polling_status": polling_status,
        "bot_username": bot_username,
        "last_update_id": last_update_id,
        "last_message_at": last_message_at,
    }


def _get_bot_username() -> str | None:
    try:
        adapter = TelegramAdapter()
        if not adapter.configured():
            return None
        result = adapter.get_me()
        if result.get("ok"):
            return result.get("result", {}).get("username")
    except Exception:
        pass
    return None


def _build_telegram_status() -> dict:
    from core.first_run import ensure_secret_store
    secrets = ensure_secret_store()
    token_configured = bool(secrets.get("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN"))
    if not token_configured:
        return {"enabled": False, "mode": "polling", "status": "not_configured"}
    polling_enabled = os.getenv("TELEGRAM_POLLING_ENABLED", "true").lower() == "true"
    status = "running" if telegram_polling_service and telegram_polling_service.running else "stopped"
    if telegram_polling_service and telegram_polling_service.last_error:
        status = "error"
    return {
        "enabled": True,
        "mode": "polling" if polling_enabled else "disabled",
        "status": status,
        "bot_username": _get_bot_username(),
        "last_update_id": telegram_polling_service.last_update_id if telegram_polling_service else None,
        "last_message_at": telegram_polling_service.last_message_at if telegram_polling_service else None,
        "last_error": telegram_polling_service.last_error if telegram_polling_service else None,
        "consecutive_failures": telegram_polling_service._consecutive_failures if telegram_polling_service else 0,
    }


@app.get("/api/telegram/bot-info")
def telegram_bot_info():
    from core.first_run import ensure_secret_store
    secrets = ensure_secret_store()
    token_configured = bool(secrets.get("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN"))
    if not token_configured:
        return {"configured": False}
    adapter = TelegramAdapter()
    try:
        me = adapter.get_me()
        if not me.get("ok"):
            return {"configured": True, "ok": False, "error": me.get("description")}
        return {
            "configured": True,
            "ok": True,
            "bot_username": me.get("result", {}).get("username"),
            "bot_id": me.get("result", {}).get("id"),
        }
    except (httpx.HTTPError, RuntimeError) as error:
        return {"configured": True, "ok": False, "error": str(error)}


def _get_system_resources() -> dict | None:
    try:
        return {
            "cpu": int(psutil.cpu_percent(interval=0.1)),
            "ram": int(psutil.virtual_memory().percent),
            "disk": int(psutil.disk_usage('/').percent),
        }
    except Exception:
        return None


@app.get("/api/status")
def system_status():
    try:
        with socket.create_connection((os.getenv("POSTGRES_HOST", "postgres"), 5432), timeout=1):
            postgres = "OK"
    except OSError:
        postgres = "OFFLINE"
    scheduled = sorted((job.scheduled_for for job in store.jobs.values()
                        if job.status == JobStatus.NEW and job.scheduled_for and not _job_is_due(job)))
    return {"core": "OK", "version": application_version(), "system": get_system_state(), "storage": "OK", "redis": "OK" if task_queue.backend == "redis" else "OFFLINE",
            "postgres": postgres,
            "queue": {"backend": task_queue.backend, "depth": task_queue.depth(),
                      "inflight": task_queue.inflight_depth(),
                      "dead_letter": len(task_queue.dead_letters())},
            "scheduler": {"pending": len(scheduled), "next_run": scheduled[0] if scheduled else None},
            "orchestration": {"active_jobs": sum(job.status not in {JobStatus.READY, JobStatus.PUBLISHED,
                                                                       JobStatus.FAILED, JobStatus.CANCELLED}
                                                  for job in store.jobs.values()),
                              "active_scenes": sum(scene.status == StageStatus.RUNNING
                                                   for job in store.jobs.values() for scene in job.scenes)},
             "ollama": "STUB" if os.getenv("DEMO_MODE", "true").lower() == "true" else "CONFIGURED",
             "telegram": _build_telegram_status(),
             "providers": provider_matrix(),
             "resources": _get_system_resources(),
             "update": update_status(), "workers": workers()}

@app.get("/status")
def status_page():
    return HTMLResponse("""<!DOCTYPE html>
<html>
<head>
  <title>Vertep Status</title>
  <meta charset="utf-8">
  <style>
    body { font-family: sans-serif; margin: 2rem; background: #0f172a; color: #e2e8f0; }
    h1 { font-size: 1.5rem; margin-bottom: 1rem; }
    pre { background: #1e293b; padding: 1rem; border-radius: 0.5rem; overflow-x: auto; }
    .ok { color: #4ade80; } .off { color: #f87171; } .warn { color: #facc15; }
  </style>
</head>
<body>
  <h1>Vertep Status</h1>
  <pre id="status">Loading...</pre>
  <script>
    async function load() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        const el = document.getElementById('status');
        let text = JSON.stringify(data, null, 2);
        text = text.replace(/"OK"/g, '<span class="ok">"OK"</span>');
        text = text.replace(/"OFFLINE"/g, '<span class="off">"OFFLINE"</span>');
        text = text.replace(/"STUB"/g, '<span class="warn">"STUB"</span>');
        text = text.replace(/"NOT CONFIGURED"/g, '<span class="off">"NOT CONFIGURED"</span>');
        el.innerHTML = text;
      } catch (e) {
        document.getElementById('status').textContent = 'Failed to load status: ' + e;
      }
    }
    load();
    setInterval(load, 5000);
  </script>
</body>
</html>""")









# Text model and voice model routes are defined in core/api/models.py

@app.get("/api/system/update")
def web_update_status():
    return {**update_status(), "system": get_system_state()}


@app.post("/api/system/recovery/normal")
def recover_normal_operation():
    status = system_status()
    failed = [name for name in ("core", "postgres", "redis") if status.get(name) != "OK"]
    if failed:
        raise HTTPException(409, "Recovery health checks failed: " + ", ".join(failed))
    return set_system_state(SystemState.NORMAL, "Administrator confirmed healthy runtime recovery")


def _read_optional_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


@app.get("/api/system/roles")
def local_roles_status():
    definitions = node_roles()
    plan = _read_optional_json(config_root() / "deployment-plan.json")
    deployment = _read_optional_json(config_root() / "deployment-status.json")
    request = _read_optional_json(config_root() / "deployment-request.json")
    active = plan.get("additional_roles", []) if plan.get("role") == "core" else []
    queued = bool(request)
    return {
        "node_role": plan.get("role") or installation().get("node_role") or "core",
        "active_roles": active,
        "available_roles": [
            {"id": role, "label": definition.get("label", role),
             "services": [service for service in definition.get("services", [])
                          if service not in {"worker", "update-agent"}],
             "capabilities": definition.get("capabilities", [])}
            for role, definition in definitions.items() if role != "core"
            and isinstance(definition, dict)
        ],
        "deployment": deployment,
        "queued": queued,
    }


@app.post("/api/system/roles")
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

@app.get("/api/system/update/readiness")
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

@app.post("/api/system/update/check")
def web_update_check():
    try:
        return request_update("check")
    except (RuntimeError, FileExistsError) as error:
        raise HTTPException(409, str(error)) from error
    except OSError as error:
        raise HTTPException(503, f"Update agent state directory is unavailable: {error}") from error

@app.post("/api/system/update/run")
def web_update_run():
    try:
        return request_update("update")
    except (RuntimeError, FileExistsError) as error:
        raise HTTPException(409, str(error)) from error
    except OSError as error:
        raise HTTPException(503, f"Update agent state directory is unavailable: {error}") from error


@app.post("/api/system/update/restart")
def web_server_restart():
    try:
        return request_update("restart")
    except (RuntimeError, FileExistsError) as error:
        raise HTTPException(409, str(error)) from error
    except OSError as error:
        raise HTTPException(503, f"Update agent state directory is unavailable: {error}") from error


@app.get("/api/system/update/rolling")
def rolling_update_status():
    return rollout_status()


@app.post("/api/system/update/rolling/cancel")
def cancel_rolling_update():
    return cancel_rollout()


@app.post("/api/system/update/rolling/rollback")
def rollback_canary():
    result = rollback_ready_nodes(store.workers)
    reconcile_rollout(store.workers)
    return result


@app.post("/api/system/update/rolling/promote")
def promote_canary():
    try:
        return promote_rollout()
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/system/update/rolling")
def begin_rolling_update(payload: RollingUpdateRequest):
    registered = {node["node_id"] for node in registered_nodes() if not node.get("revoked_at")}
    unknown = sorted(set(payload.node_ids) - registered)
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

@app.get("/api/node/status/{node_name}")
def node_system_status(node_name: str, request: Request):
    if not _valid_worker_request(node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    return system_status()

# Settings, integrations and logo routes are defined in core/api/settings.py


async def _internal_api(method: str, base_environment: str, path: str,
                        payload: dict | None = None) -> dict:
    base = os.getenv(base_environment, "").rstrip("/")
    if not base:
        raise HTTPException(503, f"Сервіс не налаштований: {base_environment} не встановлено")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.request(method, f"{base}{path}", json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.ConnectError:
        raise HTTPException(502, f"Сервіс {base_environment} недоступний на {base}. Перевірте, чи запущений backup-сервіс.")
    except httpx.TimeoutException:
        raise HTTPException(504, f"Таймаут з'єднання з {base_environment} ({base}). Сервіс не відповідає.")
    except httpx.HTTPStatusError as error:
        raise HTTPException(502, f"Помилка від {base_environment}: {error.response.text[:500]}") from error
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(503, f"Недоступний внутрішній сервіс: {error}") from error


@app.get("/api/system/backups")
async def system_backups():
    return await _internal_api("GET", "BACKUP_URL", "/snapshots")


@app.get("/api/system/state")
def system_state_api():
    from .system_state import get_system_state
    return get_system_state()


@app.post("/api/system/state")
def set_emergency_state(payload: dict | None = None):
    """Durably switch CORE to EMERGENCY.

    Called by the Backup Node when a restore fails (Issue #36/#16). The node POSTs
    here and then GETs back to verify the state actually changed; unavailability of
    this endpoint must not be treated as confirmation the system is safe.
    """
    from .system_state import SystemState, get_system_state, set_system_state
    body = payload or {}
    reason = str(body.get("reason") or "EMERGENCY")[:500]
    operation_id = body.get("operation_id")
    set_system_state(SystemState.EMERGENCY, reason, operation_id)
    return get_system_state()


@app.get("/api/system/backups/{snapshot_id}/restore/progress")
async def restore_progress_proxy(snapshot_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
        raise HTTPException(422, "Invalid snapshot identifier")
    return await _internal_api("GET", "BACKUP_URL", f"/snapshots/{snapshot_id}/restore/progress")


@app.get("/api/system/license")
async def system_license():
    return await _internal_api("GET", "LICENSE_MANAGER_URL", "/status")


@app.get("/api/system/installation-manifest")
def installation_manifest():
    manifest_path = config_root() / "installation-manifest.json"
    if manifest_path.is_file():
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise HTTPException(503, "Installation manifest is unreadable") from error
    return {key: item for key, item in installation().items() if key != "administrator"}


@app.post("/api/system/backups")
async def create_system_backup():
    return await _internal_api("POST", "BACKUP_URL", "/snapshots", {
        "job_id": "system-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), "request": {}})


@app.post("/api/system/backups/{snapshot_id}/restore")
async def restore_system_backup(snapshot_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
        raise HTTPException(422, "Invalid snapshot identifier")
    return await _internal_api("POST", "BACKUP_URL", f"/snapshots/{snapshot_id}/restore")


@app.get("/api/system/models")
async def system_models():
    return await _internal_api("GET", "OLLAMA_URL", "/api/tags")


@app.post("/api/system/models/pull")
async def pull_system_model(request: Request):
    payload = await request.json()
    name = str(payload.get("name", ""))
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._:/+-]{0,127}", name):
        raise HTTPException(422, "Invalid model name")
    return await _internal_api("POST", "OLLAMA_URL", "/api/pull", {"name": name, "stream": False})


@app.delete("/api/system/models/{name:path}")
async def delete_system_model(name: str):
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._:/+-]{0,127}", name):
        raise HTTPException(422, "Invalid model name")
    return await _internal_api("DELETE", "OLLAMA_URL", "/api/delete", {"name": name})


@app.get("/api/system/certificates")
async def system_certificates():
    return await _internal_api("GET", "CERTIFICATE_MANAGER_URL", "/certificate")


@app.post("/api/system/certificates/renew")
async def renew_system_certificate():
    return await _internal_api("POST", "CERTIFICATE_MANAGER_URL", "/certificate/renew")


@app.get("/api/operations")
async def list_operations_api(limit: int = 50):
    return list_operations(limit)


@app.get("/api/operations/{operation_id}")
async def get_operation_api(operation_id: str):
    operation = get_operation(operation_id)
    if not operation:
        raise HTTPException(404, "Operation not found")
    return operation


@app.post("/api/system/restart")
async def system_restart(payload: dict | None = None):
    """Restart CORE services or a specific worker node.

    Telegram and Web UI both call this endpoint — no duplicated logic.
    """
    body = payload or {}
    target = str(body.get("target", "core"))
    if target == "core":
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
            return {"target": "core", "status": "restart_requested", "message": "vertep-core.service restart initiated"}
        except FileNotFoundError:
            return {"target": "core", "status": "restart_requested",
                    "message": "systemctl not available; restart must be performed manually"}
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "CORE restart timed out")
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(500, f"CORE restart failed: {error}") from error
    node_id = str(body.get("node_id", "")).strip()
    if not node_id or not re.fullmatch(r"[a-z0-9-]+", node_id):
        raise HTTPException(422, "node_id is required for worker restart")
    worker = store.workers.get(node_id)
    if not worker:
        raise HTTPException(404, f"Worker {node_id} is not registered or offline")
    worker["desired_state"] = "RESTARTING"
    worker["status"] = "UPDATING"
    worker["state_reason"] = "restart via Telegram API"
    worker["state_changed_at"] = utc_now()
    store.save_worker(worker)
    audit_entry(worker["desired_state"], "restart", f"node={node_id}", f"telegram:{body.get('requested_by', 'telegram')}")
    return {"target": "node", "node_id": node_id, "status": "restart_requested",
            "message": f"Worker {node_id} marked for restart"}


@app.post("/api/system/test")
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
    from .health_checks import run_checks, health_status
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
            from core.certificates import list_certificates
            certs = list_certificates()
            result["certificates"] = {
                "total": len(certs),
                "expiring_soon": sum(1 for c in certs if c.get("expires_in_days", 999) < 30),
                "expired": sum(1 for c in certs if c.get("expires_in_days", 999) < 0)
            }
        except Exception as error:
            result["certificates_error"] = str(error)

        try:
            from pathlib import Path
            storage_root = Path(os.getenv("STORAGE_ROOT", "/opt/vertep/storage"))
            result["storage"] = {
                "writable": storage_root.exists() and os.access(storage_root, os.W_OK),
                "path": str(storage_root)
            }
        except Exception as error:
            result["storage_error"] = str(error)

        try:
            from core.queue import TaskQueue
            task_queue = TaskQueue()
            # Check recent task results (last 100)
            from core.state import store
            recent_jobs = list(store.jobs.values())[-50:] if store.jobs else []
            task_stats = {"completed": 0, "failed": 0, "pending": 0}
            for job in recent_jobs:
                scenes = job.scenes if hasattr(job, "scenes") else []
                for scene in scenes:
                    status = scene.status.value if scene.status else ""
                    if status == "COMPLETED":
                        task_stats["completed"] += 1
                    elif status == "FAILED":
                        task_stats["failed"] += 1
                    elif status in {"PENDING", "QUEUED", "PROCESSING"}:
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
        if "certificates_error" in result:
            full_failures.append("certificates")
        elif isinstance(result.get("certificates"), dict):
            certs = result["certificates"]
            if isinstance(certs, dict) and (certs.get("total", 0) == 0):
                full_failures.append("certificates(zero)")
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


# --- Web UI mounts (design switching: v2 default at "/", v1 classic at "/v1") ---
class SPAStaticFiles(StaticFiles):
    """StaticFiles with SPA fallback for client-side routing (Angular v2)."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and scope.get("method") in ("GET", "HEAD") \
                    and not path.startswith("api/"):
                # Let the Angular router handle client-side routes (e.g. /login, /jobs).
                return await super().get_response("index.html", scope)
            raise


@app.get("/admin", include_in_schema=False)
def admin_alias():
    # v2 is now the default at root; keep /admin as a redirect alias.
    return Response(status_code=307, headers={"Location": "/"})


# Mount v2 only when the Angular build output is present. The Docker image build
# always emits dist (web-v2 Node stage), but a fresh checkout or CI test
# collection has no compiled frontend, so importing core.app must not require it.
_V2_STATIC_DIR = "web-v2/dist/vertep-admin-v2/browser"
if not os.path.isdir(_V2_STATIC_DIR):
    _V2_STATIC_DIR = "web-v2/dist/vertep-admin-v2"
app.mount("/v1", StaticFiles(directory="web", html=True), name="web-v1")
if os.path.isdir(_V2_STATIC_DIR):
    app.mount("/", SPAStaticFiles(directory=_V2_STATIC_DIR, html=True), name="web-v2")
else:
    app.mount("/", StaticFiles(directory="web", html=True), name="web-v2")
