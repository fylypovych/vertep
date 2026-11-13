"""Worker task-result handlers for ``POST /api/tasks/result``.

``core.api.tasks.task_result`` authenticates the Worker, resolves the Job and
routes the result to exactly one handler here by the kind of task it answers:
image storyboard, storyboard, script, assembly, publish, TTS or a scene asset.
Each handler owns its fencing, persistence, acknowledgement and Job transition.
Extracted from ``core/api/tasks.py`` without changing behaviour.
"""
import base64
import binascii
import json
import os
from pathlib import Path

from fastapi import HTTPException

from ..artifacts import register_artifact
from ..file_validation import validate_media_contract, validate_media_integrity
from ..models import JobStatus, StageName, StageStatus, TaskResult, utc_now
from ..orchestration import (all_scenes_ready, cancel_scene, fail_scene,
                             finish_scene, transition_stage)
from ..state import executor, store, task_queue
from .job_helpers import (
    _enqueue_job_task,
    _finalize_and_notify,
    _character_voice_enabled,
    _dispatch_tts,
    _has_voice_worker,
    _handle_tts_failure,
    _ordered_scene_files,
    _pending_voice_scenes,
    _redact_payload,
    _persist_tts_contract,
    _scene_for_task,
    _task_for,
    _tts_task_for,
    _voice_task_for,
    _validate_tts_contract)


def _text_task_lease(
        job, task_id: str, node_name: str) -> tuple[bool, str | None]:
    """Ownership contract for a script/storyboard result (Issue i.0.0.1.2).

    A text-task result is accepted only when the submitting node holds the whole
    claim lease of this exact task for this exact Job:

    * **owner** — the Worker record is bound to ``task_id`` and ``job.job_id``;
    * **lease** — the task is still in flight, i.e. its lease has not expired,
      been re-queued, discarded or acked by CORE.

    Checked BEFORE any cleanup (Worker metadata, ``Job.*_task_id``) or queue ack,
    so a foreign, expired or stale submission changes nothing.

    ``inflight_active`` (not ``inflight_has``) proves the lease: a record whose
    deadline already passed must be rejected during the window before the
    watchdog re-queues it (Issue i.0.0.1.61).
    """
    worker = store.workers.get(node_name)
    if (worker is None or worker.get("current_task") != task_id
            or worker.get("current_job") != job.job_id):
        return False, "owner/lease mismatch"
    if not task_queue.inflight_active(task_id):
        return False, "task lease is no longer active"
    return True, None


def image_storyboard_result(job, result: TaskResult):
    """Result of an image-storyboard task."""
    from ..image_storyboard import handle_image_result
    worker = store.workers.get(result.node_name)
    if worker:
        desired_status = worker.get("desired_state")
        next_status = desired_status if desired_status in {
            "DRAINING", "QUARANTINED"} else "READY"
        worker.update({"status": next_status,
                       "current_job": None,
                       "current_task": None,
                       "last_seen": utc_now()})
        store.save_worker(worker)
    try:
        handle_image_result(
            store,
            job,
            result.task_id,
            result.success,
            result.image_base64,
            result.error,
            result.artifacts or result.images)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    from ..state import task_queue as _tq2
    _tq2.ack(result.task_id)
    store.repository.record_task({"job_id": job.job_id,
                                  "task_id": result.task_id,
                                  "task": "image",
                                  "kind": "image_storyboard"},
                                 "COMPLETED" if result.success else "FAILED",
                                 result.node_name,
                                 result.error)
    if not result.success:
        _tq2.dead_letter({"job_id": job.job_id,
                          "task_id": result.task_id,
                          "kind": "image_storyboard"},
                         result.error)
    return job


