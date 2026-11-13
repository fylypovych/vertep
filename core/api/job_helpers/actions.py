"""Job lifecycle actions (pause, resume, cancel) and their queue effects."""

from fastapi import HTTPException

from ...models import (
    JobStatus,
    StageName,
    StageStatus,
)
from ...orchestration import transition_stage
from ...state import (
    store,
    task_queue,
)
from .assembly import (
    _clear_assembly_staging,
    _cancel_and_release_state,
    record_assembly_release,
)
from .scene_tasks import _task_for
from .tts import _tts_task_for


def _job_action(job_id: str, status: JobStatus, event: str):
    job = store.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
        task_status = "CANCELLED" if status == JobStatus.CANCELLED else "PAUSED"
        for task_id, scene_id in list(job.active_task_ids.items()):
            scene = next(
                (item for item in job.scenes if item.scene_id == scene_id), None)
            worker = scene.assigned_worker if scene else None
            if worker:
                task_queue.request_cancel(worker, task_id)
            task_queue.discard(task_id)
            store.repository.record_task(
                _task_for(
                    job,
                    scene) | {
                    "task_id": task_id},
                task_status,
                worker)
            if scene:
                scene.status = StageStatus.CANCELLED if status == JobStatus.CANCELLED else StageStatus.PAUSED
                scene.task_id = None
                scene.assigned_worker = None
        for task_id, scene_id in list(job.tts_active_task_ids.items()):
            scene = next(
                (item for item in job.scenes if item.scene_id == scene_id), None)
            worker = scene.assigned_worker if scene else None
            if worker:
                task_queue.request_cancel(worker, task_id)
            task_queue.discard(task_id)
            store.repository.record_task(
                _tts_task_for(
                    job,
                    scene) | {
                    "task_id": task_id},
                task_status,
                worker)
            if scene:
                scene.task_id = None
        if job.storyboard_task_id:
            task_id = job.storyboard_task_id
            worker = job.assigned_worker
            if worker:
                task_queue.request_cancel(worker, task_id)
            task_queue.discard(task_id)
            store.repository.record_task(
                {"job_id": job.job_id, "task_id": task_id, "task": "storyboard"}, task_status, worker)
            job.storyboard_task_id = None
        if job.script_task_id:
            task_id = job.script_task_id
            worker = job.assigned_worker
            if worker:
                task_queue.request_cancel(worker, task_id)
            task_queue.discard(task_id)
            store.repository.record_task(
                {"job_id": job.job_id, "task_id": task_id, "task": "script"}, task_status, worker)
            job.script_task_id = None
        # Issue #122 P5/P6: an in-flight assembly attempt is fenced the same way.
        # The logical cancellation is immediate; the Worker is told to stop the
        # upstream work and a late result is discarded, because a running upstream
        # task cannot be confirmed stopped (the pinned runtime answers DELETE 409
        # while it is busy, §9.8).
        #
        # What the cancel cannot prove is recorded instead of assumed: each cancelled
        # attempt gets its real release state, so a runtime that may still be
        # rendering is kept out of new attempts until evidence frees it (§5).
        if job.assembly_task_ids:
            snapshot = job.video_engine_snapshot or {}
            for task_id, version in list(job.assembly_task_ids.items()):
                worker = job.assigned_worker
                if worker:
                    task_queue.request_cancel(worker, task_id)
                task_queue.discard(task_id)
                store.repository.record_task(
                    {"job_id": job.job_id, "task_id": task_id, "task": "assembly",
                     "version": version}, task_status, worker)
                submit_key = (snapshot.get("submit_key")
                              or job.assembly_submit_keys.get(str(version)))
                if submit_key:
                    state, reason = _cancel_and_release_state(
                        job, submit_key=submit_key)
                    record_assembly_release(
                        job, submit_key=submit_key, snapshot=snapshot,
                        state=state, reason=reason, task_id=task_id,
                    )
            job.assembly_task_ids.clear()
            job.assembly_task_id = None
            job.assembly_cancel_requested = True
            _clear_assembly_staging(store.root, job.job_id)
            if job.stages and job.stages.get(StageName.ASSEMBLY.value) \
                    and job.stages[StageName.ASSEMBLY.value].status == StageStatus.RUNNING:
                transition_stage(
                    job,
                    StageName.ASSEMBLY,
                    StageStatus.CANCELLED if status == JobStatus.CANCELLED else StageStatus.PAUSED)
        for task_id, channel in list(job.publish_task_ids.items()):
            worker = job.assigned_worker
            if worker:
                task_queue.request_cancel(worker, task_id)
            task_queue.discard(task_id)
            store.repository.record_task(
                {
                    "job_id": job.job_id,
                    "task_id": task_id,
                    "task": "publish",
                    "channel": channel},
                task_status,
                worker)
        job.publish_task_ids.clear()
        job.active_task_ids.clear()
        job.tts_active_task_ids.clear()
        job.active_task_id = None
        job.assigned_worker = None
        if job.stages and job.stages[StageName.ASSETS.value].status == StageStatus.RUNNING:
            transition_stage(
                job,
                StageName.ASSETS,
                StageStatus.CANCELLED if status == JobStatus.CANCELLED else StageStatus.PAUSED)
        if job.stages and job.stages[StageName.TTS.value].status == StageStatus.RUNNING:
            transition_stage(
                job,
                StageName.TTS,
                StageStatus.CANCELLED if status == JobStatus.CANCELLED else StageStatus.PAUSED)
        if job.stages and job.stages.get(
                StageName.SCRIPT.value) and job.stages[StageName.SCRIPT.value].status == StageStatus.RUNNING:
            transition_stage(
                job,
                StageName.SCRIPT,
                StageStatus.CANCELLED if status == JobStatus.CANCELLED else StageStatus.PAUSED)
    elif status == JobStatus.NEW:
        for scene in job.scenes:
            if scene.status == StageStatus.PAUSED:
                scene.status = StageStatus.PENDING
        # Issue i.0.0.1.5: a resumed/retried Job re-dispatches its TTS scenes
        # from scratch, so the per-scene attempt budget is reset with it.  A
        # budget spent before the pause must not keep a legitimately resumed Job
        # permanently blocked.
        job.tts_attempts = {}
        if job.stages:
            if job.stages[StageName.ASSETS.value].status == StageStatus.PAUSED:
                transition_stage(job, StageName.ASSETS, StageStatus.RUNNING)
            if job.stages[StageName.TTS.value].status == StageStatus.PAUSED:
                transition_stage(job, StageName.TTS, StageStatus.RUNNING)
    return store.update(job, status, event)
