"""Shared job/task/worker helper functions for the job-domain routers.

Business helpers (no route registration) used by ``core.api.jobs``,
``core.api.tasks`` and ``core.api.workers``, as well as by ``core.app`` lifespan
and the watchdog.  Extracted from ``core.app.py`` so the job domain can live in
dedicated router modules instead of one large file.
"""
import os
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import httpx
from fastapi import HTTPException, Request

from adapters.telegram import TelegramAdapter

from ..artifacts import register_artifact
from ..dispatcher import available_worker, can_retry
from ..models import JobStatus, StageName, StageStatus, TaskResult
from ..orchestration import (all_scenes_ready, finish_scene, initialize_plan,
                             interrupt_scene, pending_scenes, transition_stage)
from ..pipeline import finalize_job_safe, prepare_job_safe, queue_storyboard
from ..state import result_locks, store, task_queue, workflow_registry


def _serialize_job_result(function):
    @wraps(function)
    def locked(result: TaskResult, request: Request):
        with result_locks[result.job_id]:
            return function(result, request)
    return locked


def _job_is_due(job) -> bool:
    scheduler_url = os.getenv("SCHEDULER_URL", "").rstrip("/")
    if scheduler_url:
        try:
            response = httpx.post(f"{scheduler_url}/due",
                                  json={"jobs": [job.model_dump(mode="json")], "limit": 1}, timeout=3)
            response.raise_for_status()
            return bool(response.json().get("jobs"))
        except (httpx.HTTPError, ValueError):
            return False
    if not job.scheduled_for:
        return True
    try:
        scheduled = datetime.fromisoformat(job.scheduled_for.replace("Z", "+00:00"))
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        return scheduled <= datetime.now(timezone.utc)
    except ValueError:
        return False


def _scene_for_task(job, task_id: str):
    scene_id = job.active_task_ids.get(task_id)
    return next((scene for scene in job.scenes if scene.scene_id == scene_id), None)


def _select_worker(workers: list[dict], job):
    dispatcher_url = os.getenv("DISPATCHER_URL", "").rstrip("/")
    if not dispatcher_url:
        return available_worker(workers, job)
    try:
        response = httpx.post(f"{dispatcher_url}/select",
                              json={"workers": workers, "job": job.model_dump(mode="json")}, timeout=3)
        response.raise_for_status()
        return response.json().get("worker")
    except (httpx.HTTPError, ValueError):
        return None


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
        try:
            last_seen = datetime.fromisoformat(str(worker["last_seen"]))
        except (KeyError, TypeError, ValueError):
            worker["status"] = "OFFLINE"
            continue
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


def _dispatch_assets(store, job) -> None:
    initialize_plan(job)
    if job.stages[StageName.ASSETS.value].status == StageStatus.PENDING:
        transition_stage(job, StageName.ASSETS, StageStatus.RUNNING)
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


def _prepare_and_dispatch(job) -> None:
    from ..pipeline import generate_script
    while True:
        if job.script and job.status == JobStatus.NEW:
            initialize_plan(job)
            assets_stage = job.stages[StageName.ASSETS.value]
            if assets_stage.status in {StageStatus.PENDING, StageStatus.FAILED, StageStatus.PAUSED}:
                transition_stage(job, StageName.ASSETS, StageStatus.RUNNING)
            store.update(job, JobStatus.ASSET_GENERATION, "IMAGE TASK REDISPATCHED")
        elif job.status in {JobStatus.NEW, JobStatus.SCRIPT_QUEUED, JobStatus.SCRIPT_GENERATING, JobStatus.SCRIPT_FAILED}:
            prepare_job_safe(store, job)
        else:
            break
        if job.status != JobStatus.FAILED or not can_retry(job):
            break
        job.retries += 1
        store.update(job, JobStatus.NEW, f"AUTOMATIC RETRY {job.retries}/{job.max_retries}")
    if job.status == JobStatus.SCRIPT_PENDING_APPROVAL:
        _progress(job, "SCRIPT_PENDING_APPROVAL")
        return
    if job.status == JobStatus.SCRIPT_APPROVED:
        queue_storyboard(store, job)
        return
    if job.status == JobStatus.STORYBOARD_APPROVED:
        store.transition(job, JobStatus.ASSET_GENERATION, "ASSET GENERATION STARTED")
        _dispatch_assets(store, job)
        return
    if job.status == JobStatus.ASSET_GENERATION:
        _dispatch_assets(store, job)


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