def storyboard_result(job, result: TaskResult):
    """Result of a storyboard text task."""
    from ..storyboard import StoryboardService
    # Issue i.0.0.1.2: fence the submission before any side effect.  The
    # storyboard branch used to accept a result from any node whose
    # ``current_task`` matched, and freed that node afterwards; the lease
    # must still be live and bound to this Job before anything is cleaned up
    # or acked.
    owned, reason = _text_task_lease(job, result.task_id, result.node_name)
    if not owned:
        store.event(job, f"STORYBOARD RESULT {result.task_id} REJECTED: "
                         f"{reason} (node={result.node_name})")
        return job
    terminal_error = None
    try:
        accepted = StoryboardService(store).handle_result(
            job.job_id,
            result.task_id,
            result.success,
            result.artifacts,
            result.error,
            result.node_name,
            cancelled=result.cancelled)
    except RuntimeError as error:
        if result.success or job.status != JobStatus.STORYBOARD_FAILED:
            raise HTTPException(400, str(error)) from error
        # A failed result that exhausts the budget is still an accepted
        # result: finish its lease and owner before returning the failed
        # Job.
        job.storyboard_task_id = None
        job.active_task_id = None
        accepted = job
        terminal_error = str(error)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if accepted is None:
        # Rejected result: the task stays in flight for the legitimate
        # claim holder; do NOT ack, dead-letter or free the worker.
        return job
    job.assigned_worker = None
    store.repository.save_job(job)
    worker = store.workers.get(result.node_name)
    if worker:
        desired_status = worker.get("desired_state")
        next_status = desired_status if desired_status in {
            "DRAINING", "QUARANTINED"} else "READY"
        worker.update({"status": next_status,
                       "current_job": None,
                       "current_task": None,
                       "last_seen": utc_now()})
        store.save_worker(worker)
    from ..state import task_queue as _tq2
    _tq2.ack(result.task_id)
    store.repository.record_task({"job_id": job.job_id,
                                  "task_id": result.task_id,
                                  "task": "storyboard"},
                                 "COMPLETED" if result.success else "FAILED",
                                 result.node_name,
                                 result.error)
    if not result.success:
        _tq2.dead_letter({"job_id": job.job_id,
                          "task_id": result.task_id,
                          "task": "storyboard"},
                         result.error)
    if terminal_error is not None:
        raise HTTPException(400, terminal_error)
    return job


def script_result(job, result: TaskResult):
    """Result of a script text task."""
    from ..api.job_helpers import _handle_script_result
    # Issue i.0.0.1.2: validate ownership BEFORE any cleanup.  This branch
    # used to clear the Worker metadata (and ``job.script_task_id`` inside
    # ``_handle_script_result``) without proving the submitter held the
    # claim, so a foreign or expired node could complete the task, release
    # the real claim holder and get the task acked.
    owned, reason = _text_task_lease(job, result.task_id, result.node_name)
    if not owned:
        store.event(job, f"SCRIPT RESULT {result.task_id} REJECTED: "
                         f"{reason} (node={result.node_name})")
        return job
    if result.cancelled:
        store.event(
            job, f"SCRIPT RESULT {result.task_id} REJECTED: "
            f"cancelled result cannot complete a script (node={result.node_name})")
        return job
    worker = store.workers.get(result.node_name)
    if worker:
        desired_status = worker.get("desired_state")
        next_status = desired_status if desired_status in {
            "DRAINING", "QUARANTINED"} else "READY"
        worker.update({"status": next_status,
                       "current_job": None,
                       "current_task": None,
                       "last_seen": utc_now()})
        store.save_worker(worker)
    try:
        _handle_script_result(store,
                              job,
                              {"success": result.success,
                               "task_id": result.task_id,
                               "error": result.error,
                               "worker_name": result.node_name},
                              result.artifacts or [])
    except (ValueError, RuntimeError) as error:
        raise HTTPException(400, str(error)) from error
    from ..state import task_queue as _tq2
    _tq2.ack(result.task_id)
    store.repository.record_task({"job_id": job.job_id,
                                  "task_id": result.task_id,
                                  "task": "script"},
                                 "COMPLETED" if result.success else "FAILED",
                                 result.node_name,
                                 result.error)
    if not result.success and job.status != JobStatus.SCRIPT_FAILED:
        _tq2.dead_letter({"job_id": job.job_id,
                          "task_id": result.task_id,
                          "task": "script"},
                         result.error)
    return job


