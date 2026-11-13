"""Task claim/result routes for the Vertep CORE web application.

Worker-facing task lifecycle endpoints (owning a task, renewing a lease,
cancellations, dead-letter queue and result submission). Extracted from
``core/app.py``.
"""
import threading
from .workflow_execution import router as workflow_execution_router
import hashlib
import re
from pathlib import Path

from fastapi.responses import FileResponse
from fastapi import APIRouter, HTTPException, Query, Request, Response

from ..models import (JobStatus, StageName, StageStatus, TaskClaim, TaskRenew,
                      TaskResult, utc_now)
from ..orchestration import start_scene, transition_stage
from ..security import _valid_worker_request
from ..state import logger, store, task_queue
from ..version import worker_incompatibility
from ..turns import may_claim_now, record_claim
from ..alert_store import get_alert_store
from ..system_state import dispatch_allowed, get_system_state
from .lab_lease import hold_gpu_for_laboratory
from .job_helpers import (
    _job_is_due,
    _redact_payload,
    _scene_for_task,
    _select_worker,
    _serialize_job_result,
    assembly_worker_mismatch,
    engine_release_hold)
from .task_results import (
    _text_task_lease,
    assembly_result,
    image_storyboard_result,
    publish_result,
    scene_result,
    script_result,
    storyboard_result,
    tts_result)

router = APIRouter()
router.include_router(workflow_execution_router)


def _report_gpu_outcome(job, result: TaskResult) -> None:
    """A failed GPU task asks the operator for a restart; nothing switches providers (#431)."""
    task = task_queue.find(result.task_id) or {}
    if not (result.task_id in job.image_storyboard_task_ids
            or task.get("task_type") in {"image", "video"} or task.get("kind") in {"image", "video"}):
        return
    identity = {"type": "GPU_PROVIDER_FAILED", "source": "gpu", "node_name": result.node_name}
    if result.success:
        get_alert_store().resolve(identity, "the node completed a GPU task")
    elif not getattr(result, "cancelled", False):
        error = str(result.error or "невідома помилка")[:300]
        named = re.match(r"(?:Allowed GPU providers exhausted: )?([\w.-]+): ", error)
        provider = named.group(1) if named else "ComfyUI"
        get_alert_store().record({**identity, "severity": "error", "details": {"provider": provider},
                                  "message": f"Збій GPU-провайдера {provider} на вузлі {result.node_name}: {error}. "
                                             "Автоматичного перемикання немає, потрібен перезапуск."})


