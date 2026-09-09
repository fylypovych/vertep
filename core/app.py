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
from .models import (JobCreate, JobUpdate, JobStatus, StageName, StageStatus,
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
                        set_integration_secret, installation)
from .telegram_store import (get_admin_chat_ids, get_allowed_chat_ids, is_allowed_chat,
                             is_admin_chat, load_telegram_settings, save_telegram_settings)
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
from .storyboard_telegram import render_storyboard, storyboard_keyboard

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
from .security import (_authenticate_user, _hash_secret, _session_token, _valid_session,
                       _valid_worker_request, _valid_worker_token, _verify_hash, _worker_tokens)


@asynccontextmanager
async def lifespan(_app):
    global telegram_polling_service
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
                                  "/api/system/update/readiness", "/api/system/recovery/normal"}
        if (request.url.path in internal_update_routes and internal_key
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
        if request.method != "GET" and role == "viewer":
            return Response("Insufficient role", 403)
        if request.method in {"PUT", "DELETE"} and request.url.path.startswith(("/api/characters", "/api/brands", "/api/workflows")) and role != "admin":
            return Response("Administrator role required", 403)
        if request.method != "GET" and request.url.path.startswith(("/api/system/update", "/api/system/roles", "/api/system/recovery")) and role != "admin":
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
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:"
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

@app.get("/setup", include_in_schema=False)
def setup_page(request: Request):
    query = f"?{request.url.query}" if request.url.query else ""
    return Response(status_code=307, headers={"Location": f"/v1/setup.html{query}"})



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
            job = store.jobs.get(task.get("job_id"))
            if job and job.status == JobStatus.ASSET_GENERATION:
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


def _handle_telegram_callback(callback: dict) -> dict:
    data = str(callback.get("data", ""))
    action, _, payload = data.partition(":")
    callback_id = str(callback.get("id", ""))
    chat_id = str(callback.get("message", {}).get("chat", {}).get("id", "unknown"))

    if action == "select_brand":
        return _handle_brand_selection(callback, chat_id, payload)
    elif action == "select_character":
        return _handle_character_selection(callback, chat_id, payload)
    elif action == "approve":
        return _handle_approve_job(callback, chat_id, payload)
    elif action == "reject":
        return _handle_reject_job(callback, chat_id, payload)
    elif action == "publish_channel":
        return _handle_publish_channel(callback, chat_id, payload)
    elif action == "publish_all":
        return _handle_publish_all(callback, chat_id, payload)
    elif action in {"sb_ok", "sb_regen", "sb_edit", "sb_reject"}:
        return _handle_storyboard_callback(callback, chat_id, action, payload)

    job_id = payload
    job = store.jobs.get(job_id)
    if job and action == "cancel":
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
    _telegram_pending_character.pop(chat_id, None)
    TelegramAdapter().answer_callback(callback_id, f"Персонаж {character_id} обрано. Створюю завдання…")
    try:
        load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), character_id)
    except Exception as error:
        TelegramAdapter().answer_callback(callback_id, f"Помилка: невідомий персонаж {character_id}")
        raise HTTPException(400, f"Unknown character: {character_id}") from error
    job = _create_job_from_telegram(pending, brand_id, character_id)
    _queue_storyboard(job, chat_id)
    TelegramAdapter().send_message(chat_id, f"JOB {job.job_id}\nSTATUS: {job.status.value}\nГенерую розкадровку…")
    TelegramAdapter().answer_callback(callback_id, f"Job {job.job_id} створено. Генерую розкадровку.")
    return job.model_dump(mode="json")


def _queue_storyboard(job, chat_id: str, revision: str | None = None) -> None:
    service = StoryboardService(store)
    service.store.update(job, JobStatus.STORYBOARD_QUEUED, "STORYBOARD QUEUED")
    executor.submit(_generate_storyboard_and_notify, job.job_id, chat_id, revision)


def _generate_storyboard_and_notify(job_id: str, chat_id: str, revision: str | None = None) -> None:
    try:
        storyboard = StoryboardService(store).generate(job_id, revision)
        chunks = render_storyboard(store.jobs[job_id], storyboard)
        for index, chunk in enumerate(chunks):
            markup = storyboard_keyboard(job_id, storyboard.version) if index == len(chunks) - 1 else None
            TelegramAdapter().send_message(chat_id, chunk, markup)
    except Exception as error:
        logger.error("Storyboard generation failed: %s", error, extra={"job_id": job_id})
        try:
            TelegramAdapter().send_message(chat_id, f"❌ Не вдалося створити розкадровку {job_id}: {error}")
        except Exception:
            pass


