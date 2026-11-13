"""Job dispatch: assets, TTS, image storyboard gate and finalization."""

import os
from pathlib import Path

import httpx

from adapters.telegram import TelegramAdapter

from ...artifacts import register_artifact
from ...configuration import load_character
from ...dispatcher import can_retry
from ...models import (
    JobStatus,
    SceneRecord,
    StageName,
    StageStatus,
)
from ...orchestration import (
    all_scenes_ready,
    finish_scene,
    initialize_plan,
    pending_scenes,
    transition_stage,
)
from ...pipeline import (
    JobStore,
    finalize_job_safe,
    prepare_job_safe,
    queue_storyboard,
    _progress,
)
from ...state import (
    store,
    task_queue,
)
from .scene_tasks import _enqueue_job_task
from .tts import (
    _tts_max_retries,
    _tts_attempts_remaining,
    _enqueue_tts_task,
)


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
        store.update(
            job,
            JobStatus.ASSETS_READY,
            f"ASSEMBLY RETRY {job.retries}/{job.max_retries}")
    if job.status == JobStatus.READY and job.source.startswith("telegram:"):
        parts = job.source.split(":", 2)
        if len(parts) == 3 and parts[1] != "unknown":
            try:
                TelegramAdapter().send_message(
                    parts[1], f"JOB {job.job_id}\nSTATUS: READY")
            except httpx.HTTPError as error:
                store.event(job, f"TELEGRAM NOTIFICATION FAILED: {error}")


def _ordered_scene_files(job) -> list[Path]:
    files = []
    accepted_kinds = {"video_scene"} if job.task_type == "video" else {"image"}
    for scene in sorted(job.scenes, key=lambda value: value.index):
        for artifact_id in scene.artifact_ids:
            artifact = next((value for value in job.artifacts if value.artifact_id ==
                            artifact_id and value.kind in accepted_kinds), None)
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
            transition_stage(
                job,
                StageName.ASSETS,
                StageStatus.FAILED,
                "Recovered image artifacts are missing")
            store.update(
                job,
                JobStatus.FAILED,
                "RECOVERY FAILED: IMAGE ARTIFACTS ARE MISSING")
        return
    queued_tasks = [(scene, _enqueue_job_task(job, scene))
                    for scene in pending_scenes(job) if not scene.task_id]
    from ...local_fallback import local_fallback_allowed
    if local_fallback_allowed():
        images = []
        for scene, queued_task in queued_tasks:
            task_id = queued_task["task_id"]
            task_queue.discard(task_id)
            image = store.root / job.job_id / \
                "images" / f"{scene.scene_id}.ppm"
            _demo_image(image)
            artifact = register_artifact(
                job,
                store.root,
                image,
                "image",
                scene_id=scene.scene_id,
                task_id=task_id,
                workflow=job.workflow)
            finish_scene(scene, [artifact.artifact_id])
            job.completed_task_ids.append(task_id)
            job.active_task_ids.pop(task_id, None)
            images.append(image)
        job.active_task_id = None
        if all_scenes_ready(job):
            transition_stage(job, StageName.ASSETS, StageStatus.READY)
            _finalize_and_notify(job, images)


def _pending_voice_scenes(job) -> list[SceneRecord]:
    initialize_plan(job)
    tts_scene_ids = set(job.tts_active_task_ids.values())
    return [scene for scene in job.scenes
            if scene.status in {StageStatus.READY, StageStatus.PENDING}
            and scene.scene_id not in tts_scene_ids and scene.voiceover]


def _character_voice_enabled(job) -> bool:
    """Whether the character's voice config requests TTS synthesis.

    An explicitly disabled provider (``disabled``/``off``/``false``/empty) means
    no voice is wanted, so CORE skips the TTS stage cleanly instead of enqueuing
    a synthesis task that the Voice Worker would reject.
    """
    try:
        character = load_character(
            Path(
                os.getenv(
                    "CHARACTERS_ROOT",
                    "characters")),
            job.character_id)
        provider = (character.voice or {}).get("provider")
    except Exception:
        return False
    return bool(provider) and str(provider).strip(
    ).lower() not in {"disabled", "off", "false"}


def _has_voice_worker(store: JobStore) -> bool:
    return any("speech_synthesis" in (worker.get("capabilities") or []) or worker.get(
        "role") == "voice" for worker in store.workers.values())


