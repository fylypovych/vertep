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
                     RollingUpdateRequest, CoordinatorUpdateRequest, utc_now, Channel, ChannelCreate, ChannelUpdate, CHANNEL_TYPES)
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
from .operations import (create_operation, get_operation, list_operations,
                         get_or_create_operation,
                         begin_operation, advance_operation, complete_operation, fail_operation,
                         is_operation_in_progress, audit_entry, OperationStatus,
                         link_operation_request, add_pending_notification,
                         pending_notifications, acknowledge_notification,
                         set_operation_fields)
from .telegram_callbacks import (DurableCallbackSet, claim_callback,
                                 complete_callback, link_operation,
                                 set_callback_target)
from .update_manager import (mark_request_failed, pending_request_ids,
                             request_update, update_status)
from .pipeline import regenerate_script
from .node_registry import (create_node_csr, create_registration_token, enroll_node, node_roles,
                            registered_nodes, renew_node, revoke_node, verify_node_certificate,
                            verify_node_token, write_node_crl)
from .version import application_version
from .runtime_identity import CORE_RUNTIME_INSTANCE_ID
from .deployment_plan import create_plan
from .telegram.handlers import _handle_system_callback as _telegram_handle_system_callback, _handle_telegram_callback as _telegram_handle_telegram_callback, _handle_telegram_message as _telegram_handle_telegram_message, _start_telegram_polling as _telegram_start_telegram_polling, _restart_telegram_polling as _telegram_restart_telegram_polling, _stop_telegram_polling as _telegram_stop_telegram_polling, _process_telegram_update as _telegram_process_telegram_update, _handle_brand_selection as _telegram_handle_brand_selection, _send_character_selection as _telegram_send_character_selection, _sanitize_creation_error as _telegram_sanitize_creation_error, _handle_character_selection as _telegram_handle_character_selection, _queue_storyboard as _telegram_queue_storyboard, _generate_storyboard_and_notify as _telegram_generate_storyboard_and_notify, _regenerate_script_and_notify as _telegram_regenerate_script_and_notify, _regenerate_storyboard_and_notify as _telegram_regenerate_storyboard_and_notify, _handle_storyboard_callback as _telegram_handle_storyboard_callback, _handle_script_callback as _telegram_handle_script_callback, _handle_video_callback as _telegram_handle_video_callback, _finalize_video_regenerate as _telegram_finalize_video_regenerate, _notify_video_regeneration as _telegram_notify_video_regeneration, _collect_reassembly_inputs as _telegram_collect_reassembly_inputs, _handle_approve_job as _telegram_handle_approve_job, _handle_reject_job as _telegram_handle_reject_job, _handle_publish_channel as _telegram_handle_publish_channel, _handle_publish_all as _telegram_handle_publish_all, _publish_to_channel as _telegram_publish_to_channel, _send_approval_request as _telegram_send_approval_request, _save_telegram_attachments as _telegram_save_telegram_attachments, _create_job_from_telegram as _telegram_create_job_from_telegram


# Backward-compatible aliases for direct calls from tests and other modules.
def _handle_telegram_callback(callback: dict) -> dict:
    return _telegram_handle_telegram_callback(callback)


def _handle_telegram_message(chat_id: str, source_id: str, text: str, message: dict) -> dict:
    return _telegram_handle_telegram_message(chat_id, source_id, text, message)


def _handle_character_selection(callback: dict, chat_id: str, character_id: str) -> dict:
    return _telegram_handle_character_selection(callback, chat_id, character_id)


def _handle_script_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    return _telegram_handle_script_callback(callback, chat_id, action, payload)


def _handle_video_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    return _telegram_handle_video_callback(callback, chat_id, action, payload)


def _handle_approve_job(callback: dict, chat_id: str, job_id: str) -> dict:
    return _telegram_handle_approve_job(callback, chat_id, job_id)


def _handle_reject_job(callback: dict, chat_id: str, job_id: str) -> dict:
    return _telegram_handle_reject_job(callback, chat_id, job_id)


def _handle_publish_channel(callback: dict, chat_id: str, payload: str) -> dict:
    return _telegram_handle_publish_channel(callback, chat_id, payload)