def assembly_result(job, result: TaskResult):
    """Result of a video assembly attempt."""
    from .job_helpers import (
        _assembly_retry_task,
        _assembly_worker_owns,
        _clear_assembly_staging,
        _enqueue_assembly_task,
        _handle_assembly_result,
        _release_worker)
    from ..state import task_queue as _tq2
    # Fencing first: a result from a node that no longer owns the attempt (lease
    # expired, node reassigned, attempt cancelled) is refused without touching
    # the Job, so a late or duplicate result can never overwrite an accepted
    # version (Issue #122 §5).
    if not _assembly_worker_owns(job, result.task_id, result.node_name):
        store.event(job, f"ASSEMBLY RESULT {result.task_id} REJECTED: "
                         f"fencing mismatch (node={result.node_name})")
        raise HTTPException(
            409, "Assembly result does not match the active attempt lease")
    # Cancel check BEFORE any side effect: discard the result without writing
    # the video file or registering a version (Issue #122 §5).
    if job.assembly_cancel_requested:
        _tq2.ack(result.task_id)
        job.assembly_task_ids.pop(result.task_id, None)
        if job.assembly_task_id == result.task_id:
            job.assembly_task_id = None
        _release_worker(store.workers.get(result.node_name))
        store.repository.record_task(
            {"job_id": job.job_id, "task_id": result.task_id, "task": "assembly"},
            "CANCELLED", result.node_name, None)
        store.event(
            job, f"ASSEMBLY RESULT {result.task_id} DISCARDED: attempt cancelled")
        return job
    _release_worker(store.workers.get(result.node_name))
    if not result.success:
        _tq2.ack(result.task_id)
        job.assembly_task_ids.pop(result.task_id, None)
        if job.assembly_task_id == result.task_id:
            job.assembly_task_id = None
        job.assembly_error = result.error
        job.assembly_attempt += 1
        job.assigned_worker = None
        store.repository.record_task(
            {"job_id": job.job_id, "task_id": result.task_id, "task": "assembly"},
            "FAILED", result.node_name, result.error)
        if job.assembly_attempt <= job.max_retries:
            task = _assembly_retry_task(job)

            if task is not None:
                _tq2.dead_letter({"job_id": job.job_id,
                                  "task_id": result.task_id,
                                  "task": "assembly"},
                                 result.error)
                _enqueue_assembly_task(job, task, new_attempt=True)
                store.update(
                    job,
                    JobStatus.ASSEMBLY,
                    f"ASSEMBLY RETRY {job.assembly_attempt}/{job.max_retries}")
                return job
        _tq2.dead_letter({"job_id": job.job_id, "task_id": result.task_id,
                          "task": "assembly"}, result.error)
        transition_stage(
            job,
            StageName.ASSEMBLY,
            StageStatus.FAILED,
            result.error or "assembly failed")
        store.update(
            job,
            JobStatus.FAILED,
            f"ASSEMBLY FAILED: {result.error or 'unknown'}")
        # Terminal failure: no retry will reuse the staged copies, so they
        # go.
        _clear_assembly_staging(store.root, job.job_id)
        return job
    try:
        _handle_assembly_result(job,
                                {"success": True,
                                 "task_id": result.task_id,
                                 "worker_name": result.node_name},
                                result.artifacts or [])
    except (ValueError, RuntimeError) as error:
        _tq2.ack(result.task_id)
        job.assembly_task_ids.pop(result.task_id, None)
        if job.assembly_task_id == result.task_id:
            job.assembly_task_id = None
        job.assembly_error = str(error)
        job.assembly_attempt += 1
        store.repository.record_task(
            {"job_id": job.job_id, "task_id": result.task_id, "task": "assembly"},
            "FAILED", result.node_name, str(error))
        store.event(
            job, f"ASSEMBLY RESULT {result.task_id} REJECTED: {error}")
        if job.assembly_attempt <= job.max_retries:
            task = _assembly_retry_task(job)

            if task is not None:
                _enqueue_assembly_task(job, task, new_attempt=True)
                store.update(
                    job,
                    JobStatus.ASSEMBLY,
                    f"ASSEMBLY RETRY {job.assembly_attempt}/{job.max_retries}")
                return job
        transition_stage(
            job,
            StageName.ASSEMBLY,
            StageStatus.FAILED,
            str(error))
        store.update(job, JobStatus.FAILED, f"ASSEMBLY REJECTED: {error}")
        _clear_assembly_staging(store.root, job.job_id)
        return job
    _tq2.ack(result.task_id)
    job.assembly_attempt = 0
    job.assembly_error = None
    job.assigned_worker = None
    store.repository.record_task(
        {"job_id": job.job_id, "task_id": result.task_id, "task": "assembly"},
        "COMPLETED", result.node_name, None)
    transition_stage(job, StageName.ASSEMBLY, StageStatus.READY)
    version = job.active_video_version
    store.update(job, JobStatus.VIDEO_READY, f"VIDEO READY (v{version})")
    store.update(job, JobStatus.VIDEO_PENDING_APPROVAL,
                 f"VIDEO PENDING APPROVAL (v{version})")
    if job.source.startswith("telegram:"):
        from ..pipeline import _send_video_approval_to_telegram
        _send_video_approval_to_telegram(job)
    else:
        from ..pipeline import _progress
        _progress(job, "VIDEO_PENDING_APPROVAL")
    return job