def _dispatch_tts(store, job) -> None:
    initialize_plan(job)
    if job.stages[StageName.TTS.value].status == StageStatus.PENDING:
        transition_stage(job, StageName.TTS, StageStatus.RUNNING)
    # Issue i.0.0.1.5: pin the per-scene TTS attempt budget on the Job at
    # dispatch time so a retry cannot inherit a budget that was raised for a
    # different stage, and so the budget survives a CORE restart (it is part of
    # the persisted Job record).
    try:
        job.tts_max_retries = max(0, int(os.getenv("TTS_MAX_RETRIES", "3")))
    except (TypeError, ValueError):
        job.tts_max_retries = 3
    if _pending_voice_scenes(job) and not _character_voice_enabled(job):
        transition_stage(job, StageName.TTS, StageStatus.READY)
        if job.task_type in {"image", "video"}:
            store.transition(job, JobStatus.ASSETS_READY,
                             "TTS SKIPPED; VOICE DISABLED")
        return
    if not job.tts_active_task_ids and not _pending_voice_scenes(job):
        transition_stage(job, StageName.TTS, StageStatus.READY)
        if job.task_type in {"image", "video"}:
            store.transition(job, JobStatus.ASSETS_READY,
                             "TTS SKIPPED; NO VOICEOVER")
        return
    [_enqueue_tts_task(job, scene)
     for scene in _pending_voice_scenes(job)
     if _tts_attempts_remaining(job, scene)]
    # Issue i.0.0.1.5: a scene whose separate TTS budget is already spent is
    # never silently re-dispatched.  It fails the TTS stage and is dead-lettered
    # instead of looping on an exhausted budget, so worker loss or a lost ack
    # cannot restart an unbounded retry.
    exhausted = [scene for scene in _pending_voice_scenes(job)
                 if not _tts_attempts_remaining(job, scene)]
    for scene in exhausted:
        job.tts_attempts[scene.scene_id] = _tts_max_retries(job)
        transition_stage(job, StageName.TTS, StageStatus.FAILED,
                         f"TTS budget exhausted for {scene.scene_id}")
        task_queue.dead_letter({"job_id": job.job_id, "task": "voice",
                                "scene_id": scene.scene_id},
                               f"TTS budget {_tts_max_retries(job)} exhausted")
        store.update(job, JobStatus.FAILED,
                     f"TTS FAILED: budget exhausted for {scene.scene_id}")


def _image_storyboard_gate(store, job) -> bool:
    """Block video/image asset generation until the ACTIVE image storyboard is approved.

    Issue #36/#6: Resume/Retry and other re-dispatches reset the job to NEW while the
    script is already ready. Without this gate ``_dispatch_assets`` would start video
    asset generation even though the current preview images were never approved by the
    operator. Returns ``True`` when the pipeline must wait (missing preview images are
    (re)queued, with LOCAL_WORKER_FALLBACK first-pass generation kept functional).

    Issue #60: Also block when storyboard is missing entirely or image_status is not
    "approved" after a legitimate storyboard generation attempt.
    """
    if job.task_type == "video" or job.storyboards:
        _PRE_STORYBOARD = {
            JobStatus.SCRIPT_APPROVED,
            JobStatus.STORYBOARD_QUEUED,
            JobStatus.STORYBOARD_GENERATING,
            JobStatus.STORYBOARD_FAILED}
        if job.status in _PRE_STORYBOARD:
            return False
        if not job.storyboards or not job.active_storyboard_version:
            store.event(
                job, "STORYBOARD GATE: No active storyboard; asset generation blocked")
            return True
        sb = next((s for s in job.storyboards if s.version ==
                  job.active_storyboard_version), None)
        if sb is None:
            store.event(
                job,
                f"STORYBOARD GATE: Active storyboard {job.active_storyboard_version} not found; asset generation blocked")
            return True
        if sb.image_status != "approved":
            from ...image_storyboard import queue_image_storyboard
            if not all(s.image_artifact_id for s in sb.scenes):
                queue_image_storyboard(store, job, sb.version)
                from ...local_fallback import local_fallback_allowed
                if local_fallback_allowed():
                    from ...image_storyboard import handle_image_result as _handle
                    import base64
                    demo_ppm = b"P6\n2 2\n255\n" + bytes((80, 120, 90)) * 4
                    b64 = base64.b64encode(demo_ppm).decode()
                    for tid in list(job.image_storyboard_task_ids.keys()):
                        _handle(store, job, tid, True, image_base64=b64)
                        from ...state import task_queue as _tq
                        _tq.ack(tid)
                    store.event(
                        job, f"IMAGE STORYBOARD {sb.version}:{sb.image_version} READY (fallback)")
            store.event(
                job,
                f"IMAGE STORYBOARD {sb.version} NOT APPROVED (status={sb.image_status}); waiting for approval")
            return True
    return False