def _handle_publish_all(callback: dict, chat_id: str, job_id: str) -> dict:
    return _telegram_handle_publish_all(callback, chat_id, job_id)


def _publish_to_channel(job, channel) -> dict:
    return _telegram_publish_to_channel(job, channel)


def _send_approval_request(job) -> None:
    return _telegram_send_approval_request(job)


def _save_telegram_attachments(job, message: dict, root: Path) -> None:
    return _telegram_save_telegram_attachments(job, message, root)


def _create_job_from_telegram(pending: dict, brand_id: str, character_id: str | None = None):
    return _telegram_create_job_from_telegram(pending, brand_id, character_id)

# Wall-clock moment this CORE process started.  Operations created before it
# belong to an earlier process and may be stale leftovers (Issue #174.3).
_PROCESS_STARTED_AT = datetime.now(timezone.utc)

# Delegation shim: system_restart moved to core.api.system (Issue #110 Phase C).
# The CI test caller references core.app.system_restart; keep the symbol alive
# by delegating to the canonical implementation.
async def system_restart(payload: dict | None = None):
    from .api.system import system_restart as _system_restart
    return await _system_restart(payload)
from .rolling_update import (cancel_rollout, is_core_node, promote_rollout, reconcile_rollout,
                             rollout_status, rollback_ready_nodes, start_rollout,
                             update_core_coordinator)
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
from .api.system import router as system_router
from .api.backups import router as backups_router
from .api.operations import router as operations_router
from .api.certificates import router as certificates_router
from .security import (_authenticate_user, _hash_secret, _session_token, _valid_session,
                       _valid_worker_request, _valid_worker_token, _worker_tokens,
                       account_password_matches, account_profile, password_policy,
                       password_policy_violations, profile_field_violations, save_account_profile,
                       session_response, set_account_password)


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
                _wr.root = Path(env_wf)
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
    # Issue #78: a provider backend chosen in the Web UI must survive restarts.
    try:
        from .provider_switch import apply_provider_overrides, reset_provider_registry
        if apply_provider_overrides():
            reset_provider_registry()
    except Exception as override_error:  # noqa: BLE001 - never block startup
        try:
            from .state import logger as _ov_log
            _ov_log.warning("Provider override apply failed: %s", override_error)
        except Exception:
            pass
    for recovered_job in list(store.jobs.values()):
        if (recovered_job.status in {JobStatus.NEW, JobStatus.WAITING_FOR_SYSTEM}
                and dispatch_allowed() and _job_is_due(recovered_job)):
            if recovered_job.status == JobStatus.WAITING_FOR_SYSTEM:
                store.update(recovered_job, JobStatus.NEW, "SYSTEM RETURNED TO NORMAL")
            executor.submit(_prepare_and_dispatch, recovered_job)
    watchdog_task = asyncio.create_task(_watchdog())
    # Issue #174.3: an update operation whose host request was lost must reach
    # a terminal state after a restart, otherwise every retry is refused.
    try:
        recovered = _recover_stale_update_operations()
        if recovered:
            logger.warning("Recovered %d stale update operation(s) after restart", len(recovered))
    except Exception as recover_error:  # noqa: BLE001 - never block startup
        logger.warning("Update operation recovery failed: %s", recover_error)
    # Issue #174.4: a restart operation whose effect survived the process
    # restart must be finished and reported to the operator.
    try:
        finished = _reconcile_restart_operations()
        if finished:
            logger.warning("Finished %d restart operation(s) after restart", len(finished))
    except Exception as reconcile_error:  # noqa: BLE001 - never block startup
        logger.warning("Restart operation reconciliation failed: %s", reconcile_error)
    # Issue #78: model pulls live on background threads/worker commands and do
    # not survive a CORE restart — fail the orphaned ones so a retry is possible.
    try:
        from .pull_executor import reconcile_model_pull_operations
        recovered = reconcile_model_pull_operations()
        if recovered:
            logger.warning("Reconciled %d orphaned model operation(s) after restart", recovered)
    except Exception as reconcile_error:  # noqa: BLE001 - never block startup
        logger.warning("Model operation reconcile failed: %s", reconcile_error)
    # The polling service delivers the durable notifications that the
    # reconciliation above has queued, so it starts after the recovery.
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
_telegram_system_callbacks = DurableCallbackSet()
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
            elif path.startswith("/api/system/backups/") and "/restore" in path and request.method == "POST":
                operation = "restore"
            elif path.startswith("/api/system/backups") and request.method == "POST":
                operation = "backup"
            elif path.startswith("/api/system/models") and request.method in {"POST", "DELETE"}:
                operation = "model_pull"
            elif path.startswith("/api/models/text") and request.method in {"POST", "DELETE"}:
                operation = "model_pull"
            elif path.startswith("/api/characters"):
                operation = "resource_management"
            elif path.startswith("/api/brands"):
                operation = "resource_management"
            elif path.startswith("/api/workflows"):
                operation = "resource_management"
            elif path.startswith("/api/channels"):
                operation = "resource_management"
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
        # The Fleet self-calls (Telegram restart/system handlers) read the node
        # listing and the node detail with the internal key; without that the
        # readiness poll could never observe the acknowledged restart.
        internal_node_read = (request.method == "GET"
                              and (request.url.path == "/api/nodes"
                                   or request.url.path.startswith("/api/nodes/")))
        if ((request.url.path in internal_update_routes or internal_actions
                or internal_node_read)
                and internal_key
                and secrets.compare_digest(request.headers.get("x-vertep-internal-key", ""), internal_key)):
            response = await call_next(request)
            return self._secure(response)
        public = ("/api/health", "/api/telegram/webhook", "/api/nodes/register")
        setup_routes = ("/api/setup",)
        if request.url.path.startswith(public) or request.url.path.startswith(setup_routes) or request.url.path == "/api/nodes/register":
            response = await call_next(request)
            return self._secure(response)
        demo_mode = os.getenv("DEMO_MODE", "true").lower() == "true"
        demo_paths = {"/api/health", "/api/setup", "/api/setup/health"}
        if (
            demo_mode
            and not os.getenv("ADMIN_PASSWORD", "").strip()
            and not os.getenv("CONFIG_ROOT", "").strip()
            and request.url.path in demo_paths
        ):
            response = await call_next(request)
            return self._secure(response)
        if not configured_user() and not password and not os.getenv("USERS_JSON", "").strip(" {}"):
            # An installation that has not completed setup still answers the
            # session probe: the login page needs ``authenticated: false`` with a
            # 200, never a 401 that would trigger a native browser dialog (§75 S1).
            if request.method == "GET" and request.url.path == "/api/session":
                response = await call_next(request)
                return self._secure(response)
            return self._secure(Response("Authentication required", 401))
        if request.method in {"GET", "HEAD"} and not request.url.path.startswith("/api/"):
            # Issue #75 S1: the Web UI is a static bundle. An unauthenticated
            # visitor must be able to load /login, otherwise the guard redirect
            # has nothing to render. No Vertep data is served outside /api,
            # which stays behind the authentication checks below.
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
            # The Angular login form submits credentials explicitly. A Basic
            # challenge on its session probe opens a second, native browser login.
            return self._secure(Response("Authentication required", 401))
        actor, role = session_identity or (user, basic_role)
        if request.method != "GET" and role == "viewer" and not request.url.path.startswith("/api/session"):
            return Response("Insufficient role", 403)
        if request.method in {"PUT", "DELETE"} and request.url.path.startswith(("/api/characters", "/api/brands", "/api/workflows")) and role != "admin":
            return Response("Administrator role required", 403)
        if request.method != "GET" and request.url.path.startswith(("/api/system/update", "/api/system/roles", "/api/system/recovery", "/api/system/restart", "/api/system/test", "/api/system/models")) and role != "admin":
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
app.include_router(system_router)
app.include_router(backups_router)
app.include_router(operations_router)
app.include_router(certificates_router)

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
    # Issue #197/#163.3: the lease that expired belonged to a specific Worker.
    # The task payload does not always carry ``node_name`` (the claim route
    # keeps it on the Worker record), so resolve the owning node from the
    # record that still holds this task.  Release its ownership record so the
    # node is not left BUSY on a phantom task while the Job re-queues for
    # another node; otherwise a stale heartbeat/listing keeps reporting a
    # worker bound to a task that is no longer the Job's active storyboard
    # task.
    stale_node = task.get("node_name")
    if stale_node is None:
        stale_node = next(
            (name for name, w in store.workers.items()
             if w.get("current_task") == task.get("task_id")), None)
    stale_worker = store.workers.get(stale_node) if stale_node else None
    if stale_worker:
        stale_worker["current_job"] = None
        stale_worker["current_task"] = None
        stale_worker["last_seen"] = utc_now()
        store.save_worker(stale_worker)
    # Record the lost lease in the repository so the audit trail shows the
    # task was released, not silently dropped.
    store.repository.record_task({"task_id": task["task_id"], "task": "storyboard",
                                            "job_id": job.job_id}, "RELEASED", stale_node,
                                         error="storyboard task lease expired")
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
        # Issue #197/#163.1: the budget and the revision belong to the version
        # that was active when the lease expired, so a lease-loss retry keeps
        # burning the same budget and reuses the pinned revision instead of a
        # stale ``revision_request`` from a superseded version.
        from .storyboard import build_storyboard_prompt
        from .configuration import load_character, read_json
        import os as _os
        from pathlib import Path as _Path
        character = load_character(_Path(_os.getenv("CHARACTERS_ROOT", "characters")), job.character_id).model_dump()
        brand = read_json(_Path(_os.getenv("BRANDS_ROOT", "brands")) / job.brand_id / "brand.json")
        active_revision = getattr(job, "storyboard_active_revision", None)
        if active_revision is None and job.storyboards:
            last = job.storyboards[-1]
            if getattr(last, "revision_request", None) is not None:
                active_revision = last.revision_request
            elif getattr(job, "storyboard_active_revision", None) is not None:
                active_revision = getattr(job, "storyboard_active_revision")
            # Issue #197/#163.1: never substitute a stale revision_request from a
            # superseded storyboard when none is pinned for the active revision;
            # keep None so the prompt uses the character/job defaults.
        else:
            if active_revision is None and getattr(job, "storyboard_active_revision", None) is not None:
                active_revision = getattr(job, "storyboard_active_revision")
        if active_revision is None and getattr(job, "storyboards", None):
            # Avoid pulling from an arbitrary previous revision; only use an
            # explicitly pinned active_revision.
            pass
        from .storyboard import build_storyboard_prompt
        from .configuration import load_character, read_json
        import os as _os
        from pathlib import Path as _Path
        character = load_character(_Path(_os.getenv("CHARACTERS_ROOT", "characters")), job.character_id).model_dump()
        brand = read_json(_Path(_os.getenv("BRANDS_ROOT", "brands")) / job.brand_id / "brand.json")
        prompt = build_storyboard_prompt(job, character, brand, active_revision)
        version = (job.storyboards[-1].version + 1 if job.storyboards else 1)
        image_version = job.storyboards[-1].image_version if job.storyboards else 1
        new_task = task_queue.enqueue({
            "job_id": job.job_id, "task": "storyboard", "priority": job.priority,
            "topic": job.topic, "prompt": prompt, "storyboard_version": version,
            "image_version": image_version,
            "revision": active_revision,
            "timeout": int(_os.getenv("OLLAMA_STORYBOARD_TIMEOUT", "300")),
        })

        job.storyboard_task_id = new_task["task_id"]
        job.active_task_id = new_task["task_id"]
        job.storyboard_attempt_version = version
        job.storyboard_active_revision = active_revision
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







