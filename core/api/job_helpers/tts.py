"""TTS (voice) task construction, contract validation, retries and failures."""

import hashlib
import json
import os
import time
from pathlib import Path

from ...artifacts import register_artifact
from ...configuration import load_character
from ...models import (
    JobStatus,
    StageName,
    StageStatus,
    utc_now,
)
from ...orchestration import transition_stage
from ...state import (
    store,
    task_queue,
)


def _tts_task_for(job, scene) -> dict:
    character = load_character(
        Path(
            os.getenv(
                "CHARACTERS_ROOT",
                "characters")),
        job.character_id)
    voice_config = character.voice or {}
    return {
        "job_id": job.job_id,
        "task": "voice",
        "priority": job.priority,
        "min_vram_mb": 0,
        "workflow": job.workflow or "",
        "topic": scene.voiceover or scene.prompt or job.topic,
        "task_id": None,
        "scene_id": scene.scene_id,
        "character_id": job.character_id,
        "voice": voice_config.get("voice"),
        "provider": voice_config.get("provider"),
        "language": voice_config.get("language") or character.language or "uk",
        "model": voice_config.get("model") or voice_config.get("engine"),
        "engine": voice_config.get("engine"),
        "speed": voice_config.get(
            "speed",
            150)}


def _validate_tts_contract(task: dict, data: bytes, contract) -> None:
    """Validate an audio contract against the task requirements BEFORE any side effect.

    The Voice Worker declares in ``contract`` which character voice config
    (provider/voice/model/language/speed) actually drove the synthesis plus a
    sha256 of the audio bytes.  CORE re-checks the digest against what it
    received and cross-checks the declared parameters against the task
    requirements so a mismatched or tampered artifact is rejected before the
    audio file is written, the artifact is registered, the Worker is released
    or the task is acked.  A rejected result must leave no accepted artifact and
    must not change Worker ownership.

    Only the fields the task actually requires are cross-checked: a task that
    does not pin a model/language/speed does not demand them, but a task that
    does pin them must be honoured exactly.  ``provider`` and ``voice`` are
    always mandatory for a voice task.
    """
    if not isinstance(contract, dict):
        raise ValueError("Audio contract is missing or not a dict")
    if contract.get("format") != "audio_contract/v1":
        raise ValueError(
            "Audio contract must declare format 'audio_contract/v1'")
    expected = contract.get("sha256")
    if not isinstance(expected, str) or not expected:
        raise ValueError("Audio contract must declare sha256")
    if expected != hashlib.sha256(data).hexdigest():
        raise ValueError(
            "Audio contract sha256 does not match artifact payload")
    if not contract.get("provider"):
        raise ValueError("Audio contract must declare provider")
    if not contract.get("voice"):
        raise ValueError("Audio contract must declare voice")
    for field in ("provider", "voice", "model", "language", "speed"):
        required = task.get(field)
        if required is None:
            continue
        actual = contract.get(field)
        if actual is None:
            raise ValueError(
                f"Audio contract is missing required field {field!r}")
        if str(actual) != str(required):
            raise ValueError(
                f"Audio contract {field!r}={actual!r} does not match task requirement {required!r}")


def _persist_tts_contract(
        store,
        job,
        scene,
        result,
        audio_path,
        data,
        contract) -> list:
    """Validate and persist a verifiable audio contract for a Voice Worker artifact.

    The contract is produced by the Voice Worker and declares which character
    voice config (provider/voice/...) actually drove the synthesis plus a
    sha256 of the audio bytes.  We re-check the digest against what CORE
    received and store the contract as a sidecar artifact so the audio is
    verifiable after the fact.  Validation is performed by
    :func:`_validate_tts_contract` before any file is written here.

    Issue #197/#165.2: the task requirements are read from the snapshot taken
    at enqueue time (``job.voice_task_snapshot[result.task_id]``), never
    rebuilt from the live Character config.  A config change between claim
    and result must not silently validate an artifact against a different
    voice requirement than the one the Worker was dispatched with.
    """
    _validate_tts_contract(
        _voice_task_for(
            job,
            result.task_id,
            scene),
        data,
        contract)
    contract_path = audio_path.with_suffix(".contract.json")
    contract_path.write_text(
        json.dumps(
            contract,
            ensure_ascii=False,
            indent=2),
        encoding="utf-8")
    return [register_artifact(job, store.root, contract_path, "audio_contract",
                              scene_id=scene.scene_id, task_id=result.task_id,
                              node_name=result.node_name)]


def _voice_task_for(job, task_id, scene) -> dict:
    """Return the voice-task requirements for a result.

    Issue #197/#165.2: when the Job carries a ``voice_task_snapshot`` the
    requirements are read from the exact snapshot that was issued at enqueue
    time, never rebuilt from the live Character config.  A config change
    between claim and result must be a controlled rejection, not a silent
    re-derivation.  When no snapshot tracking is active (tests that bypass
    the enqueue/claim flow) the task is rebuilt as before.
    """
    snapshot = (job.voice_task_snapshot or {}).get(task_id)
    if snapshot is not None:
        return snapshot
    if job.voice_task_snapshot:
        # Snapshot tracking is active for this Job but no snapshot was issued
        # for this task_id: the result is for a task that was never dispatched
        # to a Worker (or for a task whose claim was lost).  Reject rather
        # than silently rebuild from the live Character config.
        raise ValueError("No issued voice-task snapshot for this result; "
                         "the task was never dispatched or the config changed")
    return _tts_task_for(job, scene)


