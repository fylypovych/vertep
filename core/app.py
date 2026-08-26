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
from functools import wraps
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from .models import (JobCreate, JobUpdate, JobStatus, StageName, StageStatus,
                     WorkerHeartbeat, TaskClaim, TaskRenew, TaskResult, worker_transition_allowed,
                     WorkerLogBatch, NodeAction, IntegrationSecretUpdate,
                     RollingUpdateRequest, utc_now)
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
from .system_state import dispatch_allowed, get_system_state, jobs_may_be_created
from .first_run import (complete_setup, configured_user, is_configured, session_secret,
                        setup_status, config_root, integration_secret_status,
                        set_integration_secret)
from .node_registry import (create_node_csr, create_registration_token, enroll_node, node_roles,
                            registered_nodes, renew_node, revoke_node, verify_node_certificate,
                            verify_node_token)
from .version import application_version
from .rolling_update import reconcile_rollout, rollout_status, start_rollout
from adapters.telegram import TelegramAdapter
from adapters.publisher import PUBLISHERS

@asynccontextmanager
async def lifespan(_app):
    for recovered_job in list(store.jobs.values()):
        if (recovered_job.status in {JobStatus.NEW, JobStatus.WAITING_FOR_SYSTEM}
                and dispatch_allowed() and _job_is_due(recovered_job)):
            if recovered_job.status == JobStatus.WAITING_FOR_SYSTEM:
                store.update(recovered_job, JobStatus.NEW, "SYSTEM RETURNED TO NORMAL")
            executor.submit(_prepare_and_dispatch, recovered_job)
    watchdog_task = asyncio.create_task(_watchdog())
    yield
    watchdog_task.cancel()

app = FastAPI(title="Vertep CORE", version=application_version(), lifespan=lifespan)
store = JobStore(os.getenv("JOB_ROOT", "jobs"))
executor = ThreadPoolExecutor(max_workers=2)
task_queue = TaskQueue()
logger = configure_logging("core")
workflow_registry = WorkflowRegistry(os.getenv("WORKFLOWS_ROOT", "workflows"))
request_windows: dict[str, deque[float]] = defaultdict(deque)
last_maintenance = 0.0
result_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)

def _serialize_job_result(function):
    @wraps(function)
    def locked(result: TaskResult, request: Request):
        with result_locks[result.job_id]:
            return function(result, request)
    return locked

def _job_is_due(job) -> bool:
    if not job.scheduled_for:
        return True
    try:
        scheduled = datetime.fromisoformat(job.scheduled_for.replace("Z", "+00:00"))
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        return scheduled <= datetime.now(timezone.utc)
    except ValueError:
        return False

def _worker_tokens() -> dict[str, str]:
    result = {}
    for item in os.getenv("WORKER_TOKENS", "").split(","):
        if ":" in item:
            node, token = item.split(":", 1)
            result[node.strip()] = token.strip()
    return result

def _hash_secret(value: str, salt: str = "vertep", iterations: int = 200_000) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", value.encode(), salt.encode(), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"