def _json_error(detail: str, status: int, **extra) -> Response:
    from core.logging_config import secret_redact
    body = {"detail": secret_redact(detail), **extra}
    return Response(content=json.dumps(body, ensure_ascii=False), status_code=status,
                    media_type="application/json")




@app.delete("/api/session")
def logout(response: Response):
    response.delete_cookie("vertep_session")
    response.delete_cookie("vertep_csrf")
    return {"authenticated": False}

@app.get("/api/session")
def session_info(request: Request):
    # Issue #75 S1: the canonical identity is decided once, on the server.
    return session_response(_valid_session(request.cookies.get("vertep_session", "")))


def _session_identity(request: Request) -> Response | tuple[str, str]:
    """Return the validated identity or a ready-to-return 401 response."""
    identity = _valid_session(request.cookies.get("vertep_session", ""))
    profile = account_profile(*identity) if identity else None
    return profile or _json_error("Unauthorized", 401)


@app.put("/api/session/profile")
def update_profile(request: Request, payload: dict):
    """Issue #75 S3: self-service name/email fields for the own account."""
    identity = _session_identity(request)
    if isinstance(identity, Response):
        return identity
    updates = {field: payload.get(field) for field in ("display_name", "email")
               if field in payload}
    if not updates:
        return _json_error("Немає полів для оновлення", 400)
    violations = profile_field_violations(updates)
    if violations:
        return _json_error("Недійсні поля профілю", 422, fields=violations)
    if not save_account_profile(identity["user"], updates):
        return _json_error("User not found", 404)
    return account_profile(identity["user"], identity["role"])


