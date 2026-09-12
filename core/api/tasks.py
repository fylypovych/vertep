"""Task claim/result routes for the Vertep CORE web application.

Worker-facing task lifecycle endpoints (owning a task, renewing a lease,
cancellations, dead-letter queue and result submission). Extracted from
``core/app.py``.
"""
import base64
import binascii
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..artifacts import register_artifact
from ..file_validation import validate_signature
from ..models import (JobStatus, StageName, StageStatus, TaskClaim, TaskRenew,
                      TaskResult, utc_now)
from ..orchestration import (all_scenes_ready, cancel_scene, fail_scene,
                             finish_scene, start_scene, transition_stage)
from ..security import _valid_worker_request
from ..state import executor, store, task_queue
from ..system_state import dispatch_allowed, get_system_state
from .job_helpers import (_enqueue_job_task, _finalize_and_notify,
                          _character_voice_enabled,
                          _dispatch_tts, _has_voice_worker, _ordered_scene_files, _pending_voice_scenes,
                          _persist_tts_contract,
                          _scene_for_task, _select_worker, _serialize_job_result,
                          _task_for, _tts_task_for)

router = APIRouter()


@router.post("/api/tasks/claim")
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
        if task.get("kind") == "image_storyboard":
            sb_version = task.get("storyboard_version")
            if job.status != JobStatus.STORYBOARD_PENDING_APPROVAL or job.active_storyboard_version != sb_version:
                task_queue.ack(task["task_id"])
                continue
            worker = _select_worker([worker_data], job, task_type="image", min_vram_mb=task.get("min_vram_mb")) if job else None
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
            store.event(job, f"{payload.node_name} IMAGE STORYBOARD CLAIMED {task.get('scene_id')}")
            store.repository.record_task(task, "CLAIMED", payload.node_name)
            return {"task": task}
        if task.get("task") == "storyboard":
            if job.status != JobStatus.STORYBOARD_QUEUED or job.storyboard_task_id != task.get("task_id"):
                task_queue.ack(task["task_id"])
                continue
            worker = _select_worker([worker_data], job, task_type="text") if job else None
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
            store.event(job, f"{payload.node_name} STORYBOARD CLAIMED {task['task_id']}")
            store.repository.record_task(task, "CLAIMED", payload.node_name)
            return {"task": task}
        scene = _scene_for_task(job, task.get("task_id", ""))
        is_tts = job.status == JobStatus.TTS_GENERATING and task.get("task") == "voice"
        effective_task_type = task.get("task") if is_tts else job.task_type
        effective_min_vram = task.get("min_vram_mb") if is_tts else None
        voice_requirements = {"voice": task.get("voice"), "model": task.get("model")} if is_tts else None
        worker = _select_worker([worker_data], job, task_type=effective_task_type, min_vram_mb=effective_min_vram,
                                voice_requirements=voice_requirements) if job else None
        if job and worker:
            for held_task_id in held_tasks:
                task_queue.release(held_task_id)
            held_tasks.clear()
        else:
            held_tasks.append(task["task_id"])
            continue
        if not ((job.status == JobStatus.ASSET_GENERATION and scene) or (is_tts and scene)):
            task_queue.ack(task["task_id"])
            continue
        if is_tts:
            scene.assigned_worker = payload.node_name
            store.workers[payload.node_name] = worker_data
            store.workers[payload.node_name]["status"] = "BUSY"
            store.workers[payload.node_name]["current_job"] = job.job_id
            store.workers[payload.node_name]["current_task"] = task["task_id"]
            store.save_worker(store.workers[payload.node_name])
            store.event(job, f"{payload.node_name} TTS ASSIGNED TO {scene.scene_id}")
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
    job = next((job for job in store.jobs.values() if payload.task_id in job.active_task_ids), None)
    scene = _scene_for_task(job, payload.task_id) if job else None
    if not job or not scene or scene.assigned_worker != payload.node_name:
        raise HTTPException(409, "Task is no longer assigned to this worker")
    return {"renewed": task_queue.renew(payload.task_id)}


