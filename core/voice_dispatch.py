"""Voice preview/synthesis through Worker dispatch (Issue i.0.0.4.70, #470).

CORE never synthesizes speech itself (AGENTS.md §3.1/§3.5): the Web UI preview
and the synthesis endpoint enqueue a job-less ``voice_preview`` task into the
regular task queue. A Voice Worker claims it through ``/api/tasks/claim``, runs
the ordinary ``execute_role_task()`` voice executor (TTS runtime via the Worker)
and returns the audio artifact with its ``audio_contract/v1`` through
``/api/tasks/result``. The HTTP request waits for that result.

There is no CORE fallback: without a ready Voice Worker the request fails
explicitly instead of calling the TTS runtime from CORE.
"""

from __future__ import annotations

import base64
import hashlib
import os
import threading
import uuid
from datetime import datetime, timezone

from .dispatcher import available_worker
from .models import Job, JobStatus

TASK_KIND = "voice_preview"
# Synthetic owner of a preview task: the claim/renew/result paths recognise the
# task by ``kind``; no Job with this id ever exists.
PREVIEW_JOB_ID = "voice-preview"
_DEFAULT_VOICE = "default"


class VoiceDispatchError(Exception):
    """A preview/synthesis that the Worker path could not deliver."""

    status_code = 502

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class VoiceWorkerUnavailable(VoiceDispatchError):
    status_code = 503


class VoiceDispatchTimeout(VoiceDispatchError):
    status_code = 504


class _Pending:
    __slots__ = ("task", "event", "audio", "error", "node_name")

    def __init__(self, task: dict) -> None:
        self.task = task
        self.event = threading.Event()
        self.audio: bytes | None = None
        self.error: str | None = None
        self.node_name: str | None = None


_pending: dict[str, _Pending] = {}
_lock = threading.Lock()


def _timeout_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("VOICE_PREVIEW_TIMEOUT_SECONDS", "180")))
    except ValueError:
        return 180.0


def _selection_job() -> Job:
    """A transient Job used only for the shared Worker selection rules."""
    return Job(job_id=PREVIEW_JOB_ID, topic="voice preview", character_id="",
               priority=5, status=JobStatus.NEW, task_type="voice",
               created_at=datetime.now(timezone.utc).isoformat())


def requirements(task: dict) -> dict:
    """Voice readiness requirements of a preview task.

    The engine's own ``default`` voice is not a catalog entry, so it is not
    demanded from the Worker catalog. The provider identity is fenced before
    dispatch by the executor-config checks of the preview endpoint.
    """
    wanted = {"voice": task.get("voice"), "model": task.get("model"),
              "language": task.get("language"), "speed": task.get("speed")}
    if wanted["voice"] == _DEFAULT_VOICE:
        wanted["voice"] = None
    return {key: value for key, value in wanted.items() if value}


def is_preview_task(task: dict | None) -> bool:
    return bool(task) and task.get("kind") == TASK_KIND


def worker_for(workers: list[dict], task: dict) -> dict | None:
    """The ready Voice Worker among ``workers`` that may run ``task``."""
    return available_worker(workers, _selection_job(), task_type="voice",
                            min_vram_mb=0, voice_requirements=requirements(task) or None)


def build_task(text: str, voice: str, speed: int, *,
               language: str | None = None, model: str | None = None) -> dict:
    task = {
        "task_id": f"voice-preview-{uuid.uuid4().hex}",
        "job_id": PREVIEW_JOB_ID,
        "kind": TASK_KIND,
        "task": "voice",
        "priority": 9,
        "min_vram_mb": 0,
        "workflow": "",
        "topic": text,
        "voice": voice,
        "speed": int(speed),
        "scene_id": None,
        "character_id": None,
    }
    if language:
        task["language"] = language
    if model:
        task["model"] = model
    return task