def _tts_max_retries(job) -> int:
    """Per-scene TTS attempt budget (Issue i.0.0.1.5).

    A dedicated budget is required because the generic ``job.max_retries`` is
    shared with image/assembly/publish and counts *additional* attempts after
    the first failure, while a TTS scene must never loop without bound.  The
    value is pinned per-Job at dispatch time so a retry cannot silently inherit
    a budget that was raised for a different stage.
    """
    try:
        return int(getattr(job, "tts_max_retries", 3))
    except (TypeError, ValueError):
        return 3


def _tts_attempts_used(job, scene) -> int:
    return int((job.tts_attempts or {}).get(scene.scene_id, 0))


def _tts_attempts_remaining(job, scene) -> bool:
    return _tts_attempts_used(job, scene) < _tts_max_retries(job)


def _tts_backoff_seconds(job, scene) -> float:
    """Bounded exponential backoff for a TTS retry (Issue i.0.0.1.5).

    The delay is computed from the *scene's* own attempt count, not from the
    Job-level ``retries`` counter, so a second failing scene never inherits the
    backoff of a first one.  The schedule is capped by ``TTS_RETRY_BACKOFF_CAP``
    so a slow TTS runtime cannot stall the Job indefinitely.
    """
    base = float(os.getenv("TTS_RETRY_BACKOFF_BASE", "2"))
    cap = float(os.getenv("TTS_RETRY_BACKOFF_CAP", "30"))
    attempt = _tts_attempts_used(job, scene)
    return min(cap, base ** max(0, attempt))


def _enqueue_tts_task(job, scene, *, delay: float = 0) -> dict:
    task = _tts_task_for(job, scene)
    if delay:
        task["not_before"] = time.time() + delay
    queued = task_queue.enqueue(task)
    scene.task_id = queued["task_id"]
    job.tts_active_task_ids[queued["task_id"]] = scene.scene_id
    # Issue #197/#165.2: snapshot the exact voice requirements that were
    # issued to the Worker at enqueue time.  Validation later compares the
    # Worker's contract against this snapshot instead of rebuilding the task
    # from the live Character config, so a config change between claim and
    # result cannot silently validate an artifact against different
    # requirements than the ones the Worker was dispatched with.
    job.voice_task_snapshot[queued["task_id"]] = dict(task)
    store.repository.record_task(queued, "QUEUED")
    store.event(
        job,
        f"TTS TASK {queued['task_id']} QUEUED FOR {scene.scene_id}")
    return queued


def _handle_tts_failure(
        job,
        scene,
        task_id: str,
        node_name: str,
        error: str | None) -> None:
    """Apply the separate TTS attempt budget to a failed voice result (Issue i.0.0.1.5).

    A failed TTS scene is retried only while its own scene-scoped budget is not
    exhausted.  The retry is scheduled with a bounded exponential backoff derived
    from the scene's own attempt count, and the delay is carried into the queue
    as ``not_before`` so the dispatcher never hands the same scene to a worker
    before the backoff has elapsed.  Once the budget is spent the TTS stage is
    failed and the scene is dead-lettered; there is no unbounded or instant
    retry path.
    """
    used = _tts_attempts_used(job, scene) + 1
    job.tts_attempts[scene.scene_id] = used
    task_queue.discard(task_id)
    job.tts_active_task_ids.pop(task_id, None)
    job.voice_task_snapshot.pop(task_id, None)
    scene.assigned_worker = None
    # Clear worker state to match voice task worker loss recovery pattern
    worker = store.workers.get(node_name)
    if worker:
        worker.update({"current_job": None, "current_task": None, "status": worker.get(
            "desired_state") or "READY", "last_seen": utc_now()})
        store.save_worker(worker)
    store.repository.record_task(
        _tts_task_for(
            job,
            scene) | {
            "task_id": task_id},
        "FAILED",
        node_name,
        error)
    if used < _tts_max_retries(job):
        delay = _tts_backoff_seconds(job, scene)
        store.event(
            job, f"{scene.scene_id} TTS FAILED, RETRY {used}/{_tts_max_retries(job)} "
            f"(backoff {delay:.1f}s): {error or 'unknown error'}")
        _enqueue_tts_task(job, scene, delay=delay)
        return
    transition_stage(
        job,
        StageName.TTS,
        StageStatus.FAILED,
        error or "unknown error")
    task_queue.dead_letter({"job_id": job.job_id,
                            "task_id": task_id,
                            "task": "voice",
                            "scene_id": scene.scene_id},
                           error)
    store.update(
        job,
        JobStatus.FAILED,
        f"TTS FAILED: {error or 'unknown error'} (budget {_tts_max_retries(job)} exhausted)")