def _verify_hash(value: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = _hash_secret(value, salt, int(iterations)).rsplit("$", 1)[1]
        return secrets.compare_digest(actual, expected)
    except ValueError:
        return False

def _valid_worker_token(node_name: str, supplied: str, client_dn: str = "",
                        client_serial: str = "") -> bool:
    if os.getenv("NODE_MTLS_REQUIRED", "false").lower() == "true":
        common_names = re.findall(r"(?:^|[,/])\s*CN=([^,/]+)", client_dn)
        if (not common_names or not secrets.compare_digest(common_names[-1], node_name)
                or not verify_node_certificate(node_name, client_serial)):
            return False
    if supplied and verify_node_token(supplied, node_name):
        return True
    if _hash_secret(supplied) in {item.strip() for item in os.getenv("REVOKED_TOKEN_HASHES", "").split(",") if item.strip()}:
        return False
    for item in os.getenv("WORKER_TOKEN_HASHES", "").split(","):
        if ":" in item:
            node, encoded = item.split(":", 1)
            if node.strip() == node_name:
                return _verify_hash(supplied, encoded.strip())
    expected = _worker_tokens().get(node_name) or os.getenv("NODE_API_TOKEN", "")
    return not expected or secrets.compare_digest(supplied, expected)


def _valid_worker_request(node_name: str, request: Request) -> bool:
    return _valid_worker_token(node_name, request.headers.get("x-vertep-token", ""),
                               request.headers.get("x-vertep-client-dn", ""),
                               request.headers.get("x-vertep-client-serial", ""))

def _session_token(user: str = "admin", role: str = "admin") -> str:
    expiry = str(int(time.time()) + int(os.getenv("SESSION_TTL", "28800")))
    payload = f"{expiry}:{user}:{role}"
    signature = hmac.new(session_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"

def _valid_session(token: str) -> tuple[str, str] | None:
    try:
        payload, signature = token.rsplit(".", 1)
        expiry, user, role = payload.split(":", 2)
        expected = hmac.new(session_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
        return (user, role) if int(expiry) > time.time() and secrets.compare_digest(signature, expected) else None
    except (ValueError, TypeError):
        return None

def _authenticate_user(user: str, password: str) -> str | None:
    configured = configured_user()
    if configured and secrets.compare_digest(user, configured[0]) and _verify_hash(password, configured[1]["password_hash"]):
        return str(configured[1].get("role", "admin"))
    try:
        users = json.loads(os.getenv("USERS_JSON", "{}"))
    except ValueError:
        users = {}
    record = users.get(user)
    if isinstance(record, dict) and _verify_hash(password, str(record.get("password_hash", ""))):
        return str(record.get("role", "viewer"))
    if secrets.compare_digest(user, os.getenv("ADMIN_USER", "admin")) and secrets.compare_digest(password, os.getenv("ADMIN_PASSWORD", "")):
        return "admin"
    return None

class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        setup_route = request.url.path.startswith(("/api/setup", "/setup.html", "/api/health"))
        if not is_configured() and not setup_route:
            if request.url.path == "/":
                return Response(status_code=307, headers={"Location": "/setup.html"})
            return Response("Complete the First Run Wizard", 503,
                            {"Retry-After": "30", "X-Vertep-Setup": "required"})
        if not is_configured() and setup_route:
            expected_setup = os.getenv("SETUP_TOKEN_HASH", "")
            if (expected_setup and request.url.path.startswith("/api/setup")
                    and not secrets.compare_digest(
                        hashlib.sha256(request.headers.get("x-vertep-setup-token", "").encode()).hexdigest(),
                        expected_setup)):
                return Response("Invalid setup code", 401)
            return self._secure(await call_next(request))
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
        if request.method != "GET" and request.url.path.startswith("/api/system/update") and role != "admin":
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
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
        return response

app.add_middleware(AdminAuthMiddleware)

@app.get("/api/setup")
def first_run_status():
    return {**setup_status(), "roles": node_roles()}

@app.get("/api/setup/health")
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

@app.post("/api/setup/complete")
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
        completed = complete_setup(str(payload.get("installation_name", "")), str(payload.get("username", "")),
                                   str(payload.get("password", "")), str(payload.get("password_confirmation", "")),
                                   str(payload.get("ai_backend", "skip")), payload.get("backend_url"),
                                   role, core_url or None, credentials)
        if role == "core":
            completed["core_url"] = os.getenv("PUBLIC_URL") or str(request.base_url).rstrip("/")
            completed["core_certificate"] = (Path(os.getenv("CORE_CERTIFICATE_PATH", "/data/config/pki/ca.crt"))
                                               .read_text(encoding="utf-8")
                                               if Path(os.getenv("CORE_CERTIFICATE_PATH", "/data/config/pki/ca.crt")).is_file()
                                               else None)
            completed["registration_token"] = create_registration_token("gpu", 900)
        return completed
    except FileExistsError as error:
        raise HTTPException(409, str(error)) from error
    except httpx.HTTPError as error:
        raise HTTPException(502, f"Core registration is unavailable: {error}") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error

def _scene_for_task(job, task_id: str):
    scene_id = job.active_task_ids.get(task_id)
    return next((scene for scene in job.scenes if scene.scene_id == scene_id), None)

def _task_for(job, scene=None) -> dict:
    workflow = job.workflow or "workflows/image/demo.json"
    script = job.script
    task_id = job.active_task_id
    scene_id = None
    if scene is not None:
        scene_id = scene.scene_id
        task_id = scene.task_id
        script = {"scenes": [{"prompt": scene.prompt,
                              "video_prompt": scene.video_prompt,
                              "voiceover": scene.voiceover,
                              "duration": scene.duration}]}
    return {"job_id": job.job_id, "task": job.task_type, "priority": job.priority,
            "min_vram_mb": job.min_vram_mb, "workflow": workflow,
            "topic": job.topic, "script": script, "task_id": task_id,
            "scene_id": scene_id}

def _validate_workflow_reference(reference: str | None, task_type: str) -> str:
    value = reference or f"workflows/{task_type}/demo.json"
    parts = Path(value).as_posix().split("/")
    if len(parts) != 3 or parts[0] != "workflows" or parts[1] != task_type:
        raise ValueError("Workflow must use workflows/<task_type>/<name>.json")
    workflow_registry.load(parts[1], parts[2])
    return value

def _enqueue_job_task(job, scene=None, *, new_attempt: bool = False, delay: float = 0) -> dict:
    task = _task_for(job, scene)
    if delay:
        task["not_before"] = time.time() + delay
    queued = task_queue.enqueue(task, new_attempt=new_attempt)
    job.active_task_id = queued["task_id"]
    if scene is not None:
        scene.task_id = queued["task_id"]
        job.active_task_ids[queued["task_id"]] = scene.scene_id
    store.repository.record_task(queued, "QUEUED")
    suffix = f" FOR {scene.scene_id}" if scene is not None else ""
    store.event(job, f"TASK {queued['task_id']} QUEUED{suffix}")
    return queued

def _recover_stale_workers() -> None:
    now = datetime.now(timezone.utc)
    timeout = int(os.getenv("HEARTBEAT_TIMEOUT", "45"))
    for worker in store.workers.values():
        last_seen = datetime.fromisoformat(worker["last_seen"])
        if (now - last_seen).total_seconds() <= timeout or worker.get("status") == "OFFLINE":
            continue
        worker["status"] = "OFFLINE"
        current_job = worker.get("current_job")
        current_task = worker.get("current_task")
        job = store.jobs.get(current_job) if current_job else None
        scene = _scene_for_task(job, current_task) if job and current_task else None
        if job and scene and scene.assigned_worker == worker.get("node_name") and job.status == JobStatus.ASSET_GENERATION:
            task_queue.release(current_task)
            interrupt_scene(scene, f"Worker {worker.get('node_name')} heartbeat timed out")
            job.assigned_worker = None
            store.event(job, f"{worker.get('node_name')} OFFLINE; TASK {current_task} REQUEUED")
            worker["current_job"] = None
            worker["current_task"] = None

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

def _demo_image(path: Path) -> None:
    width, height = 320, 180
    header = f"P6\n{width} {height}\n255\n".encode()
    pixels = bytes((34, 54, 48)) * width * height
    path.write_bytes(header + pixels)

def _finalize_and_notify(job, image: Path | list[Path]) -> None:
    while True:
        finalize_job_safe(store, job, image)
        if job.status != JobStatus.FAILED or not can_retry(job):
            break
        job.retries += 1
        store.update(job, JobStatus.ASSETS_READY, f"ASSEMBLY RETRY {job.retries}/{job.max_retries}")
    if job.status == JobStatus.READY and job.source.startswith("telegram:"):
        parts = job.source.split(":", 2)
        if len(parts) == 3 and parts[1] != "unknown":
            try:
                TelegramAdapter().send_message(parts[1], f"JOB {job.job_id}\nSTATUS: READY")
            except httpx.HTTPError as error:
                store.event(job, f"TELEGRAM NOTIFICATION FAILED: {error}")

def _ordered_scene_files(job) -> list[Path]:
    files = []
    accepted_kinds = {"video_scene"} if job.task_type == "video" else {"image"}
    for scene in sorted(job.scenes, key=lambda value: value.index):
        for artifact_id in scene.artifact_ids:
            artifact = next((value for value in job.artifacts
                             if value.artifact_id == artifact_id and value.kind in accepted_kinds), None)
            if artifact:
                path = store.root / job.job_id / artifact.path
                if path.is_file():
                    files.append(path)
    return files

def _prepare_and_dispatch(job) -> None:
    while True:
        if job.script and job.status == JobStatus.NEW:
            initialize_plan(job)
            assets_stage = job.stages[StageName.ASSETS.value]
            if assets_stage.status in {StageStatus.PENDING, StageStatus.FAILED, StageStatus.PAUSED}:
                transition_stage(job, StageName.ASSETS, StageStatus.RUNNING)
            store.update(job, JobStatus.ASSET_GENERATION, "IMAGE TASK REDISPATCHED")
        else:
            prepare_job_safe(store, job)
        if job.status != JobStatus.FAILED or not can_retry(job):
            break
        job.retries += 1
        store.update(job, JobStatus.NEW, f"AUTOMATIC RETRY {job.retries}/{job.max_retries}")
    if job.status != JobStatus.ASSET_GENERATION:
        return
    initialize_plan(job)
    if all_scenes_ready(job):
        images = _ordered_scene_files(job)
        if images:
            _finalize_and_notify(job, images)
        else:
            transition_stage(job, StageName.ASSETS, StageStatus.FAILED, "Recovered image artifacts are missing")
            store.update(job, JobStatus.FAILED, "RECOVERY FAILED: IMAGE ARTIFACTS ARE MISSING")
        return
    queued_tasks = [(scene, _enqueue_job_task(job, scene))
                    for scene in pending_scenes(job) if not scene.task_id]
    if os.getenv("LOCAL_WORKER_FALLBACK", "true").lower() == "true":
        images = []
        for scene, queued_task in queued_tasks:
            task_id = queued_task["task_id"]
            task_queue.discard(task_id)
            image = store.root / job.job_id / "images" / f"{scene.scene_id}.ppm"
            _demo_image(image)
            artifact = register_artifact(job, store.root, image, "image", scene_id=scene.scene_id,
                                         task_id=task_id, workflow=job.workflow)
            finish_scene(scene, [artifact.artifact_id])
            job.completed_task_ids.append(task_id)
            job.active_task_ids.pop(task_id, None)
            images.append(image)
        job.active_task_id = None
        if all_scenes_ready(job):
            transition_stage(job, StageName.ASSETS, StageStatus.READY)
            _finalize_and_notify(job, images)

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "core", "jobs": len(store.jobs)}

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

@app.get("/api/security/check")
def security_check():
    values = {"ADMIN_PASSWORD": os.getenv("ADMIN_PASSWORD", ""), "NODE_API_TOKEN": os.getenv("NODE_API_TOKEN", ""),
              "POSTGRES_PASSWORD": os.getenv("POSTGRES_PASSWORD", "")}
    weak = [key for key, value in values.items() if len(value) < 16 or "replace" in value.lower()]
    return {"ok": not weak, "weak_or_missing": weak, "recommendation": "Use random values of at least 32 characters"}

@app.get("/api/logs")
def logs(limit: int = 200, level: str | None = None, job_id: str | None = None):
    return read_logs(min(max(limit, 1), 1000), level, job_id)

@app.post("/api/logs/ingest")
def ingest_logs(batch: WorkerLogBatch, request: Request):
    if not _valid_worker_request(batch.node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    for entry in batch.entries:
        level = str(entry.get("level", "INFO")).upper()
        logger.log(getattr(__import__("logging"), level, 20), str(entry.get("message", "")),
                   extra={"node_name": batch.node_name, "job_id": entry.get("job_id")})
    return {"accepted": len(batch.entries)}

@app.get("/api/metrics")
def metrics():
    statuses = {status.value: sum(job.status == status for job in store.jobs.values()) for status in JobStatus}
    scene_statuses = {status.value: sum(scene.status == status for job in store.jobs.values() for scene in job.scenes)
                      for status in StageStatus}
    return {"jobs_total": len(store.jobs), "jobs_by_status": statuses, "queue_ready": task_queue.depth(),
            "queue_inflight": task_queue.inflight_depth(),
            "queue_dead_letter": len(task_queue.dead_letters()),
            "jobs_scheduled": sum(job.status == JobStatus.NEW and not _job_is_due(job) for job in store.jobs.values()),
            "scenes_by_status": scene_statuses,
            "workers_online": sum(worker.get("status") != "OFFLINE" for worker in store.workers.values())}

@app.get("/metrics", response_class=Response)
def prometheus_metrics():
    values = metrics()
    lines = [f"vertep_jobs_total {values['jobs_total']}", f"vertep_queue_ready {values['queue_ready']}",
             f"vertep_queue_inflight {values['queue_inflight']}",
             f"vertep_queue_dead_letter {values['queue_dead_letter']}",
             f"vertep_jobs_scheduled {values['jobs_scheduled']}",
             f"vertep_workers_online {values['workers_online']}"]
    lines.extend(f'vertep_jobs_status{{status="{status}"}} {count}' for status, count in values["jobs_by_status"].items())
    lines.extend(f'vertep_scenes_status{{status="{status}"}} {count}' for status, count in values["scenes_by_status"].items())
    return Response("\n".join(lines) + "\n", media_type="text/plain")

@app.get("/api/alerts")
def alerts():
    result = []
    for job in store.jobs.values():
        if job.status == JobStatus.FAILED:
            result.append({"severity": "error", "type": "JOB_FAILED", "job_id": job.job_id,
                           "message": job.events[-1] if job.events else "Job failed"})
    for worker in workers():
        if worker.get("status") in {"OFFLINE", "ERROR"}:
            result.append({"severity": "error", "type": "WORKER_OFFLINE", "node_name": worker["node_name"]})
    for task in task_queue.dead_letters():
        result.append({"severity": "error", "type": "DEAD_LETTER_TASK", "job_id": task.get("job_id"),
                       "task_id": task.get("task_id"), "message": task.get("error") or "Task retries exhausted"})
    return result[-200:]

@app.post("/api/maintenance/cleanup")
def maintenance_cleanup(dry_run: bool = True, retention_days: int | None = None):
    result = cleanup_jobs(store, retention_days, dry_run)
    result["temporary_files"] = cleanup_temporary_files(store.root, dry_run=dry_run)
    logger.info("Maintenance cleanup", extra={"action": "dry-run" if dry_run else "delete"})
    return result

@app.get("/api/workflows")
def workflows():
    return workflow_registry.list()

@app.get("/api/workflows/{kind}/{name}")
def get_workflow(kind: str, name: str):
    try:
        return workflow_registry.load(kind, name)
    except (ValueError, OSError) as error:
        raise HTTPException(404, str(error)) from error

@app.put("/api/workflows/{kind}/{name}")
def put_workflow(kind: str, name: str, workflow: dict):
    try:
        return workflow_registry.save(kind, name, workflow)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error

@app.post("/api/jobs")
def create_job(request: JobCreate):
    if not jobs_may_be_created():
        raise HTTPException(503, f"New jobs are disabled while system is {get_system_state()['state']}")
    try:
        character = load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), request.character_id)
    except Exception as error:
        raise HTTPException(400, f"Invalid character configuration: {error}") from error
    generation = character.generation
    if request.workflow is None:
        request.workflow = generation.get("workflow") or character.workflow
    if request.min_vram_mb == 0:
        request.min_vram_mb = int(generation.get("min_vram_mb", 0))
    request.aspect_ratio = character.visual.get("aspect_ratio", request.aspect_ratio)
    request.output_preset = character.visual.get("output_preset", request.output_preset)
    try:
        request.workflow = _validate_workflow_reference(request.workflow, request.task_type)
    except (ValueError, OSError) as error:
        raise HTTPException(400, f"Invalid workflow: {error}") from error
    job = store.create(request.topic, request.character_id, request.priority, request.source,
                       request.task_type, request.min_vram_mb, request.brand_id, request.workflow,
                       request.aspect_ratio, request.output_preset, request.scheduled_for)
    if not dispatch_allowed():
        store.update(job, JobStatus.WAITING_FOR_SYSTEM,
                     f"WAITING FOR SYSTEM: {get_system_state()['state']}")
    if request.scheduled_for:
        try:
            datetime.fromisoformat(request.scheduled_for.replace("Z", "+00:00"))
        except ValueError as error:
            store.delete(job.job_id)
            raise HTTPException(422, "scheduled_for must be an ISO-8601 datetime") from error
    if job.status == JobStatus.NEW and _job_is_due(job):
        executor.submit(_prepare_and_dispatch, job)
    elif job.status == JobStatus.NEW:
        store.event(job, f"SCHEDULED FOR {job.scheduled_for}")
    return job

@app.get("/api/jobs")
def list_jobs():
    return list(store.jobs.values())

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in store.jobs:
        raise HTTPException(404, "Job not found")
    return store.jobs[job_id]

@app.get("/api/jobs/{job_id}/assets")
def job_assets(job_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    integrity = {item["artifact_id"]: item["valid"] for item in verify_artifacts(job, store.root)}
    return [{"artifact_id": item.artifact_id, "name": item.filename, "kind": item.kind,
             "size": item.size, "mime_type": item.mime_type, "valid": integrity[item.artifact_id],
             "url": f"/api/jobs/{job_id}/artifacts/{item.artifact_id}/download"}
            for item in job.artifacts]

@app.patch("/api/jobs/{job_id}")
def edit_job(job_id: str, request: JobUpdate):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    changes = request.model_dump(exclude_none=True)
    expected_version = changes.pop("expected_version", None)
    if expected_version is not None and expected_version != job.version:
        raise HTTPException(409, f"Job changed; current version is {job.version}")
    prompt = changes.pop("prompt", None)
    script_changed = "script" in changes
    if "workflow" in changes:
        try:
            changes["workflow"] = _validate_workflow_reference(changes["workflow"], job.task_type)
        except (ValueError, OSError) as error:
            raise HTTPException(400, f"Invalid workflow: {error}") from error
    for key, value in changes.items():
        setattr(job, key, value)
    if prompt is not None:
        job.script = job.script or {"title": job.topic, "scenes": [{}]}
        scenes = job.script.setdefault("scenes", [{}])
        if not scenes:
            scenes.append({})
        scenes[0]["prompt"] = prompt
        script_changed = True
    if script_changed:
        job.scenes = []
        initialize_plan(job)
    job.version += 1
    if job.script is not None:
        (store.root / job.job_id / "script.json").write_text(json.dumps(job.script, indent=2), encoding="utf-8")
    return store.event(job, "JOB EDITED")

@app.post("/api/jobs/{job_id}/regenerate")
def regenerate_job(job_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    for task_id, scene_id in list(job.active_task_ids.items()):
        scene = next((item for item in job.scenes if item.scene_id == scene_id), None)
        if scene and scene.assigned_worker:
            task_queue.request_cancel(scene.assigned_worker, task_id)
        task_queue.discard(task_id)
    job_root = (store.root / job.job_id).resolve()
    retained_artifacts = []
    for artifact in job.artifacts:
        if artifact.kind == "input":
            retained_artifacts.append(artifact)
            continue
        path = (job_root / artifact.path).resolve()
        if job_root in path.parents and path.is_file():
            path.unlink()
    job.retries = 0
    job.script = None
    job.scenes = []
    job.stages = {}
    job.artifacts = retained_artifacts
    job.active_task_id = None
    job.active_task_ids.clear()
    job.completed_task_ids.clear()
    job.assigned_worker = None
    job.output_path = None
    job.approved = False
    job.published_to.clear()
    job.publication_results.clear()
    job.version += 1
    write_manifest(job, store.root)
    store.update(job, JobStatus.NEW, "REGENERATION REQUESTED")
    executor.submit(_prepare_and_dispatch, job)
    return job

@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not can_retry(job):
        return store.update(job, JobStatus.FAILED, "MAX RETRIES REACHED")
    job.retries += 1
    store.update(job, JobStatus.NEW, "RETRY REQUESTED")
    executor.submit(_prepare_and_dispatch, job)
    return job

def _job_action(job_id: str, status: JobStatus, event: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
        task_status = "CANCELLED" if status == JobStatus.CANCELLED else "PAUSED"
        for task_id, scene_id in list(job.active_task_ids.items()):
            scene = next((item for item in job.scenes if item.scene_id == scene_id), None)
            worker = scene.assigned_worker if scene else None
            if worker:
                task_queue.request_cancel(worker, task_id)
            task_queue.discard(task_id)
            store.repository.record_task(_task_for(job, scene) | {"task_id": task_id},
                                         task_status, worker)
            if scene:
                scene.status = StageStatus.CANCELLED if status == JobStatus.CANCELLED else StageStatus.PAUSED
                scene.task_id = None
                scene.assigned_worker = None
        job.active_task_ids.clear()
        job.active_task_id = None
        job.assigned_worker = None
        if job.stages and job.stages[StageName.ASSETS.value].status == StageStatus.RUNNING:
            transition_stage(job, StageName.ASSETS,
                             StageStatus.CANCELLED if status == JobStatus.CANCELLED else StageStatus.PAUSED)
    elif status == JobStatus.NEW:
        for scene in job.scenes:
            if scene.status == StageStatus.PAUSED:
                scene.status = StageStatus.PENDING
        if job.stages and job.stages[StageName.ASSETS.value].status == StageStatus.PAUSED:
            transition_stage(job, StageName.ASSETS, StageStatus.RUNNING)
    return store.update(job, status, event)

@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str):
    return _job_action(job_id, JobStatus.PAUSED, "JOB PAUSED")

@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str):
    job = _job_action(job_id, JobStatus.NEW, "JOB RESUMED")
    executor.submit(_prepare_and_dispatch, job)
    return job

@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    return _job_action(job_id, JobStatus.CANCELLED, "JOB CANCELLED")

@app.post("/api/jobs/{job_id}/approve")
def approve_job(job_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.approved = True
    return store.event(job, "JOB APPROVED")

@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    if not store.delete(job_id):
        raise HTTPException(404, "Job not found")
    return {"deleted": job_id}

@app.post("/api/workers/heartbeat")
def heartbeat(payload: WorkerHeartbeat, request: Request):
    if not _valid_worker_request(payload.node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    data = payload.model_dump()
    if data["status"] in {"ONLINE", "FREE"}:
        data["status"] = "READY"
    role = node_roles().get(data["role"], {})
    allowed = set(role.get("capabilities", []))
    declared = set(data.get("capabilities", [])) & allowed
    self_test = data.get("self_test") or {}
    data["capabilities"] = sorted(declared)
    data["tested_capabilities"] = (sorted(declared) if self_test.get("status") == "PASSED"
                                    and self_test.get("role") == data["role"] else [])
    if (os.getenv("REQUIRE_WORKER_SELF_TEST", "false").lower() == "true"
            and data["status"] == "READY" and not data["tested_capabilities"]):
        data["status"] = "ERROR"
    previous = store.workers.get(payload.node_name)
    desired = (previous or {}).get("desired_state")
    if desired == "QUARANTINED":
        data["status"] = "QUARANTINED"
        data["desired_state"] = desired
    elif desired == "DRAINING":
        data["desired_state"] = desired
        if data["status"] != "BUSY" and not data.get("current_task"):
            data["status"] = "DRAINING"
    if previous and previous.get("self_test_requested_at"):
        checked_at = str((data.get("self_test") or {}).get("checked_at", ""))
        if checked_at > previous["self_test_requested_at"]:
            data["self_test_requested_at"] = None
        else:
            data["self_test_requested_at"] = previous["self_test_requested_at"]
    if previous and not worker_transition_allowed(previous.get("status", "OFFLINE"), data["status"]):
        raise HTTPException(409, f"Illegal worker state transition: {previous.get('status')} -> {data['status']}")
    data["last_seen"] = utc_now()
    store.workers[payload.node_name] = data
    store.save_worker(data)
    reconcile_rollout(store.workers)
    data = store.workers[payload.node_name]
    store.save_worker(data)
    return {"accepted": True, "workers": len(store.workers),
            "desired_state": data.get("desired_state"),
            "self_test_requested_at": data.get("self_test_requested_at"),
            "update_target_version": data.get("update_target_version")}

@app.get("/api/workers")
def workers():
    _recover_stale_workers()
    now = datetime.now(timezone.utc)
    timeout = int(os.getenv("HEARTBEAT_TIMEOUT", "45"))
    result = []
    for worker in store.workers.values():
        last_seen = datetime.fromisoformat(worker["last_seen"])
        item = dict(worker)
        if (now - last_seen).total_seconds() > timeout:
            item["status"] = "OFFLINE"
        result.append(item)
    return result

@app.get("/api/characters")
def characters():
    root = Path(os.getenv("CHARACTERS_ROOT", "characters"))
    result = []
    for path in root.glob("*/character.json"):
        try:
            result.append(load_character(root, path.parent.name).model_dump())
        except (OSError, ValueError):
            continue
    return result

@app.get("/api/characters/{character_id}")
def get_character(character_id: str):
    if not SAFE_ID.fullmatch(character_id):
        raise HTTPException(400, "Invalid character ID")
    try:
        return load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), character_id)
    except Exception as error:
        raise HTTPException(404, f"Character not found or invalid: {error}") from error

@app.put("/api/characters/{character_id}")
def put_character(character_id: str, config: CharacterConfig):
    if character_id != config.id:
        raise HTTPException(400, "Character ID cannot be changed")
    save_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), config)
    return config