def publish_result(job, result: TaskResult):
    """Result of a publish task."""
    from ..api.job_helpers import (
        _handle_publish_result,
        _validate_publication_receipt,
        _match_publish_receipt)
    # Owner/lease check: the result must belong to the active publish task
    # and to the worker that currently owns it.  A mismatched or duplicate
    # result is rejected without releasing the Worker or acking the task,
    # so a lost ack / restart can retry the same channel safely.
    active_channel = job.publish_task_ids.get(result.task_id)
    worker = store.workers.get(result.node_name)
    owns_task = (
        worker is not None
        and worker.get("current_task") == result.task_id
        and worker.get("current_job") == job.job_id
    )
    if not owns_task or active_channel is None or not task_queue.inflight_active(
            result.task_id):
        store.event(job, f"PUBLISH RESULT {result.task_id} REJECTED: "
                         f"owner/lease mismatch (node={result.node_name})")
        raise HTTPException(
            409, "Publish result does not match the active task lease")
    if result.success:
        try:
            artifact = next(a for a in result.artifacts or []
                            if a.get("kind") == "publication_receipt")
            receipt = json.loads(
                base64.b64decode(
                    artifact["data_base64"],
                    validate=True))
            valid, detail = _validate_publication_receipt(
                receipt, active_channel)
            if not valid:
                raise ValueError(detail)
            intent = job.publish_intent.get(active_channel) or {}
            for field, expected in {"job_id": job.job_id, "task_id": result.task_id,
                                    "video_version": intent.get("video_version"),
                                    "video_sha256": intent.get("video_sha256")}.items():
                if expected is not None and str(
                        receipt.get(field)) != str(expected):
                    raise ValueError(
                        f"Publish receipt {field} does not match issued task")
            if _match_publish_receipt(job, active_channel, receipt)[
                    0] == "reject":
                raise ValueError(
                    "Publish receipt does not match issued intent")
        except (StopIteration, KeyError, TypeError, ValueError) as error:
            raise HTTPException(
                400, "Invalid issued publication receipt") from error
    if worker:
        desired_status = worker.get("desired_state")
        next_status = desired_status if desired_status in {
            "DRAINING", "QUARANTINED"} else "READY"
        worker.update({"status": next_status,
                       "current_job": None,
                       "current_task": None,
                       "last_seen": utc_now()})
        store.save_worker(worker)
    channel = active_channel
    try:
        _handle_publish_result(store,
                               job,
                               {"success": result.success,
                                "task_id": result.task_id,
                                "error": result.error},
                               result.artifacts or [],
                               channel)
    except (ValueError, RuntimeError) as error:
        raise HTTPException(400, str(error)) from error
    from ..state import task_queue as _tq2
    _tq2.ack(result.task_id)
    if result.success:
        store.repository.record_task(
            {
                "job_id": job.job_id,
                "task_id": result.task_id,
                "task": "publish",
                "channel": channel},
            "COMPLETED",
            result.node_name,
            result.error)
    else:
        store.repository.record_task(
            {
                "job_id": job.job_id,
                "task_id": result.task_id,
                "task": "publish",
                "channel": channel},
            "FAILED",
            result.node_name,
            result.error)
        _tq2.dead_letter({"job_id": job.job_id,
                          "task_id": result.task_id,
                          "task": "publish",
                          "channel": channel},
                         result.error)
    remaining = list(job.publish_task_ids.keys())
    if not remaining:
        all_published = all(
            r.get("status") == "PUBLISHED" for r in job.publication_results.values())
        not_configured = any(
            r.get("status") == "NOT_CONFIGURED" for r in job.publication_results.values())
        failed = any(
            r.get("status") == "FAILED" for r in job.publication_results.values())
        if all_published and job.publication_results:
            transition_stage(job, StageName.PUBLISH, StageStatus.READY)
            store.update(job, JobStatus.PUBLISHED, "PUBLISHED")
        elif not_configured and not failed and not all_published:
            transition_stage(
                job,
                StageName.PUBLISH,
                StageStatus.FAILED,
                "NOT CONFIGURED")
            store.update(
                job,
                JobStatus.FAILED,
                "PUBLISHING FAILED OR NOT CONFIGURED")
        elif failed:
            transition_stage(
                job,
                StageName.PUBLISH,
                StageStatus.FAILED,
                job.publish_error or "publish failed")
            store.update(job, JobStatus.FAILED, "PUBLISHING FAILED")
        else:
            store.event(
                job,
                f"PUBLISH: {len(job.publication_results)} channel(s) resolved, no pending tasks")
    return job


