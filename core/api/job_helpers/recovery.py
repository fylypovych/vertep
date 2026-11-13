"""Recovery of tasks held by stale or lost Workers."""

import os
from datetime import datetime
from datetime import timezone

from ...models import (
    JobStatus,
    StageName,
    StageStatus,
)
from ...orchestration import (
    interrupt_scene,
    transition_stage,
)
from ...state import (
    executor,
    store,
    task_queue,
)
from .common import _scene_for_task
from .assembly import (
    _enqueue_assembly_task,
    _assembly_retry_task,
)
from .tts import _tts_attempts_used
from .script import (
    _enqueue_script_task,
    _load_character_prompt,
)
from .publish import _enqueue_publish_task
from .dispatch import (
    _dispatch_tts,
    _prepare_and_dispatch,
)


def _recover_stale_workers(lost_nodes: set[str] | None = None) -> None:
    now = datetime.now(timezone.utc)
    timeout = int(os.getenv("HEARTBEAT_TIMEOUT", "45"))
    for worker in store.workers.values():
        try:
            last_seen = datetime.fromisoformat(str(worker["last_seen"]))
        except (KeyError, TypeError, ValueError):
            worker["status"] = "OFFLINE"
            continue
        forced = worker.get("node_name") in (lost_nodes or set())
        if not forced and ((now - last_seen).total_seconds()
                           <= timeout or worker.get("status") == "OFFLINE"):
            continue
        worker["status"] = "OFFLINE"
        current_job = worker.get("current_job")
        current_task = worker.get("current_task")
        job = store.jobs.get(current_job) if current_job else None
        scene = _scene_for_task(
            job, current_task) if job and current_task else None
        if job and scene and scene.assigned_worker == worker.get(
                "node_name") and job.status == JobStatus.ASSET_GENERATION:
            task_queue.release(current_task)
            interrupt_scene(
                scene, f"Worker {worker.get('node_name')} heartbeat timed out")
            job.assigned_worker = None
            store.event(
                job, f"{worker.get('node_name')} OFFLINE; TASK {current_task} REQUEUED")
            worker["current_job"] = None
            worker["current_task"] = None
        elif job and job.script_task_id == current_task and job.status in {JobStatus.SCRIPT_QUEUED, JobStatus.SCRIPT_GENERATING}:
            task_queue.discard(current_task)
            job.script_task_id = None
            job.active_task_id = None
            job.assigned_worker = None
            # Issue #197/#163.3: charge the script attempt budget for the lost
            # lease, exactly like ``_handle_script_result`` does for a failed
            # result.  Without this a node that keeps dropping its heartbeat
            # re-dispatches forever without ever exhausting ``max_retries``.
            job.script_attempt = getattr(job, "script_attempt", 0) + 1
            store.repository.record_task(
                {
                    "task_id": current_task,
                    "task": "script",
                    "job_id": job.job_id},
                "RELEASED",
                worker.get("node_name"),
                error="script worker heartbeat timed out")
            if job.script_attempt <= getattr(job, "max_retries", 3):
                system_prompt, character = _load_character_prompt(job)
                _enqueue_script_task(
                    store,
                    job,
                    system_prompt,
                    character,
                    revision=job.revision)
                store.event(
                    job, f"{worker.get('node_name')} OFFLINE; SCRIPT TASK {current_task} REQUEUED")
            else:
                task_queue.dead_letter({"job_id": job.job_id,
                                        "task_id": current_task,
                                        "task": "script"},
                                       "script worker heartbeat timed out after retries")
                store.repository.record_task(
                    {
                        "job_id": job.job_id,
                        "task_id": current_task,
                        "task": "script"},
                    "FAILED",
                    worker.get("node_name"),
                    error="script worker heartbeat timed out after retries")
                store.update(
                    job,
                    JobStatus.SCRIPT_FAILED,
                    f"SCRIPT FAILED AFTER {job.script_attempt} ATTEMPTS")
                store.event(
                    job,
                    f"{worker.get('node_name')} OFFLINE; SCRIPT TASK {current_task} FAILED AFTER RETRIES")
            worker["current_job"] = None
            worker["current_task"] = None
            store.save_worker(worker)
        elif job and current_task in job.publish_task_ids and job.status == JobStatus.PUBLISHING:
            task_queue.discard(current_task)
            channel = job.publish_task_ids.pop(current_task, None)
            job.assigned_worker = None
            _enqueue_publish_task(store, job, channel)
            store.event(
                job, f"PUBLISH TASK {current_task} REQUEUED FOR {channel}")
            worker["current_job"] = None
            worker["current_task"] = None
            store.save_worker(worker)
        elif job and current_task in job.tts_active_task_ids and job.status == JobStatus.TTS_GENERATING:
            # Deterministic worker-loss recovery for Voice tasks: release the
            # lease, mark the scene lost and re-dispatch the remaining TTS so
            # the Job is never silently dropped.  The scene's own TTS attempt
            # budget is charged for the lost lease too (Issue i.0.0.1.5): a node
            # that keeps dropping its lease cannot re-dispatch the same scene
            # forever, and once the budget is spent the scene fails the TTS
            # stage instead of looping.
            scene = next((s for s in job.scenes if s.scene_id ==
                         job.tts_active_task_ids.get(current_task)), None)
            task_queue.discard(current_task)
            job.tts_active_task_ids.pop(current_task, None)
            # Issue #197/#165.2: drop the issued voice-task snapshot for the lost
            # lease so a late result cannot validate against a stale snapshot and
            # the per-worker-loss dict does not grow without bound.
            job.voice_task_snapshot.pop(current_task, None)
            job.assigned_worker = None
            if scene:
                scene.assigned_worker = None
                job.tts_attempts[scene.scene_id] = _tts_attempts_used(
                    job, scene) + 1
                interrupt_scene(
                    scene, f"Worker {worker.get('node_name')} heartbeat timed out")
            # Issue #197/#163.3: record the lost lease in the repository so the
            # audit trail shows the task was released, not silently dropped.
            # The task is re-enqueued (not dead-lettered): the Job stays on the
            # lifecycle path and a healthy Worker re-claims it.  Only terminal
            # exhaustion dead-letters.
            store.repository.record_task(
                {
                    "task_id": current_task,
                    "task": "voice",
                    "job_id": job.job_id},
                "RELEASED",
                worker.get("node_name"),
                error="voice worker heartbeat timed out")
            _dispatch_tts(store, job)
            store.event(
                job, f"{worker.get('node_name')} OFFLINE; TTS TASK {current_task} REQUEUED")
            worker["current_job"] = None
            worker["current_task"] = None
            store.save_worker(worker)
        elif job and job.storyboard_task_id == current_task and job.status in {JobStatus.STORYBOARD_QUEUED,
                                                                               JobStatus.STORYBOARD_GENERATING}:
            # Retire the lost attempt before issuing a replacement ID.
            task_queue.discard(current_task)
            job.storyboard_task_id = None
            job.assigned_worker = None
            job.storyboard_attempt = getattr(job, "storyboard_attempt", 0) + 1
            job.storyboard_attempt_version = (
                job.storyboards[-1].version + 1 if job.storyboards else 1)
            store.repository.record_task(
                {
                    "task_id": current_task,
                    "task": "storyboard",
                    "job_id": job.job_id},
                "RELEASED",
                worker.get("node_name"),
                error="storyboard worker heartbeat timed out")
            attempts = max(
                1, int(
                    os.getenv(
                        "OLLAMA_STORYBOARD_MAX_RETRIES", "3")))
            if job.storyboard_attempt < attempts:
                pinned_revision = getattr(
                    job, "storyboard_active_revision", None)
                job.storyboard_active_revision = pinned_revision
                from ...pipeline import queue_storyboard as _queue_storyboard
                _queue_storyboard(store, job, revision=pinned_revision)
                store.event(
                    job,
                    f"{worker.get('node_name')} OFFLINE; STORYBOARD TASK {current_task} REQUEUED")
            else:
                task_queue.dead_letter(
                    {
                        "job_id": job.job_id,
                        "task_id": current_task,
                        "task": "storyboard"},
                    "storyboard worker heartbeat timed out (attempts exhausted)")
                store.repository.record_task(
                    {
                        "job_id": job.job_id,
                        "task_id": current_task,
                        "task": "storyboard"},
                    "FAILED",
                    worker.get("node_name"),
                    error="storyboard worker heartbeat timed out (attempts exhausted)")
                job.storyboard_task_id = None
                job.active_task_id = None
                store.update(
                    job,
                    JobStatus.STORYBOARD_FAILED,
                    f"STORYBOARD FAILED after {attempts} attempts: task lease expired repeatedly")
                store.event(
                    job,
                    f"{worker.get('node_name')} OFFLINE; STORYBOARD TASK {current_task} FAILED AFTER RETRIES")
            worker["current_job"] = None
            worker["current_task"] = None
            store.save_worker(worker)
        elif job and current_task in job.assembly_task_ids:
            # Issue #122 P6: a Worker that disappears while it owns an assembly
            # attempt releases the lease, the version stays unaccepted and the
            # attempt is re-dispatched with the SAME durable submit key, so the
            # retry reconciles to the same upstream work instead of starting a
            # second render.
            task_queue.release(current_task)
            job.assembly_task_ids.pop(current_task, None)
            if job.assembly_task_id == current_task:
                job.assembly_task_id = None
            job.assembly_attempt += 1
            job.assigned_worker = None
            store.event(
                job, f"{worker.get('node_name')} OFFLINE; ASSEMBLY TASK "
                f"{current_task} REQUEUED")
            retry = _assembly_retry_task(
                job) if job.assembly_attempt <= job.max_retries else None
            if retry is not None:
                _enqueue_assembly_task(job, retry, new_attempt=True)
            else:
                transition_stage(job, StageName.ASSEMBLY, StageStatus.FAILED,
                                 "assembly worker lost")
                store.update(
                    job,
                    JobStatus.FAILED,
                    f"ASSEMBLY FAILED: worker {worker.get('node_name')} was lost")
            worker["current_job"] = None
            worker["current_task"] = None
        elif job and current_task == job.active_task_id and job.status in {
                JobStatus.ASSET_GENERATION, JobStatus.VIDEO_GENERATION, JobStatus.SCRIPT_QUEUED,
                JobStatus.SCRIPT_GENERATING, JobStatus.TTS_GENERATING}:
            # Fallback: worker loss while holding a lease whose scene/task
            # mapping is already cleared (e.g. during a transition).  Release
            # the lease and re-dispatch so the Job is never stranded, then reset
            # the assignment so a healthy worker can re-claim.
            task_queue.release(current_task)
            job.assigned_worker = None
            job.active_task_id = None
            scene = _scene_for_task(job, current_task)
            if scene:
                scene.assigned_worker = None
                interrupt_scene(
                    scene, f"Worker {worker.get('node_name')} heartbeat timed out")
            store.event(
                job, f"{worker.get('node_name')} OFFLINE; TASK {current_task} RELEASED")
            executor.submit(_prepare_and_dispatch, job)
            worker["current_job"] = None
            worker["current_task"] = None