@app.delete("/api/characters/{character_id}")
def delete_character(character_id: str):
    if any(job.character_id == character_id for job in store.jobs.values()):
        raise HTTPException(409, "Character is referenced by jobs")
    directory = Path(os.getenv("CHARACTERS_ROOT", "characters")) / character_id
    if not directory.is_dir():
        raise HTTPException(404, "Character not found")
    shutil.rmtree(directory)
    return {"deleted": character_id}

@app.get("/api/brands")
def brands():
    root = Path(os.getenv("BRANDS_ROOT", "brands"))
    result = []
    for path in root.glob("*/brand.json"):
        try:
            result.append(BrandConfig.model_validate(read_json(path)).model_dump())
        except ValueError:
            continue
    return result

@app.put("/api/brands/{brand_id}")
def put_brand(brand_id: str, config: BrandConfig):
    if brand_id != config.id:
        raise HTTPException(400, "Brand ID cannot be changed")
    directory = Path(os.getenv("BRANDS_ROOT", "brands")) / brand_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brand.json").write_text(config.model_dump_json(indent=2), encoding="utf-8")
    return config

@app.post("/api/telegram/webhook")
def telegram_webhook(update: dict, request: Request):
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if webhook_secret and not secrets.compare_digest(request.headers.get("x-telegram-bot-api-secret-token", ""), webhook_secret):
        raise HTTPException(401, "Invalid Telegram webhook secret")
    callback = update.get("callback_query")
    if callback:
        data = str(callback.get("data", ""))
        action, _, job_id = data.partition(":")
        job = store.jobs.get(job_id)
        if job and action == "cancel":
            cancel_job(job_id)
        text = f"{job_id}: {job.status.value}" if job else "Job not found"
        return TelegramAdapter().answer_callback(str(callback.get("id", "")), text)
    message = update.get("message", {})
    text = str(message.get("text") or message.get("caption") or "").strip()
    if not text:
        raise HTTPException(400, "Telegram update has no text")
    chat_id = str(message.get("chat", {}).get("id", "unknown"))
    allowed = {item.strip() for item in os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if item.strip()}
    if allowed and chat_id not in allowed:
        raise HTTPException(403, "Telegram chat is not allowed")
    source_id = str(message.get("message_id", ""))
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
    character_id = os.getenv("TELEGRAM_DEFAULT_CHARACTER", "did_samogon")
    if text.startswith("/new "):
        parts = text.split(maxsplit=2)
        if len(parts) == 3:
            character_id, text = parts[1], parts[2]
    source = f"telegram:{chat_id}:{source_id or 'unknown'}"
    if source_id and store.repository.has_telegram_update(chat_id, source_id):
        return next((job for job in store.jobs.values() if job.source == source), {"duplicate": True})
    for existing in store.jobs.values():
        if existing.source == source:
            return existing
    try:
        load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), character_id)
    except Exception as error:
        raise HTTPException(400, f"Unknown character: {character_id}") from error
    job = store.create(text, character_id, int(os.getenv("TELEGRAM_DEFAULT_PRIORITY", "5")), source)
    if source_id:
        store.repository.record_telegram_update(chat_id, source_id, update)
    attachments = {key: message.get(key) for key in ("photo", "video", "document", "audio") if message.get(key)}
    if attachments:
        reference = store.root / job.job_id / "references" / "telegram.json"
        reference.write_text(json.dumps(attachments, ensure_ascii=False, indent=2), encoding="utf-8")
        register_artifact(job, store.root, reference, "input", workflow="telegram:webhook")
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
                    downloaded = store.root / job.job_id / "references" / Path(filename).name
                    if downloaded.exists():
                        downloaded = downloaded.with_name(
                            f"{downloaded.stem}-{hashlib.sha256(data).hexdigest()[:8]}{downloaded.suffix}")
                    downloaded.write_bytes(data)
                    register_artifact(job, store.root, downloaded, "input", workflow="telegram:download")
                except Exception as error:
                    store.event(job, f"TELEGRAM DOWNLOAD FAILED: {error}")
    TelegramAdapter().send_message(chat_id, f"JOB {job.job_id}\nSTATUS: NEW",
                                   {"inline_keyboard": [[{"text": "Status", "callback_data": f"status:{job.job_id}"},
                                                         {"text": "Cancel", "callback_data": f"cancel:{job.job_id}"}]]})
    executor.submit(_prepare_and_dispatch, job)
    return job