@router.get("/api/tasks/cancellations/{node_name}")
def task_cancellations(node_name: str, request: Request):
    if not _valid_worker_request(node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    return task_queue.pop_cancellations(node_name)


@router.get("/api/tasks/dead-letter")
def dead_letter_tasks():
    return task_queue.dead_letters()


@router.get("/api/tasks/queue")
def queue_tasks():
    def _summary(task: dict) -> dict:
        return {
            "task_id": task.get("task_id", ""),
            "job_id": task.get("job_id", ""),
            "task": task.get("task", ""),
            "priority": task.get("priority", 5),
            "scene_id": task.get("scene_id", ""),
            "enqueued_at": task.get("enqueued_at"),
            "workflow": task.get("workflow", ""),
        }
    return {"ready": [_summary(t) for t in task_queue.ready_tasks()],
            "inflight": [_summary(t) for t in task_queue.inflight_tasks()]}


@router.post("/api/tasks/dead-letter/{task_id}/retry")
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


@router.post("/api/tasks/result")
@_serialize_job_result
def task_result(result: TaskResult, request: Request):
    if not _valid_worker_request(result.node_name, request):
        raise HTTPException(401, "Token is not valid for this worker")
    job = store.jobs.get(result.job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if result.task_id in job.image_storyboard_task_ids:
        from ..image_storyboard import handle_image_result
        worker = store.workers.get(result.node_name)
        if worker:
            desired_status = worker.get("desired_state")
            next_status = desired_status if desired_status in {"DRAINING", "QUARANTINED"} else "READY"
            worker.update({"status": next_status, "current_job": None, "current_task": None, "last_seen": utc_now()})
            store.save_worker(worker)
        try:
            handle_image_result(store, job, result.task_id, result.success, result.image_base64, result.error, result.artifacts or result.images)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        from ..state import task_queue as _tq2
        _tq2.ack(result.task_id)
        store.repository.record_task({"job_id": job.job_id, "task_id": result.task_id, "task": "image", "kind": "image_storyboard"}, "COMPLETED" if result.success else "FAILED", result.node_name, result.error)
        if not result.success:
            _tq2.dead_letter({"job_id": job.job_id, "task_id": result.task_id, "kind": "image_storyboard"}, result.error)
        return job
    if job.storyboard_task_id == result.task_id:
        from ..storyboard import StoryboardService
        worker = store.workers.get(result.node_name)
        if worker:
            desired_status = worker.get("desired_state")
            next_status = desired_status if desired_status in {"DRAINING", "QUARANTINED"} else "READY"
            worker.update({"status": next_status, "current_job": None, "current_task": None, "last_seen": utc_now()})
            store.save_worker(worker)
        try:
            StoryboardService(store).handle_result(job.job_id, result.task_id, result.success, result.artifacts, result.error)
        except (ValueError, RuntimeError) as error:
            raise HTTPException(400, str(error)) from error
        from ..state import task_queue as _tq2
        _tq2.ack(result.task_id)
        store.repository.record_task({"job_id": job.job_id, "task_id": result.task_id, "task": "storyboard"}, "COMPLETED" if result.success else "FAILED", result.node_name, result.error)
        if not result.success:
            _tq2.dead_letter({"job_id": job.job_id, "task_id": result.task_id, "task": "storyboard"}, result.error)
        return job
    is_tts = result.task_id in job.tts_active_task_ids
    if is_tts:
        if result.task_id in job.completed_task_ids:
            return job
        scene = _scene_for_task(job, result.task_id)
        if not scene or scene.assigned_worker != result.node_name:
            raise HTTPException(409, "TTS result does not match the active task")
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
            store.repository.record_task(_tts_task_for(job, scene) | {"task_id": result.task_id}, "FAILED", result.node_name, result.error)
            job.tts_active_task_ids.pop(result.task_id, None)
            scene.assigned_worker = None
            if len(scene.attempts) < job.max_retries:
                store.event(job, f"{scene.scene_id} TTS FAILED, RETRY {len(scene.attempts)}/{job.max_retries}: {result.error or 'unknown error'}")
                delay = float(os.getenv("RETRY_BACKOFF_BASE", "2")) * (2 ** max(0, job.retries - 1))
                _enqueue_tts_task(job, scene)
                return job
            transition_stage(job, StageName.TTS, StageStatus.FAILED, result.error or "unknown error")
            return store.update(job, JobStatus.FAILED, f"TTS FAILED: {result.error or 'unknown error'}")
        artifacts = result.artifacts or []
        if not artifacts:
            raise HTTPException(400, "Successful TTS result has no artifact")
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
            max_artifact_bytes = int(os.getenv("MAX_ARTIFACT_BYTES", "26214400"))
            if total_size > max_artifact_bytes:
                raise HTTPException(413, "Artifacts are too large")
            supplied_name = Path(artifact.get("filename", f"{scene.scene_id}.wav")).name
            suffix = Path(supplied_name).suffix.lower()
            allowed_suffixes = {".wav", ".ogg", ".mp3", ".aac", ".m4a"}
            if suffix not in allowed_suffixes:
                raise HTTPException(400, f"Unsupported audio artifact type: {suffix}")
            try:
                validate_signature(data, suffix)
            except ValueError as error:
                raise HTTPException(400, str(error)) from error
            filename = f"voice-{scene.scene_id}{suffix}" if len(artifacts) == 1 else f"voice-{scene.scene_id}-{artifact_index:03d}{suffix}"
            audio_path = store.root / job.job_id / "audio" / filename
            prepared_images.append((audio_path, data, artifact.get("contract")))
        temporary_images = []
        try:
            for audio_path, data, _contract in prepared_images:
                temporary = audio_path.with_name(f".{audio_path.name}.{result.task_id[:8]}.part")
                temporary.write_bytes(data)
                temporary_images.append((temporary, audio_path))
            for temporary, audio_path in temporary_images:
                temporary.replace(audio_path)
        except OSError as error:
            for temporary, _ in temporary_images:
                temporary.unlink(missing_ok=True)
            raise HTTPException(500, "Could not persist audio artifacts") from error
        for audio_path, data, contract in prepared_images:
            saved_artifacts.append(register_artifact(job, store.root, audio_path, "audio",
                                                     scene_id=scene.scene_id, task_id=result.task_id,
                                                     node_name=result.node_name, workflow=job.workflow))
            try:
                saved_artifacts.extend(_persist_tts_contract(store, job, scene, result,
                                                             audio_path, data, contract))
            except (ValueError, OSError) as error:
                raise HTTPException(400, f"Invalid audio contract: {error}") from error
        task_queue.ack(result.task_id)
        store.repository.record_task(_tts_task_for(job, scene) | {"task_id": result.task_id}, "COMPLETED", result.node_name)
        job.completed_task_ids.append(result.task_id)
        job.tts_active_task_ids.pop(result.task_id, None)
        if not job.tts_active_task_ids:
            transition_stage(job, StageName.TTS, StageStatus.READY)
            store.transition(job, JobStatus.TTS_READY, "TTS COMPLETED")
            if job.task_type in {"image", "video"}:
                ordered_images = _ordered_scene_files(job)
                if ordered_images:
                    executor.submit(_finalize_and_notify, job, ordered_images)
                else:
                    transition_stage(job, StageName.ASSEMBLY, StageStatus.FAILED, "Missing images for assembly")
                    store.update(job, JobStatus.FAILED, "MISSING IMAGES FOR ASSEMBLY")
            elif job.task_type == "voice":
                terminal = JobStatus.READY
                return store.update(job, terminal, "VOICE TASK COMPLETED")
        return store.event(job, f"TTS RESULT FOR {scene.scene_id} RECEIVED FROM {result.node_name}")
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
    if not result.success:
        task_queue.ack(result.task_id)
        store.repository.record_task(_task_for(job, scene) | {"task_id": result.task_id}, "FAILED", result.node_name, result.error)
        fail_scene(scene, result.error or "unknown error")
        job.active_task_ids.pop(result.task_id, None)
        scene.assigned_worker = None
        if len(scene.attempts) < job.max_retries:
            store.event(job, f"{scene.scene_id} FAILED, RETRY {len(scene.attempts)}/{job.max_retries}: {result.error or 'unknown error'}")
            delay = float(os.getenv("RETRY_BACKOFF_BASE", "2")) * (2 ** max(0, job.retries - 1))
            _enqueue_job_task(job, scene, new_attempt=True)
            return job
        transition_stage(job, StageName.ASSETS, StageStatus.FAILED, result.error or "unknown error")
        return store.update(job, JobStatus.FAILED, f"ASSET GENERATION FAILED: {result.error or 'unknown error'}")
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
        if _pending_voice_scenes(job) and _character_voice_enabled(job) and _has_voice_worker(store):
            store.transition(job, JobStatus.TTS_GENERATING, "TTS GENERATION STARTED")
            _dispatch_tts(store, job)
            return job
        ordered_images = _ordered_scene_files(job)
        executor.submit(_finalize_and_notify, job, ordered_images)
    return store.event(job, f"RESULT FOR {scene.scene_id} RECEIVED FROM {result.node_name}")