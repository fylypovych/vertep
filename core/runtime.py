from __future__ import annotations

from core.api.job_helpers import _job_is_due
from core.api.job_helpers import _prepare_and_dispatch
from core.api.job_helpers import _recover_stale_workers
from core.api.job_helpers import _scene_for_task
from core.configuration import load_character
from core.configuration import read_json
from core.maintenance import cleanup_jobs
from core.maintenance import cleanup_temporary_files
from core.models import JobStatus
from core.models import utc_now
from core.orchestration import interrupt_scene
from core.state import executor
from core.state import store
from core.state import task_queue
from core.system_state import dispatch_allowed
import asyncio
import os
import time
last_maintenance = 0.0

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
    # requeue_expired already returned this ID to ready; a replacement must
    # supersede it rather than leave a second runnable task behind.
    task_queue.discard(task["task_id"])
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
        stale_worker["status"] = stale_worker.get("desired_state") or "READY"
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
        task_queue.dead_letter(task, "storyboard task lease expired (attempts exhausted)")
        store.repository.record_task(task, "FAILED", stale_node,
                                     error="storyboard task lease expired (attempts exhausted)")
        store.update(job, JobStatus.STORYBOARD_FAILED,
                     f"STORYBOARD FAILED after {attempts} attempts: task lease expired repeatedly")
        store.event(job, f"TASK {task['task_id']} LEASE EXPIRED; FAILED ({current}/{attempts})")
    return True


def handle_expired_script_lease(task: dict) -> bool:
    job = store.jobs.get(task.get("job_id"))
    if not job or job.status != JobStatus.SCRIPT_GENERATING or job.script_task_id != task.get("task_id"):
        return False
    task_queue.discard(task["task_id"])
    owner = None
    for worker in store.workers.values():
        if worker.get("current_task") == task["task_id"]:
            owner = worker.get("node_name")
            worker["current_task"] = None
            worker["current_job"] = None
            worker["status"] = worker.get("desired_state") or "READY"
            store.save_worker(worker)
    job.script_task_id = None
    job.active_task_id = None
    job.assigned_worker = None
    job.script_attempt += 1
    store.repository.record_task(task, "RELEASED", owner, error="script task lease expired")
    if job.script_attempt <= job.max_retries:
        from .api.job_helpers import _load_character_prompt, _enqueue_script_task
        prompt, character = _load_character_prompt(job)
        _enqueue_script_task(store, job, prompt, character, revision=job.revision)
    else:
        task_queue.dead_letter(task, "script task lease expired (attempts exhausted)")
        store.repository.record_task(task, "FAILED", owner, error="script task lease expired (attempts exhausted)")
        store.update(job, JobStatus.SCRIPT_FAILED, f"SCRIPT FAILED AFTER {job.script_attempt} ATTEMPTS")
    return True


async def _watchdog() -> None:
    global last_maintenance
    while True:
        await asyncio.sleep(float(os.getenv("WATCHDOG_INTERVAL", "5")))
        from core.workflow.runtime import workflow_runtime
        workflow = workflow_runtime()
        if workflow is not None:
            await asyncio.to_thread(workflow.tick)
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
            voice_job = store.jobs.get(task.get("job_id"))
            if task.get("task") == "publish" and voice_job and task["task_id"] in voice_job.publish_task_ids:
                from .api.job_helpers import _handle_publish_result
                task_queue.discard(task["task_id"])
                channel = voice_job.publish_task_ids[task["task_id"]]
                for worker in store.workers.values():
                    if worker.get("current_task") == task["task_id"]:
                        worker.update(current_task=None, current_job=None,
                                      status=worker.get("desired_state") or "READY")
                        store.save_worker(worker)
                _handle_publish_result(store, voice_job, {"success": False, "task_id": task["task_id"],
                                       "error": "publish task lease expired"}, [], channel)
                store.repository.record_task(task, "RELEASED", error="publish task lease expired")
                continue
            if task.get("task") == "voice" and voice_job and task["task_id"] in voice_job.tts_active_task_ids:
                from .api.job_helpers import _handle_tts_failure
                scene = _scene_for_task(voice_job, task["task_id"])
                if scene:
                    _handle_tts_failure(voice_job, scene, task["task_id"], scene.assigned_worker, "voice task lease expired")
                    continue
            if handle_expired_script_lease(task):
                continue
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