@app.post("/api/telegram/setup")
def telegram_setup():
    public_url = os.getenv("PUBLIC_URL", "")
    if not public_url:
        raise HTTPException(400, "PUBLIC_URL is not configured")
    try:
        return TelegramAdapter().set_webhook(public_url, os.getenv("TELEGRAM_WEBHOOK_SECRET", ""))
    except (RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(502, str(error)) from error

@app.post("/api/jobs/{job_id}/publish")
def publish_job(job_id: str, channels: list[str] | None = None):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    retryable_publish_failure = (job.status == JobStatus.FAILED and bool(job.publication_results)
                                 and bool(job.output_path) and Path(job.output_path).is_file())
    if job.status != JobStatus.READY and not retryable_publish_failure:
        raise HTTPException(409, "Job is not ready")
    targets = channels or ["youtube"]
    unknown = [channel for channel in targets if channel not in PUBLISHERS]
    if unknown:
        raise HTTPException(400, f"Unknown publishers: {', '.join(unknown)}")
    metadata = (job.script or {}) | {"job_id": job.job_id, "character_id": job.character_id,
                                     "brand_id": job.brand_id}
    results = {}
    attempts = max(1, int(os.getenv("PUBLISH_MAX_RETRIES", str(job.max_retries))))
    for attempt in range(1, attempts + 1):
        transition_stage(job, StageName.PUBLISH, StageStatus.RUNNING)
        store.update(job, JobStatus.PUBLISHING, f"PUBLISHING {attempt}/{attempts}: {', '.join(targets)}")
        results = {}
        for channel in targets:
            try:
                results[channel] = PUBLISHERS[channel].publish(job.output_path or "", metadata)
            except Exception as error:
                logger.exception("Publisher failed", extra={"job_id": job.job_id, "action": channel})
                results[channel] = {"channel": channel, "status": "FAILED", "error": str(error)}
        failed = [result for result in results.values() if result.get("status") != "PUBLISHED"]
        if not failed:
            transition_stage(job, StageName.PUBLISH, StageStatus.READY)
            break
        error = "; ".join(str(result.get("error") or result.get("status")) for result in failed)
        transition_stage(job, StageName.PUBLISH, StageStatus.FAILED, error)
        if any(result.get("status") == "NOT_CONFIGURED" for result in failed):
            break
        store.event(job, f"PUBLISH RETRY {attempt}/{attempts}: {error}")
    job.publication_results.update(results)
    job.published_to = [channel for channel, result in results.items() if result["status"] == "PUBLISHED"]
    if len(job.published_to) == len(targets):
        return store.update(job, JobStatus.PUBLISHED, "PUBLISHED")
    return store.update(job, JobStatus.FAILED, "PUBLISHING FAILED OR NOT CONFIGURED")

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
            "telegram": "CONFIGURED" if os.getenv("TELEGRAM_BOT_TOKEN") else "NOT CONFIGURED", "workers": workers()}