@app.put("/api/session/password")
def change_password(request: Request, payload: dict):
    identity = _session_identity(request)
    if isinstance(identity, Response):
        return identity
    user = identity["user"]
    old_password = payload.get("old_password", "")
    new_password = payload.get("new_password", "")
    if not old_password or not new_password:
        return _json_error("Old and new passwords required", 400)
    violations = password_policy_violations(new_password, old_password)
    if violations:
        return _json_error(violations[0], 400)
    if not account_password_matches(user, old_password):
        # Issue #75 S3: a wrong current password stays a recoverable error —
        # no lockout, no partial write, the current session keeps working.
        return _json_error("Current password is incorrect", 400)
    if not set_account_password(user, new_password):
        return _json_error("User not found", 404)
    return {"ok": True, "message": "Password changed successfully",
            "password_policy": password_policy()}

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
    return _telegram_handle_telegram_message(chat_id, source_id, text, message)


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
    # Redact secrets from error before formatting
    error = _redact_secrets(error) if error else None
    lines = [
        f"Операція {op_type} (#{op_id})",
        f"Статус: {status} · прогрес {progress}% · фаза: {phase}",
    ]
    if error:
        lines.append(f"Помилка: {error}")
    return "\n".join(lines)


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
                return {"_error": f"HTTP {response.status_code}"}
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
    _reconcile_update_operations()
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