def _prepare_and_dispatch(job) -> None:
    while True:
        if job.script and job.status == JobStatus.NEW:
            initialize_plan(job)
            assets_stage = job.stages[StageName.ASSETS.value]
            if assets_stage.status in {
                    StageStatus.PENDING,
                    StageStatus.FAILED,
                    StageStatus.PAUSED}:
                transition_stage(job, StageName.ASSETS, StageStatus.RUNNING)
            # Issue #36/#60: gate asset generation until image storyboard
            # approved
            if _image_storyboard_gate(store, job):
                return
            store.update(
                job,
                JobStatus.ASSET_GENERATION,
                "IMAGE TASK REDISPATCHED")
        elif job.status in {JobStatus.NEW, JobStatus.SCRIPT_QUEUED, JobStatus.SCRIPT_GENERATING, JobStatus.SCRIPT_FAILED}:
            prepare_job_safe(store, job)
        else:
            break
        if job.status != JobStatus.FAILED or not can_retry(job):
            break
        job.retries += 1
        store.update(
            job,
            JobStatus.NEW,
            f"AUTOMATIC RETRY {job.retries}/{job.max_retries}")
    if job.status == JobStatus.SCRIPT_PENDING_APPROVAL:
        _progress(job, "SCRIPT_PENDING_APPROVAL")
        return
    if job.status in {
            JobStatus.VIDEO_PENDING_APPROVAL,
            JobStatus.VIDEO_REVISION_REQUESTED}:
        _progress(job, job.status.value)
        return
    if job.status == JobStatus.SCRIPT_APPROVED:
        # Issue #81 R1: a free-text video revision routed through script
        # regeneration is applied again to the storyboard that follows.
        queue_storyboard(store, job, revision=job.video_revision_upstream)
        return
    if job.status == JobStatus.STORYBOARD_APPROVED:
        sb = next((s for s in job.storyboards if s.version ==
                  job.active_storyboard_version), None)
        if sb and sb.image_status != "approved":
            store.event(
                job,
                f"IMAGE STORYBOARD NOT APPROVED (status={sb.image_status}); enqueue preview images")
            from ...image_storyboard import queue_image_storyboard
            if not all(s.image_artifact_id for s in sb.scenes):
                queue_image_storyboard(store, job, sb.version)
                from ...local_fallback import local_fallback_allowed
                if local_fallback_allowed():
                    from ...image_storyboard import handle_image_result as _handle
                    import base64
                    demo_ppm = b"P6\n2 2\n255\n" + bytes((80, 120, 90)) * 4
                    b64 = base64.b64encode(demo_ppm).decode()
                    for tid in list(job.image_storyboard_task_ids.keys()):
                        _handle(store, job, tid, True, image_base64=b64)
                        from ...state import task_queue as _tq
                        _tq.ack(tid)
                    store.event(
                        job, f"IMAGE STORYBOARD {sb.version}:{sb.image_version} READY (fallback)")
            # No explicit image approval yet — wait for user action (Issue #6
            # rule)
            return
        # Issue #36/#60: gate asset generation until image storyboard approved
        if _image_storyboard_gate(store, job):
            return
        store.transition(
            job,
            JobStatus.ASSET_GENERATION,
            "ASSET GENERATION STARTED")
        _dispatch_assets(store, job)
        return
    if job.status == JobStatus.ASSET_GENERATION:
        # Issue #36/#60: gate asset generation until image storyboard approved
        if _image_storyboard_gate(store, job):
            return
        _dispatch_assets(store, job)
    if job.status == JobStatus.TTS_GENERATING:
        _dispatch_tts(store, job)