@app.post("/api/nodes/registration-tokens")
async def registration_token(request: Request):
    payload = await request.json()
    try:
        return create_registration_token(str(payload.get("role", "")), int(payload.get("ttl_seconds", 900)))
    except ValueError as error:
        raise HTTPException(422, str(error)) from error

@app.post("/api/nodes/register")
async def register_node(request: Request):
    payload = await request.json()
    try:
        if (not isinstance(payload, dict) or len(str(payload.get("registration_token", ""))) > 64
                or len(str(payload.get("node_id", ""))) > 64 or len(str(payload.get("version", ""))) > 64
                or len(str(payload.get("csr", ""))) > 16384
                or not isinstance(payload.get("capabilities", []), list)
                or len(payload.get("capabilities", [])) > 32
                or not isinstance(payload.get("hardware", {}), dict)
                or len(json.dumps(payload.get("hardware", {}))) > 65536):
            raise ValueError("Node registration payload exceeds allowed limits")
        return enroll_node(str(payload.get("registration_token", "")), str(payload.get("node_id", "")),
                           payload.get("capabilities") or [], payload.get("hardware") or {},
                           str(payload.get("version", "unknown")), str(payload.get("csr", "")))
    except PermissionError as error:
        raise HTTPException(401, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error

@app.get("/api/nodes")
def nodes():
    live = {item.get("node_name"): item for item in workers()}
    return [{**node, "runtime": live.get(node["node_id"]),
             "status": (live.get(node["node_id"]) or {}).get("status", "OFFLINE")}
            for node in registered_nodes()]


@app.post("/api/nodes/{node_id}/actions")
def control_node(node_id: str, command: NodeAction):
    worker = store.workers.get(node_id)
    if not worker:
        raise HTTPException(404, "Worker runtime is not available")
    timestamp = utc_now()
    if command.action == "drain":
        worker["desired_state"] = "DRAINING"
        if worker.get("status") != "BUSY" and not worker.get("current_task"):
            worker["status"] = "DRAINING"
    elif command.action == "resume":
        if worker.get("desired_state") == "QUARANTINED":
            raise HTTPException(409, "Quarantined worker must be explicitly unquarantined")
        worker.pop("desired_state", None)
        worker["status"] = "READY" if (worker.get("self_test") or {}).get("status") == "PASSED" else "ERROR"
    elif command.action == "quarantine":
        worker.update({"desired_state": "QUARANTINED", "status": "QUARANTINED"})
    elif command.action == "unquarantine":
        if worker.get("desired_state") != "QUARANTINED":
            raise HTTPException(409, "Worker is not quarantined")
        worker.pop("desired_state", None)
        worker["status"] = "SELF_TESTING"
        worker["self_test_requested_at"] = timestamp
    elif command.action == "self-test":
        if worker.get("status") == "BUSY":
            raise HTTPException(409, "Busy worker cannot start a self-test")
        worker["self_test_requested_at"] = timestamp
        worker["status"] = "SELF_TESTING"
    worker["state_reason"] = command.reason
    worker["state_changed_at"] = timestamp
    store.save_worker(worker)
    return worker

@app.post("/api/nodes/{node_id}/revoke")
def disable_node(node_id: str):
    try:
        return revoke_node(node_id)
    except KeyError as error:
        raise HTTPException(404, "Node not found") from error

@app.post("/api/nodes/{node_id}/renew")
async def renew_node_credentials(node_id: str, request: Request):
    if not _valid_worker_request(node_id, request):
        raise HTTPException(401, "Node credentials are not valid")
    payload = await request.json()
    try:
        return renew_node(node_id, str(payload.get("csr", "")))
    except KeyError as error:
        raise HTTPException(404, "Node is missing or revoked") from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error

@app.get("/api/system/update")
def web_update_status():
    return {**update_status(), "system": get_system_state()}

@app.get("/api/system/update/readiness")
def update_readiness():
    active = [job.job_id for job in store.jobs.values() if job.status not in {
        JobStatus.NEW, JobStatus.WAITING_FOR_SYSTEM, JobStatus.READY, JobStatus.PUBLISHED, JobStatus.FAILED,
        JobStatus.PAUSED, JobStatus.CANCELLED}]
    busy = [worker.get("node_name") for worker in store.workers.values()
            if worker.get("status") == "BUSY" or worker.get("current_task")]
    return {"ready": not active and not busy and task_queue.inflight_depth() == 0,
            "active_jobs": active, "busy_workers": busy,
            "queue_paused": not dispatch_allowed(), "inflight": task_queue.inflight_depth()}

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


@app.get("/api/system/update/rolling")
def rolling_update_status():
    return rollout_status()


@app.post("/api/system/update/rolling")
def begin_rolling_update(payload: RollingUpdateRequest):
    registered = {node["node_id"] for node in registered_nodes() if not node.get("revoked_at")}
    unknown = sorted(set(payload.node_ids) - registered)
    if unknown:
        raise HTTPException(422, f"Unknown or revoked nodes: {', '.join(unknown)}")
    try:
        rollout = start_rollout(payload.target_version, payload.node_ids)
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

@app.get("/api/integrations")
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


@app.get("/api/settings/secrets")
def secret_settings():
    return {"secrets": integration_secret_status(), "values_exposed": False}


@app.put("/api/settings/secrets/{name}")
def update_secret_setting(name: str, payload: IntegrationSecretUpdate):
    try:
        return {"secrets": set_integration_secret(name, payload.value), "values_exposed": False}
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@app.delete("/api/settings/secrets/{name}")
def delete_secret_setting(name: str):
    try:
        return {"secrets": set_integration_secret(name, None), "values_exposed": False}
    except ValueError as error:
        raise HTTPException(422, str(error)) from error

@app.post("/api/tasks/claim")
def claim_task(payload: TaskClaim, request: Request):
    if not _valid_worker_request(payload.node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    if not dispatch_allowed():
        return {"task": None, "system_state": get_system_state()["state"]}
    registered_worker = store.workers.get(payload.node_name)
    if not registered_worker:
        return {"task": None, "worker_state": "HEARTBEAT_REQUIRED"}
    worker_data = dict(registered_worker)
    claim_metrics = payload.model_dump(exclude={"node_name", "capabilities"})
    worker_data.update(claim_metrics)
    held_tasks = []
    scan_limit = max(1, task_queue.depth())
    for _ in range(scan_limit):
        task = task_queue.claim()
        if not task:
            break
        job = store.jobs.get(task["job_id"])
        worker = available_worker([worker_data], job) if job else None
        if job and worker:
            for held_task_id in held_tasks:
                task_queue.release(held_task_id)
            held_tasks.clear()
        else:
            held_tasks.append(task["task_id"])
            continue
        scene = _scene_for_task(job, task.get("task_id", ""))
        if job.status != JobStatus.ASSET_GENERATION or not scene:
            task_queue.ack(task["task_id"])
            continue
        start_scene(scene, task["task_id"], payload.node_name)
        job.assigned_worker = payload.node_name
        store.workers[payload.node_name] = worker_data
        store.workers[payload.node_name]["status"] = "BUSY"
        store.workers[payload.node_name]["current_job"] = job.job_id
        store.workers[payload.node_name]["current_task"] = task["task_id"]
        store.save_worker(store.workers[payload.node_name])
        store.event(job, f"{payload.node_name} ASSIGNED TO {scene.scene_id}")
        store.repository.record_task(task, "CLAIMED", payload.node_name)
        return {"task": task}
    for held_task_id in held_tasks:
        task_queue.release(held_task_id)
    return {"task": None}

@app.post("/api/tasks/renew")
def renew_task(payload: TaskRenew, request: Request):
    if not _valid_worker_request(payload.node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    job = next((job for job in store.jobs.values() if payload.task_id in job.active_task_ids), None)
    scene = _scene_for_task(job, payload.task_id) if job else None
    if not job or not scene or scene.assigned_worker != payload.node_name:
        raise HTTPException(409, "Task is no longer assigned to this worker")
    return {"renewed": task_queue.renew(payload.task_id)}

@app.get("/api/tasks/cancellations/{node_name}")
def task_cancellations(node_name: str, request: Request):
    if not _valid_worker_request(node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    return task_queue.pop_cancellations(node_name)

@app.get("/api/tasks/dead-letter")
def dead_letter_tasks():
    return task_queue.dead_letters()

@app.post("/api/tasks/dead-letter/{task_id}/retry")
def retry_dead_letter_task(task_id: str):
    queued = task_queue.requeue_dead_letter(task_id)
    if not queued:
        raise HTTPException(404, "Dead-letter task not found")
    job = store.jobs.get(queued.get("job_id"))
    scene_id = queued.get("scene_id")
    scene = next((item for item in job.scenes if item.scene_id == scene_id), None) if job else None
    if not job or not scene:
        task_queue.discard(queued["task_id"])
        raise HTTPException(409, "Job or scene no longer exists")
    scene.status = StageStatus.FAILED
    scene.task_id = queued["task_id"]
    job.active_task_ids[queued["task_id"]] = scene.scene_id
    job.active_task_id = queued["task_id"]
    if job.stages[StageName.ASSETS.value].status == StageStatus.FAILED:
        transition_stage(job, StageName.ASSETS, StageStatus.RUNNING)
    store.update(job, JobStatus.ASSET_GENERATION, f"DEAD-LETTER TASK {task_id} REQUEUED")
    store.repository.record_task(queued, "QUEUED")
    return queued

@app.post("/api/tasks/result")
@_serialize_job_result
def task_result(result: TaskResult, request: Request):
    if not _valid_worker_request(result.node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    job = store.jobs.get(result.job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if result.task_id in job.completed_task_ids:
        completed_scene = next((item for item in job.scenes if item.task_id == result.task_id), None)
        owner = completed_scene.attempts[-1].node_name if completed_scene and completed_scene.attempts else None
        if owner != result.node_name:
            raise HTTPException(409, "Completed task belongs to another worker")
        return job
    scene = _scene_for_task(job, result.task_id)
    if not scene:
        raise HTTPException(409, "Result does not match the active task")
    if scene.assigned_worker != result.node_name:
        raise HTTPException(409, "Task is assigned to another worker")
    if job.status in {JobStatus.CANCELLED, JobStatus.PAUSED}:
        raise HTTPException(409, f"Job is {job.status.value}")
    worker = store.workers.get(result.node_name)
    if worker:
        desired_status = worker.get("desired_state")
        next_status = desired_status if desired_status in {"DRAINING", "QUARANTINED"} else "READY"
        worker.update({"status": next_status, "current_job": None,
                       "current_task": None, "last_seen": utc_now()})
        store.save_worker(worker)
    if not result.success:
        task_queue.ack(result.task_id)
        store.repository.record_task(_task_for(job, scene) | {"task_id": result.task_id}, "FAILED", result.node_name, result.error)
        fail_scene(scene, result.error or "unknown error")
        job.active_task_ids.pop(result.task_id, None)
        scene.task_id = None
        scene.assigned_worker = None
        if len(scene.attempts) < job.max_retries:
            store.event(job, f"{scene.scene_id} FAILED, RETRY {len(scene.attempts)}/{job.max_retries}: {result.error or 'unknown error'}")
            delay = float(os.getenv("RETRY_BACKOFF_BASE", "2")) * (2 ** max(0, job.retries - 1))
            _enqueue_job_task(job, scene, new_attempt=True, delay=delay)
            return job
        job.active_task_id = None
        job.assigned_worker = None
        for sibling_task_id, sibling_scene_id in list(job.active_task_ids.items()):
            sibling = next((item for item in job.scenes if item.scene_id == sibling_scene_id), None)
            sibling_worker = sibling.assigned_worker if sibling else None
            if sibling_worker:
                task_queue.request_cancel(sibling_worker, sibling_task_id)
            task_queue.discard(sibling_task_id)
            if sibling:
                cancel_scene(sibling, f"Sibling {scene.scene_id} exhausted retries")
            store.repository.record_task(_task_for(job, sibling) | {"task_id": sibling_task_id},
                                         "CANCELLED", sibling_worker)
        job.active_task_ids.clear()
        transition_stage(job, StageName.ASSETS, StageStatus.FAILED, result.error or "unknown error")
        task_queue.dead_letter(_task_for(job, scene) | {"task_id": result.task_id}, result.error)
        return store.update(job, JobStatus.FAILED, f"WORKER FAILED: {result.error or 'unknown error'}")
    artifacts = result.artifacts or result.images or ([{"filename": result.filename,
                                                         "image_base64": result.image_base64}]
                                                       if result.image_base64 else [])
    if not artifacts:
        raise HTTPException(400, "Successful result has no artifact")
    saved_images = []
    saved_artifacts = []
    prepared_images = []
    total_size = 0
    for artifact_index, artifact in enumerate(artifacts, 1):
        try:
            encoded = artifact.get("data_base64") or artifact.get("image_base64")
            data = base64.b64decode(encoded, validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error) as error:
            raise HTTPException(400, "Invalid base64 artifact") from error
        total_size += len(data)
        max_artifact_bytes = int(os.getenv("MAX_VIDEO_ARTIFACT_BYTES", "268435456")) \
            if job.task_type == "video" else int(os.getenv("MAX_ARTIFACT_BYTES", "26214400"))
        if total_size > max_artifact_bytes:
            raise HTTPException(413, "Artifacts are too large")
        supplied_name = Path(artifact.get("filename", f"{scene.scene_id}.png")).name
        suffix = Path(supplied_name).suffix.lower()
        artifact_contracts = {
            "video": ({".mp4", ".webm", ".mov"}, "video"),
            "text": ({".txt"}, "text"),
            "voice": ({".wav", ".ogg", ".mp3", ".aac", ".m4a"}, "audio"),
            "publish": ({".json"}, "receipts"),
            "backup": ({".json"}, "receipts"),
        }
        allowed_suffixes, folder = artifact_contracts.get(
            job.task_type, ({".png", ".jpg", ".jpeg", ".webp", ".ppm"}, "images"))
        if suffix not in allowed_suffixes:
            raise HTTPException(400, f"Unsupported {job.task_type} artifact type")
        try:
            validate_signature(data, suffix)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        filename = f"{scene.scene_id}{suffix}" if len(artifacts) == 1 else f"{scene.scene_id}-{artifact_index:03d}{suffix}"
        image_path = store.root / job.job_id / folder / filename
        prepared_images.append((image_path, data))
    temporary_images = []
    try:
        for image_path, data in prepared_images:
            temporary = image_path.with_name(f".{image_path.name}.{result.task_id[:8]}.part")
            temporary.write_bytes(data)
            temporary_images.append((temporary, image_path))
        for temporary, image_path in temporary_images:
            temporary.replace(image_path)
    except OSError as error:
        for temporary, _ in temporary_images:
            temporary.unlink(missing_ok=True)
        raise HTTPException(500, "Could not persist worker artifacts") from error
    for image_path, _ in prepared_images:
        saved_images.append(image_path)
        artifact_kind = {"video": "video_scene", "text": "text", "voice": "audio",
                         "publish": "publication_receipt", "backup": "backup_receipt"}.get(
                             job.task_type, "image")
        saved_artifacts.append(register_artifact(job, store.root, image_path, artifact_kind,
                                                 scene_id=scene.scene_id, task_id=result.task_id,
                                                 node_name=result.node_name, workflow=job.workflow))
    task_queue.ack(result.task_id)
    store.repository.record_task(_task_for(job, scene) | {"task_id": result.task_id}, "COMPLETED", result.node_name)
    job.completed_task_ids.append(result.task_id)
    finish_scene(scene, [item.artifact_id for item in saved_artifacts])
    job.active_task_ids.pop(result.task_id, None)
    job.active_task_id = next(iter(job.active_task_ids), None)
    job.assigned_worker = None
    if all_scenes_ready(job):
        transition_stage(job, StageName.ASSETS, StageStatus.READY)
        if job.task_type in {"text", "voice", "backup", "publish"}:
            terminal = JobStatus.PUBLISHED if job.task_type == "publish" else JobStatus.READY
            return store.update(job, terminal, f"{job.task_type.upper()} TASK COMPLETED")
        ordered_images = _ordered_scene_files(job)
        executor.submit(_finalize_and_notify, job, ordered_images)
    return store.event(job, f"RESULT FOR {scene.scene_id} RECEIVED FROM {result.node_name}")

@app.get("/jobs/{job_id}/final/video.mp4")
def video(job_id: str):
    job = store.jobs.get(job_id)
    artifact = next((item for item in job.artifacts
                     if item.kind == "video" and item.path == "final/video.mp4"), None) if job else None
    if not artifact:
        raise HTTPException(404, "Video not ready")
    return download_artifact(job_id, artifact.artifact_id)

@app.get("/api/jobs/{job_id}/artifacts")
def job_artifacts(job_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"job_id": job_id, "artifacts": job.artifacts}

@app.post("/api/jobs/{job_id}/artifacts/verify")
def verify_job_artifacts(job_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    results = verify_artifacts(job, store.root)
    return {"job_id": job_id, "valid": all(item["valid"] for item in results), "results": results}

@app.get("/api/jobs/{job_id}/artifacts/{artifact_id}/download")
def download_artifact(job_id: str, artifact_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    artifact = next((item for item in job.artifacts if item.artifact_id == artifact_id), None)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    integrity = next(item for item in verify_artifacts(job, store.root) if item["artifact_id"] == artifact_id)
    if not integrity["valid"]:
        raise HTTPException(409, "Artifact failed its integrity check")
    path = store.root / job_id / artifact.path
    return FileResponse(path, media_type=artifact.mime_type, filename=artifact.filename)

@app.put("/api/jobs/{job_id}/uploads/{folder}/{filename}")
async def upload_job_file(job_id: str, folder: str, filename: str, request: Request):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    allowed = {
        "references": {".png", ".jpg", ".jpeg", ".webp", ".txt", ".json"},
        "audio": {".wav", ".mp3", ".aac", ".m4a", ".ogg"},
    }
    safe_name = Path(filename).name
    suffix = Path(safe_name).suffix.lower()
    if folder not in allowed or safe_name != filename or suffix not in allowed[folder]:
        raise HTTPException(400, "Unsupported upload target or file type")
    limit = int(os.getenv("MAX_UPLOAD_BYTES", "26214400"))
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "Upload is too large")
        chunks.append(chunk)
    if not size:
        raise HTTPException(400, "Upload is empty")
    data = b"".join(chunks)
    try:
        validate_signature(data, suffix)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    destination = store.root / job_id / folder / safe_name
    if destination.exists():
        raise HTTPException(409, "File already exists")
    destination.write_bytes(data)
    artifact = register_artifact(job, store.root, destination, "input", workflow="user-upload")
    store.event(job, f"INPUT {artifact.artifact_id} UPLOADED")
    return artifact

@app.get("/api/jobs/{job_id}/export")
def export_job(job_id: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    source = store.root / job_id
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in source.rglob("*"):
            if path.is_file() and not path.name.endswith(".tmp"):
                archive.write(path, path.relative_to(source).as_posix())
    return Response(buffer.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="vertep-{job_id}.zip"'})

@app.post("/api/projects/import")
async def import_project(request: Request):
    data = await request.body()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        names = archive.namelist()
        if "job.json" not in names:
            raise ValueError("job.json is missing")
        if sum(item.file_size for item in archive.infolist()) > int(os.getenv("MAX_IMPORT_BYTES", "209715200")):
            raise ValueError("uncompressed archive is too large")
        for name in names:
            parts = Path(name).parts
            if Path(name).is_absolute() or ".." in parts:
                raise ValueError("unsafe archive path")
        original = json.loads(archive.read("job.json"))
    except (ValueError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        raise HTTPException(400, f"Invalid project archive: {error}") from error
    imported = store.create(str(original.get("topic") or "Imported project"),
                            str(original.get("character_id") or "did_samogon"),
                            int(original.get("priority", 5)), source="import")
    new_id = imported.job_id
    imported_status = str(original.get("status", JobStatus.PAUSED.value))
    if imported_status not in {JobStatus.READY.value, JobStatus.PUBLISHED.value,
                               JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
        imported_status = JobStatus.PAUSED.value
    original.update({"job_id": new_id, "created_at": imported.created_at,
                     "status": imported_status, "source": "import",
                     "active_task_id": None, "active_task_ids": {}, "assigned_worker": None,
                     "output_path": None})
    try:
        imported = type(imported).model_validate(original)
    except ValueError as error:
        store.delete(new_id)
        raise HTTPException(400, f"Invalid project metadata: {error}") from error
    destination_root = store.root / new_id
    for item in archive.infolist():
        if item.is_dir() or item.filename == "job.json":
            continue
        destination = destination_root / item.filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive.read(item))
    store.jobs[new_id] = imported
    write_manifest(imported, store.root)
    store.event(imported, "PROJECT IMPORTED AND PAUSED")
    return imported

@app.get("/jobs/{job_id}/files/{folder}/{filename}")
def job_file(job_id: str, folder: str, filename: str):
    if folder not in {"images", "audio", "subtitles", "final"}:
        raise HTTPException(400, "Unsupported asset folder")
    safe_name = Path(filename).name
    job = store.jobs.get(job_id)
    artifact = next((item for item in job.artifacts if item.path == f"{folder}/{safe_name}"), None) if job else None
    if not artifact:
        raise HTTPException(404, "Asset not found")
    return download_artifact(job_id, artifact.artifact_id)

app.mount("/", StaticFiles(directory="web", html=True), name="web")