def _host_status_is_stale(status: dict) -> bool:
    """True when the durable update status stopped progressing.

    The host agent refreshes ``status.json`` while it works on a request.  A
    status that stayed in ``PENDING``/``RUNNING`` longer than the configured
    window belongs to a host agent that died mid-operation (Issue #174.3).
    """
    try:
        window = int(os.getenv("UPDATE_OPERATION_STALE_SECONDS", "3600"))
    except ValueError:
        window = 3600
    if window <= 0:
        return False
    updated_at = str(status.get("updated_at") or "")
    if not updated_at:
        return False
    try:
        moment = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds() > window


def _operation_started_before_process(operation: dict) -> bool:
    """True when the operation was created by an earlier CORE process."""
    created_at = str(operation.get("created_at") or "")
    if not created_at:
        return False
    try:
        moment = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment < _PROCESS_STARTED_AT


def _recover_stale_update_operations() -> list[str]:
    """Fail update operations whose host request was lost (Issue #174.3).

    A CORE process that dies while the host agent works on a request leaves
    the operation in ``RUNNING`` forever and the host status in
    ``PENDING``/``RUNNING``.  Every later confirmation is then refused as
    "already in progress", so the failure path has to become terminal and a
    retry has to stay possible.
    """
    try:
        status = update_status() or {}
    except Exception:
        return []
    state = str(status.get("state", "")).upper()
    host_request = status.get("request_id")
    queued = pending_request_ids()
    host_stale = _host_status_is_stale(status)
    recovered: list[str] = []
    for operation in list_operations(200):
        if operation.get("type") != "update":
            continue
        if operation.get("status") not in {OperationStatus.QUEUED.value,
                                           OperationStatus.RUNNING.value}:
            continue
        external = operation.get("external_request_id")
        if external:
            if external in queued:
                continue
            if external == host_request and state in {"PENDING", "RUNNING"}:
                if not host_stale:
                    continue
                mark_request_failed(external, "Update host agent stopped responding")
        elif not _operation_started_before_process(operation):
            continue
        fail_operation(
            operation["operation_id"],
            "Stale update operation recovered: the host request is no longer active",
        )
        recovered.append(operation["operation_id"])
    return recovered