def _regenerate_storyboard_and_notify(job_id: str, version: int, chat_id: str,
                                      revision: str | None = None) -> None:
    try:
        storyboard = StoryboardService(store).regenerate(
            job_id, version, f"telegram:{chat_id}", revision
        )
        # regenerate() is synchronous when the service has no executor.
        active = StoryboardService(store).get(job_id, storyboard.active_storyboard_version)
        chunks = render_storyboard(storyboard, active)
        for index, chunk in enumerate(chunks):
            markup = storyboard_keyboard(job_id, active.version) if index == len(chunks) - 1 else None
            TelegramAdapter().send_message(chat_id, chunk, markup)
    except Exception as error:
        logger.error("Storyboard regeneration failed: %s", error, extra={"job_id": job_id})
        try:
            TelegramAdapter().send_message(chat_id, f"❌ Не вдалося оновити розкадровку {job_id}: {error}")
        except Exception:
            pass


def _handle_storyboard_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    callback_id = str(callback.get("id", ""))
    job_id, separator, raw_version = payload.partition(":")
    if not separator or not raw_version.isdigit():
        return TelegramAdapter().answer_callback(callback_id, "Некоректна версія розкадровки")
    version = int(raw_version)
    service = StoryboardService(store)
    try:
        if action == "sb_ok":
            job = service.approve(job_id, version, f"telegram:{chat_id}")
            executor.submit(_prepare_and_dispatch, job)
            text = f"✅ Розкадровку {version} схвалено. Pipeline продовжено."
        elif action == "sb_reject":
            service.reject(job_id, version, f"telegram:{chat_id}")
            text = f"❌ Розкадровку {version} відхилено."
        elif action == "sb_edit":
            job = store.jobs.get(job_id)
            if not job or job.active_storyboard_version != version:
                raise StoryboardConflict("Версія розкадровки вже неактуальна")
            job.storyboard_revision_chat_id = chat_id
            job.storyboard_revision_version = version
            store.event(job, f"STORYBOARD {version} AWAITS REVISION TEXT")
            text = "Надішліть одним повідомленням, що потрібно змінити."
        else:
            executor.submit(_regenerate_storyboard_and_notify, job_id, version, chat_id)
            text = "🔄 Генерую нову версію розкадровки."
        return TelegramAdapter().answer_callback(callback_id, text)
    except KeyError:
        return TelegramAdapter().answer_callback(callback_id, "Job або розкадровку не знайдено")
    except StoryboardConflict as error:
        return TelegramAdapter().answer_callback(callback_id, str(error))


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
    publisher = providers.publisher()
    if not publisher.configured(channel.channel_type):
        return {"status": "NOT_CONFIGURED", "error": f"{channel.channel_type} не налаштовано"}
    try:
        result = publisher.publish(channel.channel_type, job.output_path or "", {"job_id": job.job_id, "topic": job.topic, "target": channel.target})
        job.publication_results[channel.channel_id] = result
        if result.get("status") == "PUBLISHED":
            if channel.channel_id not in job.published_to:
                job.published_to.append(channel.channel_id)
        store.update(job, job.status, f"PUBLISHED to {channel.channel_type}:{channel.target}")
        return result
    except Exception as error:
        return {"status": "FAILED", "error": str(error)}


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
    public_url = body.public_url or os.getenv("PUBLIC_URL", "")
    webhook_secret = body.webhook_secret or os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    allowed_chat_ids = body.allowed_chat_ids
    admin_chat_ids = body.admin_chat_ids
    if webhook_secret:
        os.environ["TELEGRAM_WEBHOOK_SECRET"] = webhook_secret
    save_telegram_settings(allowed_chat_ids=allowed_chat_ids, admin_chat_ids=admin_chat_ids)
    if not public_url:
        return {"status": "saved", "message": "Settings saved; webhook not configured (PUBLIC_URL is not set)"}
    try:
        adapter = TelegramAdapter()
        return adapter.set_webhook(public_url, webhook_secret)
    except (RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(502, str(error)) from error


def _start_telegram_polling() -> None:
    global telegram_polling_service
    token = os.getenv("TELEGRAM_BOT_TOKEN") or _integration_secret("telegram_bot_token") or ""
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
        raise HTTPException(503, f"{base_environment} is not configured")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.request(method, f"{base}{path}", json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as error:
        raise HTTPException(502, error.response.text[:500]) from error
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(503, f"Internal service is unavailable: {error}") from error


@app.get("/api/system/backups")
async def system_backups():
    return await _internal_api("GET", "BACKUP_URL", "/snapshots")


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