@router.post("/api/tasks/claim")
def claim_task(payload: TaskClaim, request: Request):
    if not _valid_worker_request(payload.node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    # Concurrent claims of one node would both pass the "no current_task" check.
    with _claim_locks.setdefault(payload.node_name, threading.Lock()):
        if not may_claim_now(payload.node_name, store.workers):
            return {"task": None, "worker_state": "WAITING_FOR_TURN"}
        answer = _claim_task(payload)
        if answer and answer.get("task"):
            record_claim(payload.node_name, store.workers)
            store.save_worker(store.workers[payload.node_name])
        return answer


_claim_locks: dict[str, threading.Lock] = {}


def _claim_task(payload: TaskClaim):
    incompatibility = worker_incompatibility(store.workers.get(payload.node_name))
    if incompatibility:
        logger.warning("Incompatible worker refused", extra={"node_name": payload.node_name})
        return {"task": None, "reason": incompatibility}
    if not dispatch_allowed():
        return {"task": None, "system_state": get_system_state()["state"]}
    registered_worker = store.workers.get(payload.node_name)
    if not registered_worker:
        return {"task": None, "worker_state": "HEARTBEAT_REQUIRED"}
    if hold_gpu_for_laboratory(registered_worker):
        return {"task": None, "worker_state": "WAITING_FOR_LAB_RELEASE"}
    worker_data = dict(registered_worker)
    claim_metrics = payload.model_dump(exclude={"node_name", "capabilities"})
    worker_data.update(claim_metrics)
    # A claim that does not restate the voice catalog must not erase the one
    # the heartbeat declared: ``None`` means "not stated by this claim".
    if payload.voice_catalog is None:
        worker_data["voice_catalog"] = registered_worker.get("voice_catalog")
    # T6: readiness/model routing — a Text Worker must be idle and not in a
    # transitional/desired state before it can accept a new text task (script,
    # storyboard, TTS).  A busy or draining/quarantined worker is skipped so the
    # dispatcher never over-subscribes a node that cannot actually claim.
    desired_state = worker_data.get("desired_state")
    if desired_state in {"DRAINING", "QUARANTINED", "UPDATING", "ROLLBACK",
                         "DISABLED", "RESTARTING", "REVOKED", "SELF_TESTING"} \
            or worker_data.get("current_task"):
        return {
            "task": None,
            "worker_state": desired_state or worker_data.get(
                "status",
                "BUSY")}
    held_tasks = []
    scan_limit = max(1, task_queue.depth())
    for _ in range(scan_limit):
        task = task_queue.claim()
        if not task:
            break
        job = store.jobs.get(task["job_id"])
        if job is None:
            # The Job was deleted: its task can never run and must not block the queue.
            task_queue.ack(task["task_id"])
            continue
        if task.get("kind") == "image_storyboard":
            sb_version = task.get("storyboard_version")
            if job.status not in {JobStatus.STORYBOARD_PENDING_APPROVAL,
                                  JobStatus.STORYBOARD_READY} \
                    or job.active_storyboard_version != sb_version:
                task_queue.ack(task["task_id"])
                continue
            worker = _select_worker(
                [worker_data],
                job,
                task_type="image",
                min_vram_mb=task.get("min_vram_mb")) if job else None
            if not worker:
                held_tasks.append(task["task_id"])
                continue
            for held_task_id in held_tasks:
                task_queue.release(held_task_id)
            held_tasks.clear()
            store.workers[payload.node_name] = worker_data
            store.workers[payload.node_name]["status"] = "BUSY"
            store.workers[payload.node_name]["current_job"] = job.job_id
            store.workers[payload.node_name]["current_task"] = task["task_id"]
            store.save_worker(store.workers[payload.node_name])
            store.event(
                job, f"{payload.node_name} IMAGE STORYBOARD CLAIMED {task.get('scene_id')}")
            store.repository.record_task(task, "CLAIMED", payload.node_name)
            return {"task": task}
        if task.get("task") == "storyboard":
            if job.status != JobStatus.STORYBOARD_QUEUED or job.storyboard_task_id != task.get(
                    "task_id"):
                task_queue.ack(task["task_id"])
                continue
            worker = _select_worker(
                [worker_data],
                job,
                task_type="text",
                min_vram_mb=0) if job else None
            if not worker:
                held_tasks.append(task["task_id"])
                continue
            for held_task_id in held_tasks:
                task_queue.release(held_task_id)
            held_tasks.clear()
            store.workers[payload.node_name] = worker_data
            store.workers[payload.node_name]["status"] = "BUSY"
            store.workers[payload.node_name]["current_job"] = job.job_id
            store.workers[payload.node_name]["current_task"] = task["task_id"]
            store.save_worker(store.workers[payload.node_name])
            # Issue #64 T1: claim must transition QUEUED -> GENERATING so the
            # repository record and the Job lifecycle agree.  Without this the
            # job stays in STORYBOARD_QUEUED while the worker is actively
            # generating, and a concurrent second claim sees a claimable task.
            job.assigned_worker = payload.node_name
            store.update(
                job,
                JobStatus.STORYBOARD_GENERATING,
                f"STORYBOARD TASK {task['task_id']} ASSIGNED TO {payload.node_name}")
            store.event(
                job, f"{payload.node_name} STORYBOARD CLAIMED {task['task_id']}")
            store.repository.record_task(task, "CLAIMED", payload.node_name)
            return {"task": task}
        if task.get("task") == "script":
            if job.status != JobStatus.SCRIPT_QUEUED or job.script_task_id != task.get(
                    "task_id"):
                task_queue.ack(task["task_id"])
                continue
            worker = _select_worker(
                [worker_data],
                job,
                task_type="text",
                min_vram_mb=0) if job else None
            if not worker:
                held_tasks.append(task["task_id"])
                continue
            for held_task_id in held_tasks:
                task_queue.release(held_task_id)
            held_tasks.clear()
            store.workers[payload.node_name] = worker_data
            store.workers[payload.node_name]["status"] = "BUSY"
            store.workers[payload.node_name]["current_job"] = job.job_id
            store.workers[payload.node_name]["current_task"] = task["task_id"]
            store.save_worker(store.workers[payload.node_name])
            store.event(
                job, f"{payload.node_name} SCRIPT CLAIMED {task['task_id']}")
            store.repository.record_task(task, "CLAIMED", payload.node_name)
            job.assigned_worker = payload.node_name
            store.update(
                job,
                JobStatus.SCRIPT_GENERATING,
                f"SCRIPT TASK {task['task_id']} ASSIGNED TO {payload.node_name}")
            return {"task": task}
        if task.get("task") == "publish":
            channel = task.get("channel")
            if job.status != JobStatus.PUBLISHING or job.publish_task_ids.get(
                    task.get("task_id")) != channel:
                task_queue.ack(task["task_id"])
                continue
            worker = _select_worker(
                [worker_data],
                job,
                task_type="publish",
                min_vram_mb=0) if job else None
            if not worker:
                held_tasks.append(task["task_id"])
                continue
            for held_task_id in held_tasks:
                task_queue.release(held_task_id)
            held_tasks.clear()
            store.workers[payload.node_name] = worker_data
            store.workers[payload.node_name]["status"] = "BUSY"
            store.workers[payload.node_name]["current_job"] = job.job_id
            store.workers[payload.node_name]["current_task"] = task["task_id"]
            store.save_worker(store.workers[payload.node_name])
            store.event(
                job, f"{payload.node_name} PUBLISH CLAIMED {task['task_id']} for {channel}")
            store.repository.record_task(task, "CLAIMED", payload.node_name)
            return {"task": task}
        if task.get("task") == "assembly":
            version = job.assembly_task_ids.get(
                task.get("task_id")) if job else None
            if (version is None or job.assembly_task_id != task.get("task_id")
                    or job.status not in {JobStatus.ASSEMBLY, JobStatus.VIDEO_GENERATION}):
                # The attempt was cancelled, superseded or already imported: the
                # task is dropped instead of being executed by some other node.
                # A superseded attempt is dropped too: it is still mapped to a version,
                # but its render would be refused on import, so no node may spend an
                # upstream render on it (Issue #122 P5/P6/P9).
                task_queue.ack(task["task_id"])
                continue
            if job.assembly_cancel_requested:
                task_queue.ack(task["task_id"])
                continue
            mismatch = assembly_worker_mismatch(
                payload.video_engine,
                task.get("engine_snapshot") or job.video_engine_snapshot)
            if mismatch:
                # The node cannot prove it runs the exact configuration this attempt was
                # dispatched with, so the task stays queued instead of being rendered by a
                # runtime nobody approved (Issue #122 P5/P7).
                store.event(
                    job, f"ASSEMBLY {task['task_id']} HELD: {payload.node_name} "
                    f"cannot run the dispatched engine ({mismatch})")
                held_tasks.append(task["task_id"])
                continue
            hold = engine_release_hold(
                task.get("engine_snapshot") or job.video_engine_snapshot)
            if hold:
                # A cancelled attempt on this very runtime was never proven released: the
                # pinned runtime keeps rendering until it observes the abort, so the lease
                # is not handed to another render on the strength of a logical cancel
                # (Issue #122 P6/§5).
                store.event(
                    job, f"ASSEMBLY {task['task_id']} HELD: the runtime of "
                    f"{hold.get('job_id')} has an unconfirmed release "
                    f"({hold.get('submit_key')}: {hold.get('reason')})")
                held_tasks.append(task["task_id"])
                continue
            worker = _select_worker(
                [worker_data],
                job,
                task_type="assembly",
                min_vram_mb=0)
            if not worker:
                held_tasks.append(task["task_id"])
                continue
            for held_task_id in held_tasks:
                task_queue.release(held_task_id)
            held_tasks.clear()
            # A requeued attempt (expired lease) is still bound to its previous node;
            # that binding is the fence, so it is dropped before the new owner takes the
            # attempt and a late renew/result of the old node is refused (#395 M3, §5).
            for previous in store.workers.values():
                if (previous.get("node_name") != payload.node_name
                        and previous.get("current_task") == task["task_id"]):
                    previous.update(current_task=None, current_job=None,
                                    status=previous.get("desired_state") or "READY")
                    store.save_worker(previous)
                    store.event(job, f"ASSEMBLY {task['task_id']} LEASE OF "
                                     f"{previous.get('node_name')} EXPIRED; REASSIGNED")
            store.workers[payload.node_name] = worker_data
            store.workers[payload.node_name]["status"] = "BUSY"
            store.workers[payload.node_name]["current_job"] = job.job_id
            store.workers[payload.node_name]["current_task"] = task["task_id"]
            store.save_worker(store.workers[payload.node_name])
            job.assigned_worker = payload.node_name
            store.event(
                job, f"{payload.node_name} ASSEMBLY CLAIMED {task['task_id']} FOR v{version}")
            store.repository.record_task(task, "CLAIMED", payload.node_name)
            return {"task": task}
        scene = _scene_for_task(job, task.get("task_id", ""))
        is_tts = job.status == JobStatus.TTS_GENERATING and task.get(
            "task") == "voice"
        effective_task_type = task.get("task") if is_tts else job.task_type
        effective_min_vram = task.get("min_vram_mb") if is_tts else None
        voice_requirements = None
        if is_tts:
            # Issue i.0.0.1.5: readiness must be reconciled end-to-end on the
            # full character voice config (voice/provider/model/language), not
            # just voice+model, so an unsupported combination is refused at
            # dispatch time instead of failing at synthesis time.
            voice_requirements = {
                "voice": task.get("voice"),
                "model": task.get("model"),
                "provider": task.get("provider"),
                "language": task.get("language"),
                "speed": task.get("speed"),
            }
        worker = _select_worker(
            [worker_data],
            job,
            task_type=effective_task_type,
            min_vram_mb=effective_min_vram,
            voice_requirements=voice_requirements) if job else None
        if job and worker:
            for held_task_id in held_tasks:
                task_queue.release(held_task_id)
            held_tasks.clear()
        else:
            held_tasks.append(task["task_id"])
            continue
        if not (
            (job.status == JobStatus.ASSET_GENERATION and scene) or (
                is_tts and scene)):
            task_queue.ack(task["task_id"])
            continue
        if is_tts:
            scene.assigned_worker = payload.node_name
            store.workers[payload.node_name] = worker_data
            store.workers[payload.node_name]["status"] = "BUSY"
            store.workers[payload.node_name]["current_job"] = job.job_id
            store.workers[payload.node_name]["current_task"] = task["task_id"]
            store.save_worker(store.workers[payload.node_name])
            store.event(
                job, f"{payload.node_name} TTS ASSIGNED TO {scene.scene_id}")
            store.repository.record_task(task, "CLAIMED", payload.node_name)
            return {"task": task}
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


@router.post("/api/tasks/renew")
def renew_task(payload: TaskRenew, request: Request):
    if not _valid_worker_request(payload.node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    text_job = next((job for job in store.jobs.values() if payload.task_id in {
                    job.script_task_id, job.storyboard_task_id}), None)
    if text_job is not None:
        owned, _ = _text_task_lease(
            text_job, payload.task_id, payload.node_name)
        if not owned:
            raise HTTPException(
                409, "Task is no longer assigned to this worker")
        return {"renewed": task_queue.renew(payload.task_id)}
    job = next((job for job in store.jobs.values()
               if payload.task_id in job.active_task_ids), None)
    if job is not None and _scene_for_task(job, payload.task_id):
        scene = _scene_for_task(job, payload.task_id)
        if scene.assigned_worker != payload.node_name:
            raise HTTPException(
                409, "Task is no longer assigned to this worker")
        return {"renewed": task_queue.renew(payload.task_id)}
    # Issue #122 P6: an assembly attempt is long-running, so its lease must be
    # renewable too. Ownership is fenced on the Job mapping and the worker
    # record.
    assembly_job = next((job for job in store.jobs.values()
                         if payload.task_id in job.assembly_task_ids), None)
    if assembly_job is not None:
        worker = store.workers.get(payload.node_name)
        if not worker or worker.get("current_task") != payload.task_id \
                or worker.get("current_job") != assembly_job.job_id:
            raise HTTPException(
                409, "Task is no longer assigned to this worker")
        return {"renewed": task_queue.renew(payload.task_id)}
    raise HTTPException(409, "Task is no longer assigned to this worker")


@router.get("/api/tasks/cancellations/{node_name}")
def task_cancellations(node_name: str, request: Request):
    if not _valid_worker_request(node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    return task_queue.pop_cancellations(node_name)


def _approved_input_references(snapshot: dict) -> set[str]:
    """Every Job-relative input the attempt's snapshot approved."""
    references = set()
    for entry in snapshot.get("materials") or []:
        reference = str((entry or {}).get("reference") or "")
        if reference:
            references.add(reference)
    for entry in (snapshot.get("inputs") or {}).values():
        reference = str((entry or {}).get("reference") or "")
        if reference:
            references.add(reference)
    return references


@router.get("/api/tasks/publisher-readiness")
def publisher_readiness(request: Request):
    """Channel readiness for a Publisher Node; never carries secret values."""
    from adapters.publisher import PUBLISHERS
    node_name = request.headers.get("x-vertep-node", "")
    if not node_name or not _valid_worker_request(node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    from ..publishing_settings import publishing_settings
    channels = {name: publisher.readiness() for name, publisher in PUBLISHERS.items()}
    # The target is a setting, not a secret: a node holding its own Telegram token
    # checks its right to post in this exact channel.
    return {"channels": channels, "targets": {"telegram": publishing_settings()["telegram_channel_id"]}}


def _publish_secret_scope(task_id: str, request: Request) -> tuple[set[str], str]:
    """Names this node may use: only the channel of the publish task it runs now."""
    from ..first_run import INTEGRATION_SECRET_NAMES
    node_name = request.headers.get("x-vertep-node", "")
    if not node_name or not _valid_worker_request(node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    worker = store.workers.get(node_name) or {}
    job = store.jobs.get(worker.get("current_job") or "")
    channel = job.publish_task_ids.get(task_id) if job else None
    if worker.get("current_task") != task_id or not channel:
        raise HTTPException(409, "Task is no longer owned by this worker")
    return {name for name in INTEGRATION_SECRET_NAMES if name.startswith(f"{channel}_")}, channel


@router.get("/api/tasks/{task_id}/secrets")
def task_secrets(task_id: str, request: Request):
    from ..first_run import ensure_secret_store
    names, channel = _publish_secret_scope(task_id, request)
    stored = ensure_secret_store()
    oauth = stored.get("publisher_oauth", {})
    return {"secrets": {name: stored[name] for name in names if stored.get(name)},
            "oauth": {key: value for key, value in oauth.items() if key.lower() in names}}


@router.put("/api/tasks/{task_id}/secrets")
def save_task_secrets(task_id: str, payload: dict, request: Request):
    """Keep tokens a node refreshed during publication in the one store."""
    from ..first_run import _secret_lock, _write_encrypted_secrets, ensure_secret_store
    names, _ = _publish_secret_scope(task_id, request)
    secrets, oauth = payload.get("secrets") or {}, payload.get("oauth") or {}
    if not set(secrets) <= names or not {key.lower() for key in oauth} <= names:
        raise HTTPException(403, "Secret is outside this task's channel")
    with _secret_lock:
        stored = ensure_secret_store()
        stored.update(secrets)
        stored.setdefault("publisher_oauth", {}).update(oauth)
        _write_encrypted_secrets(stored)
    return {"saved": True}


@router.get("/api/tasks/{task_id}/character-references/{filename}")
def task_character_reference(task_id: str, filename: str, request: Request):
    """Serve a character reference to the node currently running that character's task."""
    from ..character_references import reference_path
    node_name = request.headers.get("x-vertep-node", "")
    if not node_name or not _valid_worker_request(node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    worker = store.workers.get(node_name) or {}
    job = store.jobs.get(worker.get("current_job") or "")
    if worker.get("current_task") != task_id or job is None:
        raise HTTPException(409, "Task is no longer owned by this worker")
    path = reference_path(job.character_id, filename)
    if path is None:
        raise HTTPException(404, "Reference not found")
    return FileResponse(path)


@router.get("/api/tasks/{task_id}/inputs/{reference:path}")
def task_input(task_id: str, reference: str, request: Request):
    """Serve one approved input of an assembly attempt to the node rendering it (P3).

    Issue #122 P3 asks for approved inputs to reach a Worker that does not share CORE's
    storage root. The dispatch carries Job-relative references only, so a node on another
    host pulls the approved bytes from here instead of reading a path of this filesystem.

    Access is the same worker authentication as claim/renew **plus** ownership of the
    attempt: only the node currently rendering it may read its inputs, and only the inputs
    its own approved snapshot pins — an arbitrary path, or another node's Job, is refused.
    """
    node_name = request.headers.get("x-vertep-node", "")
    if not node_name or not _valid_worker_request(node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    job = next((item for item in store.jobs.values()
               if task_id in item.assembly_task_ids), None)
    publish_job = next((item for item in store.jobs.values()
                       if task_id in item.publish_task_ids), None)
    if publish_job is not None:
        worker = store.workers.get(node_name)
        if (not worker or worker.get("current_task") != task_id
                or worker.get("current_job") != publish_job.job_id
                or not task_queue.inflight_active(task_id)):
            raise HTTPException(
                409, "Publish input is no longer owned by this worker")
        channel = publish_job.publish_task_ids[task_id]
        intent = publish_job.publish_intent.get(channel) or {}
        version = next((v for v in publish_job.video_versions if v.version == intent.get(
            "video_version") and v.approved), None)
        if version is None or publish_job.active_video_version != version.version:
            raise HTTPException(
                409, "Published input version is no longer approved")
        source = Path(publish_job.output_path or "")
        if reference != source.name or not source.is_file():
            raise HTTPException(404, "Approved publish input is unavailable")
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != intent.get("video_sha256") or len(
                data) != intent.get("video_size"):
            raise HTTPException(409, "Approved publish input is corrupt")
        return Response(
            data,
            media_type="video/mp4",
            headers={
                "x-vertep-sha256": digest})
    if job is None:
        raise HTTPException(404, "Task is not an assembly attempt")
    if job.assembly_task_id != task_id or job.assembly_cancel_requested:
        raise HTTPException(409, "Attempt is no longer running")
    worker = store.workers.get(node_name)
    if not worker or worker.get("current_task") != task_id or worker.get(
            "current_job") != job.job_id:
        raise HTTPException(409, "Task is not owned by this worker")
    snapshot = job.video_engine_snapshot or {}
    if not _approved_input_references(snapshot):
        raise HTTPException(409, "Attempt carries no approved inputs")
    if reference not in _approved_input_references(snapshot):
        raise HTTPException(403, "Input is not approved for this attempt")
    job_root = (Path(store.root) / job.job_id).resolve()
    candidate = (job_root / reference).resolve()
    if not candidate.is_relative_to(job_root):
        raise HTTPException(403, "Input escapes the Job directory")
    if not candidate.is_file():
        raise HTTPException(404, "Input is not available for delivery")
    data = candidate.read_bytes()
    return Response(
        content=data,
        media_type=_approved_input_media_type(candidate.suffix),
        headers={
            "content-length": str(len(data)),
            "x-vertep-sha256": hashlib.sha256(data).hexdigest(),
        },
    )


def _approved_input_media_type(suffix: str) -> str:
    return {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".srt": "application/x-subrip",
        ".vtt": "text/vtt",
        ".ass": "text/x-ssa",
    }.get(suffix.lower(), "application/octet-stream")


@router.get("/api/tasks/dead-letter")
def dead_letter_tasks():
    return task_queue.dead_letters()


@router.get("/api/tasks/queue")
def queue_tasks(
    limit: int = Query(
            default=50,
            ge=1,
            le=500),
        offset: int = Query(
            default=0,
            ge=0),
        include_dead_letter: bool = True,
        refresh: int | None = Query(
            default=None,
            ge=0)):
    def _summary(task: dict) -> dict:
        job = store.jobs.get(task.get("job_id"))
        return {
            "task_id": task.get("task_id", ""),
            "job_id": task.get("job_id", ""),
            "task": task.get("task", ""),
            "priority": task.get("priority", 5),
            "scene_id": task.get("scene_id", ""),
            "enqueued_at": task.get("enqueued_at"),
            "workflow": task.get("workflow", ""),
            # The Queue is an operational view of the Job lifecycle: each task
            # carries the owning Job's durable status so the UI never has to
            # reconcile a separate, contradictory queue model.
            "job_status": job.status.value if job else None,
            "kind": task.get("kind"),
            "error": _redact_payload(task.get("error")),
            "failed_at": task.get("failed_at"),
        }

    def _scheduled() -> list[dict]:
        rows = []
        for job in store.jobs.values():
            if job.status == JobStatus.NEW and job.scheduled_for and not _job_is_due(
                    job):
                rows.append({"job_id": job.job_id,
                             "topic": job.topic,
                             "job_status": job.status.value,
                             "task_type": job.task_type,
                             "priority": job.priority,
                             "scheduled_for": job.scheduled_for,
                             "required_tags": list(job.required_tags or [])})
        return sorted(
            rows,
            key=lambda row: (
                row["scheduled_for"],
                row["job_id"]))

    ready = [_summary(t) for t in task_queue.ready_tasks()]
    inflight = [_summary(t) for t in task_queue.inflight_tasks()]
    scheduled = _scheduled()
    dead_letter_all = [_summary(t) for t in task_queue.dead_letters()]
    dead_letter = dead_letter_all[offset:offset +
                                  limit] if include_dead_letter else []
    generation = task_queue.generation()
    return {
        "scheduled": scheduled,
        "ready": ready,
        "inflight": inflight,
        "dead_letter": dead_letter,
        "total": {
            "scheduled": len(scheduled),
            "ready": len(ready),
            "inflight": len(inflight),
            "dead_letter": len(dead_letter_all),
        },
        "pagination": {
            "offset": offset,
            "limit": limit,
            "dead_letter_total": len(dead_letter_all)},
        "generation": generation,
        "unchanged": refresh is not None and refresh == generation,
        "refreshed_at": utc_now(),
    }


@router.post("/api/tasks/dead-letter/{task_id}/retry")
def retry_dead_letter_task(task_id: str):
    # Retry conflict semantics: never re-enqueue a task that is still being
    # processed (inflight lease) or already present on the ready queue.
    if task_queue.has_task(task_id):
        raise HTTPException(409, "Task already queued or inflight")
    queued = task_queue.requeue_dead_letter(task_id)
    if not queued:
        raise HTTPException(404, "Dead-letter task not found")
    job = store.jobs.get(queued.get("job_id"))
    scene_id = queued.get("scene_id")
    scene = next(
        (item for item in job.scenes if item.scene_id == scene_id),
        None) if job else None
    if not job or not scene:
        task_queue.discard(queued["task_id"])
        raise HTTPException(409, "Job or scene no longer exists")
    scene.status = StageStatus.FAILED
    scene.task_id = queued["task_id"]
    job.active_task_ids[queued["task_id"]] = scene.scene_id
    job.active_task_id = queued["task_id"]
    if job.stages[StageName.ASSETS.value].status == StageStatus.FAILED:
        transition_stage(job, StageName.ASSETS, StageStatus.RUNNING)
    store.update(job, JobStatus.ASSET_GENERATION,
                 f"DEAD-LETTER TASK {task_id} REQUEUED")
    store.repository.record_task(queued, "QUEUED")
    return queued


@router.post("/api/tasks/result")
@_serialize_job_result
def task_result(result: TaskResult, request: Request):
    if not _valid_worker_request(result.node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    job = store.jobs.get(result.job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    _report_gpu_outcome(job, result)
    if result.task_id in job.image_storyboard_task_ids:
        return image_storyboard_result(job, result)
    if job.storyboard_task_id == result.task_id:
        return storyboard_result(job, result)
    if job.script_task_id == result.task_id:
        return script_result(job, result)
    if result.task_id in job.assembly_task_ids:
        return assembly_result(job, result)
    if result.task_id in job.publish_task_ids:
        return publish_result(job, result)
    is_tts = result.task_id in job.tts_active_task_ids
    if is_tts:
        return tts_result(job, result)
    return scene_result(job, result)