def _record_operation_field(operation_id: str, **fields) -> None:
    """Persist correlation fields of an operation without breaking the flow.

    The fields tie the Telegram operation to the durable effect identity
    (``restart_operation_id``, ``previous_runtime_instance_id``).  A missing
    operation record must not abort the requested operation itself.
    """
    try:
        set_operation_fields(operation_id, **fields)
    except KeyError:
        logger.warning("Operation %s is not persisted; correlation fields were dropped",
                       str(operation_id)[:8])


def _queue_operation_notification(operation: dict, marker: str, message: str) -> None:
    """Queue a durable Telegram notification for a finished operation.

    The inline callback answer is lost when the CORE process dies between the
    effect and the answer.  The durable queue keeps the final result, and the
    delivery happens after the restart (Issue #174.4).
    """
    requested_by = str(operation.get("requested_by") or "")
    if not requested_by.startswith("telegram:"):
        return
    chat_id = requested_by.split(":", 1)[1]
    add_pending_notification(chat_id, f"{marker} {message}")


def _reconcile_restart_operations() -> list[str]:
    """Finish restart operations whose effect survived a CORE process restart.

    A restart effect is durable: the node records the acknowledgement of its
    new runtime instance and the CORE records its own instance id.  After a
    process restart the operation has to reach a terminal state instead of
    blocking every later restart request (Issue #174.4).
    """
    finished: list[str] = []
    for operation in list_operations(200):
        if operation.get("type") != "restart":
            continue
        if operation.get("status") not in {OperationStatus.QUEUED.value,
                                           OperationStatus.RUNNING.value}:
            continue
        target = str(operation.get("target") or "")
        if target == "core":
            previous = str(operation.get("previous_runtime_instance_id") or "")
            if not previous or previous == CORE_RUNTIME_INSTANCE_ID:
                continue
            complete_operation(operation["operation_id"],
                               {"status": "healthy", "target": "core",
                                "runtime_instance_id": CORE_RUNTIME_INSTANCE_ID,
                                "message": "CORE перезапущено"})
            _queue_operation_notification(
                operation, "✅",
                f"Перезапуск CORE #{operation['operation_id'][:8]} завершено.")
            finished.append(operation["operation_id"])
            continue
        worker = store.workers.get(target) or {}
        acknowledgement = worker.get("restart_ack") or {}
        if (worker.get("status") == "READY"
                and acknowledgement.get("operation_id")
                and acknowledgement.get("operation_id") == operation.get("restart_operation_id")):
            complete_operation(operation["operation_id"],
                               {"status": "ready", "node_id": target,
                                "restart_operation_id": acknowledgement.get("operation_id"),
                                "runtime_instance_id": acknowledgement.get("runtime_instance_id"),
                                "message": f"Worker {target} перезапущено успішно"})
            _queue_operation_notification(
                operation, "✅",
                f"Вузол {target} перезапущено і READY. ID: {operation['operation_id'][:8]}")
            finished.append(operation["operation_id"])
    return finished


def _release_notes_for(version: str) -> list[str]:
    """Release notes of a target version for the update confirmation.

    The notes come from the release artefacts that ship with the source tree
    (``releases/<version>.md`` or the matching ``CHANGELOG.md`` block).  A
    deployment without them confirms the version transition only.
    """
    if not version:
        return []
    root = Path(__file__).resolve().parents[1]
    text = ""
    try:
        text = (root / "releases" / f"{version}.md").read_text(encoding="utf-8")
    except OSError:
        text = ""
    if not text:
        try:
            changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        except OSError:
            return []
        pattern = re.compile(
            rf"(?ms)^## ПРАВИЛЬНА НАЗВА: {re.escape(version)}\s*\n(.*?)(?=^## |\Z)")
        match = pattern.search(changelog)
        text = match.group(1) if match else ""
    return [line.strip() for line in text.splitlines() if line.strip().startswith("-")][:10]