def tts_result(job, result: TaskResult):
    """Result of a TTS (voice) scene task."""
    scene = _scene_for_task(job, result.task_id)
    if not scene or scene.assigned_worker != result.node_name:
        raise HTTPException(
            409, "TTS result does not match the active task")
    owned, reason = _text_task_lease(job, result.task_id, result.node_name)
    if not owned:
        raise HTTPException(409, reason)
    if result.task_id not in (job.voice_task_snapshot or {}):
        raise HTTPException(
            409, "No issued voice-task snapshot for this result")
    if job.status in {JobStatus.CANCELLED, JobStatus.PAUSED}:
        raise HTTPException(409, f"Job is {job.status.value}")
    if not result.success:
        _handle_tts_failure(
            job,
            scene,
            result.task_id,
            result.node_name,
            result.error)
        return store.event(
            job, f"TTS RESULT FOR {scene.scene_id} RECEIVED FROM {result.node_name}")
    artifacts = result.artifacts or []
    if not artifacts:
        raise HTTPException(400, "Successful TTS result has no artifact")
    saved_artifacts = []
    prepared_images = []
    total_size = 0
    for artifact_index, artifact in enumerate(artifacts, 1):
        try:
            encoded = artifact.get(
                "data_base64") or artifact.get("image_base64")
            data = base64.b64decode(encoded, validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error) as error:
            raise HTTPException(400, "Invalid base64 artifact") from error
        total_size += len(data)
        max_artifact_bytes = int(
            os.getenv(
                "MAX_ARTIFACT_BYTES",
                "26214400"))
        if total_size > max_artifact_bytes:
            raise HTTPException(413, "Artifacts are too large")
        supplied_name = Path(
            artifact.get(
                "filename",
                f"{scene.scene_id}.wav")).name
        suffix = Path(supplied_name).suffix.lower()
        allowed_suffixes = {".wav", ".ogg", ".mp3", ".aac", ".m4a"}
        if suffix not in allowed_suffixes:
            raise HTTPException(
                400, f"Unsupported audio artifact type: {suffix}")
        try:
            validate_media_integrity(data, suffix)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        filename = f"voice-{scene.scene_id}{suffix}" if len(
            artifacts) == 1 else f"voice-{scene.scene_id}-{artifact_index:03d}{suffix}"
        audio_path = store.root / job.job_id / "audio" / filename
        # Validate the contract and the task parameters BEFORE any side effect
        # (file write, artifact registration, Worker release or ack).  A
        # rejected result must leave no accepted artifact and must not change
        # Worker ownership so the job can retry or fail loudly.
        try:
            # Issue #197/#165.2: validate the Worker's contract against the
            # exact voice-task snapshot that was issued at enqueue time,
            # never against a task rebuilt from the live Character config.
            # A config change between claim and result must be a controlled
            # rejection that leaves no artifact, no ack and no Worker
            # release.
            _validate_tts_contract(
                (job.voice_task_snapshot or {}).get(result.task_id)
                or _voice_task_for(job, result.task_id, scene),
                data, artifact.get("contract"))
        except ValueError as error:
            # A rejected result must leave no accepted artifact and must not
            # change Worker ownership. Keep the worker BUSY with this
            # current_job/current_task so the Job can retry or fail loudly.
            # The lease stays in flight (no ack) and the worker is NOT
            # released.
            raise HTTPException(
                400, f"Invalid audio contract: {error}") from error
        prepared_images.append(
            (audio_path, data, artifact.get("contract")))
    temporary_images = []
    try:
        for audio_path, data, _contract in prepared_images:
            temporary = audio_path.with_name(
                f".{audio_path.name}.{result.task_id[:8]}.part")
            temporary.write_bytes(data)
            temporary_images.append((temporary, audio_path))
        for temporary, audio_path in temporary_images:
            temporary.replace(audio_path)
    except OSError as error:
        for temporary, _ in temporary_images:
            temporary.unlink(missing_ok=True)
        # A persistence failure must not change Worker ownership. Keep the
        # worker BUSY with this current_job/current_task so the Job can retry
        # or fail loudly. The lease stays in flight (no ack).
        raise HTTPException(
            500, "Could not persist audio artifacts") from error
    for audio_path, data, contract in prepared_images:
        saved_artifacts.append(
            register_artifact(
                job,
                store.root,
                audio_path,
                "audio",
                scene_id=scene.scene_id,
                task_id=result.task_id,
                node_name=result.node_name,
                workflow=job.workflow))
        try:
            saved_artifacts.extend(
                _persist_tts_contract(
                    store,
                    job,
                    scene,
                    result,
                    audio_path,
                    data,
                    contract))
        except (ValueError, OSError) as error:
            # A rejected result must leave no accepted artifact and must not
            # change Worker ownership. Keep the worker BUSY with this
            # current_job/current_task so the Job can retry or fail loudly.
            # The lease stays in flight (no ack) and the worker is NOT
            # released.
            raise HTTPException(
                400, f"Invalid audio contract: {error}") from error
    # Release the Worker only AFTER every artifact has been validated and
    # persisted, so a rejected result does not free the worker or ack the
    # task before the Job can retry/fail loudly.
    worker = store.workers.get(result.node_name)
    if worker:
        desired_status = worker.get("desired_state")
        next_status = desired_status if desired_status in {
            "DRAINING", "QUARANTINED"} else "READY"
        worker.update({"status": next_status, "current_job": None,
                       "current_task": None, "last_seen": utc_now()})
        store.save_worker(worker)
    task_queue.ack(result.task_id)
    store.repository.record_task(
        _tts_task_for(
            job,
            scene) | {
            "task_id": result.task_id},
        "COMPLETED",
        result.node_name)
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
                transition_stage(
                    job,
                    StageName.ASSEMBLY,
                    StageStatus.FAILED,
                    "Missing images for assembly")
                store.update(
                    job,
                    JobStatus.FAILED,
                    "MISSING IMAGES FOR ASSEMBLY")
        elif job.task_type == "voice":
            terminal = JobStatus.READY
            return store.update(job, terminal, "VOICE TASK COMPLETED")
    return store.event(
        job, f"TTS RESULT FOR {scene.scene_id} RECEIVED FROM {result.node_name}")