def _verify(task: dict, artifacts: list) -> bytes:
    audio = next((item for item in artifacts or []
                  if isinstance(item, dict) and item.get("kind") == "audio"), None)
    if audio is None:
        raise VoiceDispatchError("Voice Worker returned no audio")
    try:
        data = base64.b64decode(audio.get("data_base64") or "", validate=True)
    except (ValueError, TypeError) as error:
        raise VoiceDispatchError("Voice Worker returned unreadable audio") from error
    if not data:
        raise VoiceDispatchError("Voice Worker returned no audio")
    contract = audio.get("contract")
    if not isinstance(contract, dict) or contract.get("format") != "audio_contract/v1":
        raise VoiceDispatchError("Voice Worker returned audio without a contract")
    if contract.get("sha256") != hashlib.sha256(data).hexdigest():
        raise VoiceDispatchError("Audio contract sha256 does not match the audio")
    for key in ("voice", "language", "model", "speed"):
        expected = task.get(key)
        if expected in (None, ""):
            continue
        observed = contract.get(key)
        if str(observed).strip().lower() != str(expected).strip().lower():
            raise VoiceDispatchError(f"Voice Worker synthesized with another {key}")
    return data


def synthesize(text: str, voice: str, speed: int, *,
               language: str | None = None, model: str | None = None,
               timeout: float | None = None) -> bytes:
    """Dispatch one synthesis to a Voice Worker and wait for its audio."""
    from .state import store, task_queue
    from .system_state import dispatch_allowed

    task = build_task(text, voice, speed, language=language, model=model)
    if not dispatch_allowed():
        raise VoiceWorkerUnavailable("Dispatch is paused; voice synthesis is unavailable")
    if worker_for(list(store.workers.values()), task) is None:
        raise VoiceWorkerUnavailable("No ready Voice Worker is available for voice synthesis")
    pending = _Pending(task)
    with _lock:
        _pending[task["task_id"]] = pending
    task_queue.enqueue(task)
    try:
        if not pending.event.wait(timeout if timeout is not None else _timeout_seconds()):
            raise VoiceDispatchTimeout("Voice Worker did not return audio in time")
        if pending.error is not None:
            raise VoiceDispatchError(f"Voice synthesis failed on the Worker: {pending.error}")
        return pending.audio or b""
    finally:
        with _lock:
            _pending.pop(task["task_id"], None)
        if not pending.event.is_set():
            task_queue.discard(task["task_id"])
            if pending.node_name:
                task_queue.request_cancel(pending.node_name, task["task_id"])
                release_worker(pending.node_name, task["task_id"])


def is_pending(task_id: str) -> bool:
    with _lock:
        return task_id in _pending


def claimed(task_id: str, node_name: str) -> bool:
    """Bind a pending preview to the claiming node; ``False`` when abandoned."""
    with _lock:
        pending = _pending.get(task_id)
        if pending is None or pending.event.is_set():
            return False
        pending.node_name = node_name
        return True


def owner(task_id: str) -> str | None:
    with _lock:
        pending = _pending.get(task_id)
        return pending.node_name if pending else None


def complete(task_id: str, node_name: str, *, success: bool, artifacts: list,
             error: str | None) -> bool:
    """Deliver a Worker result to the waiting request; ``False`` when stale."""
    with _lock:
        pending = _pending.get(task_id)
        if pending is None or pending.event.is_set() or pending.node_name != node_name:
            return False
    if success:
        try:
            pending.audio = _verify(pending.task, artifacts)
        except VoiceDispatchError as rejection:
            pending.error = rejection.message
    else:
        pending.error = str(error or "unknown Worker error")[:500]
    pending.event.set()
    return True


def fail(task_id: str, reason: str) -> None:
    with _lock:
        pending = _pending.get(task_id)
    if pending is not None and not pending.event.is_set():
        pending.error = reason
        pending.event.set()


def release_worker(node_name: str, task_id: str) -> None:
    from .models import utc_now
    from .state import store

    worker = store.workers.get(node_name)
    if not worker or worker.get("current_task") != task_id:
        return
    desired = worker.get("desired_state")
    worker.update(status=desired if desired in {"DRAINING", "QUARANTINED"} else "READY",
                  current_job=None, current_task=None, last_seen=utc_now())
    store.save_worker(worker)