def _update_target_version() -> str | None:
    """Version the update operation would move the system to."""
    try:
        status = update_status() or {}
    except Exception:
        return None
    return status.get("available_version") or None


def _reconcile_update_operations() -> None:
    """Correlate durable update-agent results with Telegram operations."""
    try:
        status = update_status() or {}
    except Exception:
        return
    request_id = status.get("request_id")
    state = str(status.get("state", "")).upper()
    if not request_id:
        return
    for operation in list_operations(200):
        if (operation.get("type") != "update"
                or operation.get("external_request_id") != request_id
                or operation.get("status") not in {"QUEUED", "RUNNING"}):
            continue
        # Phase trail: the host agent reports its phase in ``status.json``.
        # Mirroring it into the durable operation keeps the Telegram status
        # output truthful while the update is still running (Issue #174.3).
        phase = str(status.get("phase") or "").strip()
        if phase and phase != operation.get("current_phase"):
            try:
                progress = int(status.get("progress") or 0)
            except (TypeError, ValueError):
                progress = 0
            advance_operation(operation["operation_id"], phase, progress,
                              status.get("message"))
        if state not in {"SUCCEEDED", "FAILED", "ROLLED_BACK"}:
            continue
        if state == "SUCCEEDED":
            complete_operation(operation["operation_id"], status)
            marker = "✅"
        else:
            fail_operation(operation["operation_id"], status.get("message") or state)
            marker = "❌"
        requested_by = str(operation.get("requested_by") or "")
        if requested_by.startswith("telegram:"):
            chat_id = requested_by.split(":", 1)[1]
            version = status.get("current_version") or status.get("available_version")
            suffix = f" ({version})" if version else ""
            add_pending_notification(
                chat_id,
                f"{marker} Операція оновлення #{operation['operation_id'][:8]}{suffix} завершена: "
                f"{status.get('message') or state}",
            )


def _deliver_pending_telegram_notifications() -> None:
    """Retry final operation delivery after CORE/process restarts."""
    adapter = TelegramAdapter()
    for notification in pending_notifications():
        try:
            result = adapter.send_message(notification["chat_id"], notification["message"])
        except Exception as error:
            logger.warning("Telegram operation notification delivery failed: %s", error)
            continue
        if isinstance(result, dict) and result.get("status") == "STUB":
            # Nothing was delivered (the bot token is not configured), so the
            # notification has to stay queued for the next attempt
            # (Issue #174.2).
            logger.warning("Telegram operation notification was not delivered: %s",
                           result.get("reason"))
            continue
        acknowledge_notification(notification["notification_id"])


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
                return {"_error": f"HTTP {response.status_code}"}
            return response.json()
    except (httpx.HTTPError, ValueError):
        return None


def _handle_system_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_handle_system_callback(callback, chat_id, action, payload)


_ACTION_TO_OP_TYPE = {
    "sys_update": "update", "sys_update_check": "update", "sys_update_confirm": "update",
    "sys_restart": "restart", "sys_restart_core": "restart", "sys_restart_worker": "restart",
    "sys_backup": "backup", "sys_backup_confirm": "backup",
    "sys_restore": "restore", "sys_restore_confirm": "restore", "sys_restore_execute": "restore",
    "sys_test": "test", "sys_test_quick": "test", "sys_test_full": "test", "sys_test_node": "test",
}


def _link_callback_to_operation(callback_id: str, action: str, payload: str,
                                chat_id: str) -> None:
    """Link a CLAIMED callback to its terminal operation for crash-replay checking.

    After the side effect runs, its operation may have reached COMPLETED/FAILED.
    Linking the callback record to that operation lets a later replay detect the
    terminal state and skip re-execution (effect→crash→persist gap, Issue #174).
    """
    op_type = _ACTION_TO_OP_TYPE.get(action)
    if not op_type:
        return
    try:
        operations = list_operations(200)
    except Exception:
        return
    target = payload or None
    requested_by = f"telegram:{chat_id}"
    terminal = {"COMPLETED", "FAILED", "CANCELLED"}
    best = None
    for op in operations:
        if (op.get("type") == op_type
                and op.get("target") == target
                and op.get("requested_by") == requested_by
                and op.get("status") in terminal):
            finished = op.get("finished_at") or ""
            if best is None or finished > best.get("finished_at", ""):
                best = op
    if best and best.get("operation_id"):
        link_operation(callback_id, best["operation_id"])