def scene_result(job, result: TaskResult):
    """Result of a scene asset task (image, video, text, voice, publish, backup)."""
    if result.task_id in job.completed_task_ids:
        raise HTTPException(
            409, "Duplicate result for an already completed task")
    from .job_helpers import cancelled_attempt_release, release_assembly_hold
    from adapters.providers.base import RELEASE_RELEASED
    cancelled = cancelled_attempt_release(job, result.task_id)
    if cancelled:
        # A cancelled attempt that comes back with a render is the one thing a logical
        # cancel could not prove: the render ended, so the runtime is free. The result
        # is still rejected as a late artifact of a cancelled attempt — proving the
        # release never turns it into a second video version (Issue #122 §5,
        # §9.8).
        release_assembly_hold(
            job, cancelled["submit_key"], state=RELEASE_RELEASED,
            reason="late_result_of_cancelled_attempt_discarded",
        )
        store.event(
            job, f"ASSEMBLY RELEASE {cancelled['submit_key']} RELEASED: the cancelled "
            f"attempt {result.task_id} came back with a finished render")
        raise HTTPException(
            409, "Assembly result does not match the active attempt lease")
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
        next_status = desired_status if desired_status in {
            "DRAINING", "QUARANTINED"} else "READY"
        worker.update({"status": next_status, "current_job": None,
                       "current_task": None, "last_seen": utc_now()})
        store.save_worker(worker)
    if result.cancelled:
        # CORE already asked for this task to stop and the compute backend could
        # not interrupt an executing prompt without harming other work.  The late
        # result is a terminal cancellation: ack it, free the scene and never
        # retry or fail the job for it.
        task_queue.ack(result.task_id)
        store.repository.record_task(
            _task_for(
                job,
                scene) | {
                "task_id": result.task_id},
            "CANCELLED",
            result.node_name)
        job.active_task_ids.pop(result.task_id, None)
        job.active_task_id = None
        job.assigned_worker = None
        scene.task_id = None
        scene.assigned_worker = None
        cancel_scene(scene, result.error or "cancelled by CORE")
        return store.event(
            job, f"{scene.scene_id} CANCELLED (late result discarded)")
    if not result.success:
        task_queue.ack(result.task_id)
        store.repository.record_task(
            _task_for(
                job,
                scene) | {
                "task_id": result.task_id},
            "FAILED",
            result.node_name,
            result.error)
        fail_scene(scene, result.error or "unknown error")
        job.active_task_ids.pop(result.task_id, None)
        scene.task_id = None
        scene.assigned_worker = None
        if len(scene.attempts) < job.max_retries:
            store.event(
                job,
                f"{scene.scene_id} FAILED, RETRY {len(scene.attempts)}/{job.max_retries}: {result.error or 'unknown error'}")
            delay = float(os.getenv("RETRY_BACKOFF_BASE", "2")) * \
                (2 ** max(0, job.retries - 1))
            _enqueue_job_task(job, scene, new_attempt=True, delay=delay)
            return job
        job.active_task_id = None
        job.assigned_worker = None
        for sibling_task_id, sibling_scene_id in list(
                job.active_task_ids.items()):
            sibling = next(
                (item for item in job.scenes if item.scene_id == sibling_scene_id), None)
            sibling_worker = sibling.assigned_worker if sibling else None
            if sibling_worker:
                task_queue.request_cancel(sibling_worker, sibling_task_id)
            task_queue.discard(sibling_task_id)
            if sibling:
                cancel_scene(
                    sibling, f"Sibling {scene.scene_id} exhausted retries")
            store.repository.record_task(
                _task_for(
                    job,
                    sibling) | {
                    "task_id": sibling_task_id},
                "CANCELLED",
                sibling_worker)
        job.active_task_ids.clear()
        transition_stage(
            job,
            StageName.ASSETS,
            StageStatus.FAILED,
            result.error or "unknown error")
        task_queue.dead_letter(
            _task_for(
                job,
                scene) | {
                "task_id": result.task_id},
            result.error)
        return store.update(
            job,
            JobStatus.FAILED,
            f"WORKER FAILED: {result.error or 'unknown error'}")
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
            encoded = artifact.get(
                "data_base64") or artifact.get("image_base64")
            data = base64.b64decode(encoded, validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error) as error:
            raise HTTPException(400, "Invalid base64 artifact") from error
        total_size += len(data)
        max_artifact_bytes = int(
            os.getenv(
                "MAX_VIDEO_ARTIFACT_BYTES",
                "268435456")) if job.task_type == "video" else int(
            os.getenv(
                "MAX_ARTIFACT_BYTES",
                "26214400"))
        if total_size > max_artifact_bytes:
            raise HTTPException(413, "Artifacts are too large")
        supplied_name = Path(
            artifact.get(
                "filename",
                f"{scene.scene_id}.png")).name
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
            raise HTTPException(
                400, f"Unsupported {job.task_type} artifact type")
        try:
            validate_media_integrity(data, suffix)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        if job.task_type in {"publish", "backup"} and suffix == ".json":
            # Issue #190: a receipt artifact is persisted under the Job tree and
            # later served to the Web UI.  Mask secret-shaped values here too so
            # the on-disk copy cannot leak what the API boundary already masks.
            try:
                receipt = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as error:
                raise HTTPException(400, "Invalid receipt artifact") from error
            data = json.dumps(_redact_payload(receipt), ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8")
        expected_media = "video" if folder == "video" else (
            "image" if folder == "images" else None)
        if expected_media:
            try:
                validate_media_contract(
                    artifact, data, expected=expected_media)
            except ValueError as error:
                raise HTTPException(400, str(error)) from error
        filename = f"{scene.scene_id}{suffix}" if len(
            artifacts) == 1 else f"{scene.scene_id}-{artifact_index:03d}{suffix}"
        image_path = store.root / job.job_id / folder / filename
        # The folder of a result kind is not necessarily part of the Job layout
        # created at creation time (a video scene lands in ``videos/``), so it is
        # created on demand instead of failing the whole import with an OSError
        # (Issue #178.4).
        image_path.parent.mkdir(parents=True, exist_ok=True)
        prepared_images.append((image_path, data))
    temporary_images = []
    try:
        for image_path, data in prepared_images:
            temporary = image_path.with_name(
                f".{image_path.name}.{result.task_id[:8]}.part")
            temporary.write_bytes(data)
            temporary_images.append((temporary, image_path))
        for temporary, image_path in temporary_images:
            temporary.replace(image_path)
    except OSError as error:
        for temporary, _ in temporary_images:
            temporary.unlink(missing_ok=True)
        raise HTTPException(
            500, "Could not persist worker artifacts") from error
    for image_path, _ in prepared_images:
        saved_images.append(image_path)
        artifact_kind = {
            "video": "video_scene",
            "text": "text",
            "voice": "audio",
            "publish": "publication_receipt",
            "backup": "backup_receipt"}.get(
            job.task_type,
            "image")
        saved_artifacts.append(
            register_artifact(
                job,
                store.root,
                image_path,
                artifact_kind,
                scene_id=scene.scene_id,
                task_id=result.task_id,
                node_name=result.node_name,
                workflow=job.workflow))
    if not result.success:
        task_queue.ack(result.task_id)
        store.repository.record_task(
            _task_for(
                job,
                scene) | {
                "task_id": result.task_id},
            "FAILED",
            result.node_name,
            result.error)
        fail_scene(scene, result.error or "unknown error")
        job.active_task_ids.pop(result.task_id, None)
        scene.assigned_worker = None
        if len(scene.attempts) < job.max_retries:
            store.event(
                job,
                f"{scene.scene_id} FAILED, RETRY {len(scene.attempts)}/{job.max_retries}: {result.error or 'unknown error'}")
            delay = float(os.getenv("RETRY_BACKOFF_BASE", "2")) * \
                (2 ** max(0, job.retries - 1))
            _enqueue_job_task(job, scene, new_attempt=True)
            return job
        transition_stage(
            job,
            StageName.ASSETS,
            StageStatus.FAILED,
            result.error or "unknown error")
        return store.update(
            job,
            JobStatus.FAILED,
            f"ASSET GENERATION FAILED: {result.error or 'unknown error'}")
    task_queue.ack(result.task_id)
    store.repository.record_task(
        _task_for(
            job,
            scene) | {
            "task_id": result.task_id},
        "COMPLETED",
        result.node_name)
    job.completed_task_ids.append(result.task_id)
    finish_scene(scene, [item.artifact_id for item in saved_artifacts])
    job.active_task_ids.pop(result.task_id, None)
    job.active_task_id = next(iter(job.active_task_ids), None)
    job.assigned_worker = None
    if all_scenes_ready(job):
        transition_stage(job, StageName.ASSETS, StageStatus.READY)
        if job.task_type in {"text", "voice", "backup", "publish"}:
            terminal = JobStatus.PUBLISHED if job.task_type == "publish" else JobStatus.READY
            return store.update(
                job, terminal, f"{job.task_type.upper()} TASK COMPLETED")
        if _pending_voice_scenes(job) and _character_voice_enabled(
                job) and _has_voice_worker(store):
            store.transition(
                job,
                JobStatus.TTS_GENERATING,
                "TTS GENERATION STARTED")
            _dispatch_tts(store, job)
            return job
        ordered_images = _ordered_scene_files(job)
        executor.submit(_finalize_and_notify, job, ordered_images)
    return store.event(
        job, f"RESULT FOR {scene.scene_id} RECEIVED FROM {result.node_name}")