def _handle_telegram_callback(callback: dict) -> dict:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_handle_telegram_callback(callback)


def _handle_brand_selection(callback: dict, chat_id: str, brand_id: str) -> dict:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_handle_brand_selection(callback, chat_id, brand_id)


def _send_character_selection(chat_id: str, brand_id: str) -> dict:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_send_character_selection(chat_id, brand_id)


# Absolute filesystem paths (POSIX ``/x/...`` or Windows ``C:\...``) that may
# appear inside error text; URLs (``scheme://host``) are intentionally kept.
_ABS_PATH_RE = re.compile(r"(?<![:/])(?<!\w)(?:[A-Za-z]:[\\/]|/)[^\s'\";)]*")


def _sanitize_creation_error(error: Exception) -> str:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_sanitize_creation_error(error)


def _handle_character_selection(callback: dict, chat_id: str, character_id: str) -> dict:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_handle_character_selection(callback, chat_id, character_id)


def _queue_storyboard(job, chat_id: str, revision: str | None = None) -> None:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_queue_storyboard(job, chat_id, revision)


def _generate_storyboard_and_notify(job_id: str, chat_id: str, revision: str | None = None) -> None:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_generate_storyboard_and_notify(job_id, chat_id, revision)


def _regenerate_script_and_notify(job_id: str, chat_id: str, revision: str | None = None) -> None:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_regenerate_script_and_notify(job_id, chat_id, revision)


def _regenerate_storyboard_and_notify(job_id: str, version: int, chat_id: str,
                                      revision: str | None = None) -> None:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_regenerate_storyboard_and_notify(job_id, version, chat_id, revision)


def _handle_storyboard_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_handle_storyboard_callback(callback, chat_id, action, payload)


def _handle_script_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_handle_script_callback(callback, chat_id, action, payload)


def _handle_video_callback(callback: dict, chat_id: str, action: str, payload: str) -> dict:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_handle_video_callback(callback, chat_id, action, payload)


def _finalize_video_regenerate(job) -> None:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_finalize_video_regenerate(job)


def _notify_video_regeneration(job, prefix: str) -> None:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_notify_video_regeneration(job, prefix)


def _collect_reassembly_inputs(job) -> list:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_collect_reassembly_inputs(job)


def _handle_approve_job(callback: dict, chat_id: str, job_id: str) -> dict:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_handle_approve_job(callback, chat_id, job_id)


def _handle_reject_job(callback: dict, chat_id: str, job_id: str) -> dict:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_handle_reject_job(callback, chat_id, job_id)


def _handle_publish_channel(callback: dict, chat_id: str, payload: str) -> dict:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_handle_publish_channel(callback, chat_id, payload)


def _handle_publish_all(callback: dict, chat_id: str, job_id: str) -> dict:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_handle_publish_all(callback, chat_id, job_id)


def _publish_to_channel(job, channel) -> dict:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_publish_to_channel(job, channel)


def _send_approval_request(job) -> None:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_send_approval_request(job)


def _save_telegram_attachments(job, message: dict, root: Path) -> None:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_save_telegram_attachments(job, message, root)


def _create_job_from_telegram(pending: dict, brand_id: str, character_id: str | None = None):
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_create_job_from_telegram(pending, brand_id, character_id)


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
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_start_telegram_polling()


def _restart_telegram_polling() -> None:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_restart_telegram_polling()


from . import telegram_pump as _telegram_pump
_telegram_pump.register_restart(_restart_telegram_polling)
_telegram_pump.register_service(lambda: telegram_polling_service)


def _stop_telegram_polling() -> None:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_stop_telegram_polling()


def _process_telegram_update(update: dict) -> None:
    """Delegates to core.telegram.handlers (Issue #345 extraction)."""
    return _telegram_process_telegram_update(update)


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


# Settings, integrations and logo routes are defined in core/api/settings.py


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
