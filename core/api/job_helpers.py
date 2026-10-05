"""Shared job/task/worker helper functions for the job-domain routers.

Business helpers (no route registration) used by ``core.api.jobs``,
``core.api.tasks`` and ``core.api.workers``, as well as by ``core.app`` lifespan
and the watchdog.  Extracted from ``core.app.py`` so the job domain can live in
dedicated router modules instead of one large file.
"""
import base64
import binascii
import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import httpx
from fastapi import HTTPException, Request

from adapters.providers.base import (
    RELEASE_NOT_APPLIED,
    RELEASE_RELEASED,
    RELEASE_UNCONFIRMED,
)
from adapters.telegram import TelegramAdapter

from ..artifacts import register_artifact
from ..configuration import load_character
from ..dispatcher import available_worker, can_retry
from ..models import (JobStatus, SceneRecord, StageName, StageStatus, TaskResult,
                      VideoVersion, utc_now)
from ..orchestration import (all_scenes_ready, finish_scene, initialize_plan,
                             interrupt_scene, pending_scenes, transition_stage)
from ..pipeline import JobStore, finalize_job_safe, prepare_job_safe, queue_storyboard
from ..script_prompt import build_script_prompt
from ..script_schema import normalize_script
from ..state import executor, result_locks, store, task_queue, workflow_registry

MAX_VIDEO_ARTIFACT_BYTES = 268435456

# Attempt-scoped staging area of a Job directory (Issue #122 §6/P3): inputs that
# do not live inside the Job directory are copied here content-addressed, so a
# Worker receives Job-relative references and CORE paths never leave CORE.
ASSEMBLY_STAGING_DIR = ".assembly-staging"


def _max_video_artifact_bytes() -> int:
    return int(os.getenv("MAX_VIDEO_ARTIFACT_BYTES", str(MAX_VIDEO_ARTIFACT_BYTES)))


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
    scene_id = job.active_task_ids.get(task_id) or job.tts_active_task_ids.get(task_id)
    return next((scene for scene in job.scenes if scene.scene_id == scene_id), None)


# ---------------------------------------------------------------------------
# Video assembly executed by a Worker (Issue #122 P5/P6/P7)
# ---------------------------------------------------------------------------


def _engine_snapshot(job, *, version: int, submit_key: str, engine=None) -> dict:
    """Immutable engine/config decision of one assembly attempt (§9.15).

    The snapshot is taken when the attempt is dispatched and travels with the
    task, so a later Settings change (P7) applies to new Jobs only. Secret values
    are never copied into it: only the engine identity, the bridge contract and
    the pinned runtime reference that readiness proved on the executor. The engine
    is passed in by the caller, so the snapshot always describes the very engine
    whose attempt is being dispatched.
    """
    from adapters.providers import providers
    from adapters.providers.base import BRIDGE_SCHEMA_VERSION

    if engine is None:
        engine = providers.video_engine()
    snapshot: dict = {
        "engine_id": getattr(engine, "engine_id", "native"),
        "bridge_schema_version": BRIDGE_SCHEMA_VERSION,
        "config_revision": engine_config_revision(engine),
        "job_id": job.job_id,
        "video_version": version,
        "aspect_ratio": job.aspect_ratio,
        "preset": job.output_preset,
        "task_type": job.task_type,
        "submit_key": submit_key,
    }
    reference = getattr(getattr(engine, "contract_profile", None), "upstream_reference", None)
    if reference:
        snapshot["upstream_reference"] = reference
    if getattr(engine, "engine_id", "native") != "native":
        # Only the *reference* of the endpoint and the secret travels with the task
        # (P7): a Worker can prove it has the same configuration without the CORE
        # ever handing out a credential.
        from ..engine_config import endpoint_reference, secret_reference

        snapshot["endpoint_reference"] = endpoint_reference(engine)
        snapshot["secret_reference"] = secret_reference(engine)
    return snapshot


def engine_config_revision(engine=None) -> str:
    """Revision of the effective engine configuration (Issue #122 P7).

    Thin re-export of the shared implementation in :mod:`core.engine_config`, so
    the revision recorded in a Job snapshot, the one shown by Settings and the one
    a Worker recomputes are the same value by construction. The engine is passed
    through so a snapshot describes the engine it was handed.
    """
    from ..engine_config import engine_config_revision as shared_revision

    return shared_revision(engine)


def _assembly_submit_key(job, *, version: int, input_digest: str) -> str:
    """Durable submit key of one assembly attempt (Issue #122 §5, §9.8).

    The key is derived from the Job, the version and the digest of the approved
    inputs, so the same attempt always reuses it (making a repeated submit
    idempotent in the runtime) while any change of approved input produces a new
    attempt with a new key.
    """
    return f"{job.job_id}-v{version}-{input_digest[:16]}"


def _assembly_input_digest(job, *, materials: list[Path], audio: Path | None,
                           music: Path | None, subtitles: Path | None,
                           watermark: Path | None, script: str) -> str:
    parts = [f"script:{hashlib.sha256(script.encode('utf-8')).hexdigest()}"]
    for label, value in (("audio", audio), ("music", music), ("subtitles", subtitles),
                         ("watermark", watermark)):
        parts.append(f"{label}:{_file_digest(value)}")
    for path in materials:
        parts.append(f"material:{_file_digest(path)}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _file_digest(path: Path | None) -> str:
    if not path:
        return "-"
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _job_relative(job_root: Path, job_id: str, path: Path | None) -> str | None:
    """Job-scoped relative reference delivered to a Worker (§6/P3).

    A Worker never receives a CORE filesystem path: it receives a reference
    relative to the shared Job directory, so the same delivery works on another
    host with a different root.
    """
    if not path:
        return None
    resolved = Path(path).resolve()
    root = Path(job_root).resolve()
    try:
        return resolved.relative_to(root / job_id).as_posix()
    except ValueError as error:
        raise ValueError(
            f"Assembly input {resolved} is outside the Job directory"
        ) from error


def _stage_input(job_root: Path, job_id: str, path: Path | None) -> str | None:
    """Deliver one approved input to a Worker as a Job-relative reference (§6/P3).

    An input that already lives inside the Job directory is referenced as is. An
    input that does not (a shared brand watermark, an image kept by an older
    layout) is copied once into the content-addressed staging area of that Job
    directory, so the Worker still receives a Job-relative reference instead of a
    CORE filesystem path, and a repeated attempt of the same input reuses the
    copy instead of transferring it again.
    """
    if not path:
        return None
    try:
        return _job_relative(job_root, job_id, path)
    except ValueError:
        pass
    resolved = Path(path).resolve()
    job_dir = (Path(job_root) / job_id).resolve()
    staged = job_dir / ASSEMBLY_STAGING_DIR / _file_digest(resolved) / resolved.name
    if not staged.is_file():
        staged.parent.mkdir(parents=True, exist_ok=True)
        temporary = staged.with_name(f"{staged.name}.part")
        shutil.copy2(str(resolved), str(temporary))
        temporary.replace(staged)
    return staged.relative_to(job_dir).as_posix()


def _clear_assembly_staging(job_root: Path, job_id: str) -> None:
    """Remove the staged copies of a Job directory after a terminal attempt (§6/P3).

    Only the staging area is removed; approved outputs, versions and artifacts of
    the Job are never touched.
    """
    shutil.rmtree(Path(job_root) / job_id / ASSEMBLY_STAGING_DIR, ignore_errors=True)


def _snapshot_input(job_root: Path, job_id: str, reference: str | None) -> dict | None:
    """One delivered approved input: its Job-relative reference and its digest."""
    if not reference:
        return None
    path = (Path(job_root) / job_id / reference).resolve()
    return {"reference": reference, "sha256": _file_digest(path)}


def _with_delivered_inputs(snapshot: dict, job_root: Path, job_id: str, *,
                           materials: list[str], inputs: dict[str, str | None]) -> dict:
    """Add what the Worker will actually receive to the attempt snapshot (§9.15).

    A snapshot that names only the engine cannot prove *which* approved inputs the
    render used. Every staged reference therefore travels with its SHA256, so the
    executor verifies the delivered bytes against the approved ones instead of
    trusting whatever happens to be in its Job directory (Issue #122 P3/P5).
    """
    snapshot = dict(snapshot)
    snapshot["materials"] = [
        entry for entry in (_snapshot_input(job_root, job_id, reference) for reference in materials)
        if entry
    ]
    snapshot["inputs"] = {
        label: _snapshot_input(job_root, job_id, reference)
        for label, reference in inputs.items()
    }
    return snapshot


def _assembly_task_for(job, *, version: int, output: Path, materials: list[Path],
                       audio: Path | None, music: Path | None, subtitles: Path | None,
                       watermark: Path | None, script: str, submit_key: str,
                       snapshot: dict, durations: list[float], aspect_ratio: str,
                       preset: str | None, task_type: str) -> dict:
    job_root = store.root
    references = [_stage_input(job_root, job.job_id, path) for path in materials]
    delivered = {
        "audio": _stage_input(job_root, job.job_id, audio),
        "music": _stage_input(job_root, job.job_id, music),
        "subtitles": _stage_input(job_root, job.job_id, subtitles),
        "watermark": _stage_input(job_root, job.job_id, watermark),
    }
    return {
        "job_id": job.job_id,
        "task": "assembly",
        "priority": job.priority,
        "min_vram_mb": 0,
        "workflow": job.workflow or "",
        "topic": job.topic,
        "task_id": None,
        "version": version,
        "output": _job_relative(job_root, job.job_id, output),
        "materials": references,
        "audio": delivered["audio"],
        "music": delivered["music"],
        "subtitles": delivered["subtitles"],
        "watermark": delivered["watermark"],
        "durations": [float(value) for value in durations],
        "aspect_ratio": aspect_ratio,
        "preset": preset,
        "task_type": task_type,
        "script": script,
        "submit_key": submit_key,
        "engine_snapshot": _with_delivered_inputs(
            snapshot, job_root, job.job_id, materials=references, inputs=delivered
        ),
    }


def _enqueue_assembly_task(job, task: dict, *, new_attempt: bool = False) -> dict:
    queued = task_queue.enqueue(task, new_attempt=new_attempt)
    job.assembly_task_id = queued["task_id"]
    job.assembly_task_ids[queued["task_id"]] = int(task["version"])
    job.video_engine_snapshot = task.get("engine_snapshot")
    store.repository.record_task(queued, "QUEUED")
    store.event(job, f"ASSEMBLY TASK {queued['task_id']} QUEUED FOR v{task['version']}")
    return queued


def _release_worker(worker: dict | None) -> None:
    if not worker:
        return
    desired_status = worker.get("desired_state")
    next_status = desired_status if desired_status in {"DRAINING", "QUARANTINED"} else "READY"
    worker.update({"status": next_status, "current_job": None, "current_task": None,
                   "last_seen": utc_now()})
    store.save_worker(worker)


def _assembly_worker_owns(job, task_id: str, node_name: str) -> bool:
    """Fencing: only the node that still owns the attempt may report it (§5)."""
    worker = store.workers.get(node_name)
    return bool(
        worker
        and worker.get("current_task") == task_id
        and worker.get("current_job") == job.job_id
    )


def _engine_identity(snapshot: dict | None) -> dict[str, str]:
    """The runtime identity a release belongs to (P6/§5).

    The hold protects the exact runtime that may still be rendering, so it is keyed
    by the engine together with its endpoint reference: a Settings change that
    repoints the engine describes a different runtime and must not inherit, nor
    silently drop, another runtime's unproven release.
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    reference = snapshot.get("endpoint_reference")
    if isinstance(reference, dict):
        reference = reference.get("endpoint")
    return {
        "engine_id": str(snapshot.get("engine_id") or "native"),
        "endpoint_reference": str(reference or ""),
    }


def _cancel_and_release_state(job, *, submit_key: str) -> tuple[str, str]:
    """Cancel one attempt and report what is proven about its compute (§5, §9.8).

    The abort is addressed to the durable submit key of the attempt, and only to the
    runtime the attempt was actually dispatched with: after a Settings change the
    current engine is a different runtime, and stopping a job on it would prove
    nothing about the runtime that is still rendering. The answer is
    ``released`` only when the runtime says so — the abort was accepted, or its task
    is in a terminal state or unknown to it — and ``unconfirmed`` otherwise.
    """
    snapshot = job.video_engine_snapshot or {}
    identity = _engine_identity(snapshot)
    try:
        from adapters.providers import providers

        engine = providers.video_engine()
    except Exception as error:  # noqa: BLE001 - an unreadable engine stays unconfirmed
        return RELEASE_UNCONFIRMED, f"engine_unavailable:{type(error).__name__}"
    if (str(getattr(engine, "engine_id", "native")) != identity["engine_id"]
            or _engine_endpoint_reference(engine) != identity["endpoint_reference"]):
        return RELEASE_UNCONFIRMED, "engine_changed:attempt_runtime_not_current"
    try:
        if engine.cancel(job.job_id, submit_key=submit_key):
            return RELEASE_RELEASED, "abort_accepted"
        state = engine.release_state(submit_key)
    except Exception as error:  # noqa: BLE001 - silence is not a confirmation
        return RELEASE_UNCONFIRMED, f"cancel_unconfirmed:{type(error).__name__}"
    if state == RELEASE_NOT_APPLIED:
        return RELEASE_NOT_APPLIED, "no_remote_runtime"
    if state == RELEASE_RELEASED:
        return RELEASE_RELEASED, "abort_refused_but_runtime_reports_no_running_task"
    return RELEASE_UNCONFIRMED, "abort_refused_or_still_running"


def _engine_endpoint_reference(engine) -> str:
    """Endpoint identity of an engine, or an empty string for a local render.

    Only the non-secret scheme/host/port identity is compared, which is exactly what
    :func:`core.engine_config.endpoint_identity` publishes for drift detection.
    """
    try:
        from ..engine_config import endpoint_identity

        return str(endpoint_identity(engine) or "")
    except Exception:  # noqa: BLE001 - an unreadable reference never matches a snapshot
        return ""


def cancelled_attempt_release(job, task_id: str) -> dict | None:
    """The recorded release of a cancelled attempt, addressed by its task id (§5).

    A result of a cancelled attempt is the one thing that proves the render ended, so
    the attempt is looked up by its task id instead of being rejected as unknown: the
    result is discarded and its release becomes proven.
    """
    for submit_key, record in (job.assembly_releases or {}).items():
        if record.get("task_id") == task_id:
            return {**record, "submit_key": submit_key}
    return None


def record_assembly_release(job, *, submit_key: str, snapshot: dict | None,
                            state: str, reason: str, task_id: str | None = None) -> dict:
    """Record what is known about the compute of a cancelled attempt (§5).

    ``state`` is one of the ``RELEASE_*`` values of
    :mod:`adapters.providers.base`. Only a ``released`` state removes the runtime
    from the hold; everything else is recorded as ``unconfirmed`` with its reason so
    the decision stays visible in Job state instead of being assumed. The task id is
    kept as well, so a late result of that attempt can later prove that the render
    really ended.
    """
    identity = _engine_identity(snapshot)
    record = {
        **identity,
        "state": state,
        "reason": reason,
        "task_id": task_id or "",
        "recorded_at": utc_now(),
    }
    job.assembly_releases[submit_key] = record
    store.repository.save_job(job)
    store.event(job, f"ASSEMBLY RELEASE {submit_key} {state.upper()}"
                     + (f": {reason}" if reason else ""))
    return record


def release_assembly_hold(job, submit_key: str, *, state: str, reason: str) -> dict | None:
    """Move a recorded release to a proven state (a late fenced result ended it)."""
    record = job.assembly_releases.get(submit_key)
    if record is None or record.get("state") == state:
        return record
    record = {**record, "state": state, "reason": reason, "recorded_at": utc_now()}
    job.assembly_releases[submit_key] = record
    store.repository.save_job(job)
    store.event(job, f"ASSEMBLY RELEASE {submit_key} {state.upper()}: {reason}")
    return record


def unconfirmed_release_for(job, *, snapshot: dict | None) -> dict | None:
    """Why this runtime may not be given another render yet, or ``None`` (§5).

    The lease of a runtime whose release was never proven is not handed to another
    task: the next assembly attempt stays queued with an explicit reason instead of
    competing with work the runtime may still be doing.
    """
    identity = _engine_identity(snapshot)
    for submit_key, record in (job.assembly_releases or {}).items():
        if record.get("state") != RELEASE_UNCONFIRMED:
            continue
        if (record.get("engine_id") == identity["engine_id"]
                and str(record.get("endpoint_reference") or "") == identity["endpoint_reference"]):
            return {**record, "submit_key": submit_key}
    return None


def engine_release_hold(snapshot: dict | None) -> dict | None:
    """Unproven release of this runtime by *any* Job (§5).

    The compute of a cancelled attempt is a property of the runtime, not of one Job,
    so a second Job must not take the same runtime either.
    """
    identity = _engine_identity(snapshot)
    for other in store.jobs.values():
        if not isinstance(getattr(other, "assembly_releases", None), dict):
            continue
        hold = unconfirmed_release_for(other, snapshot=snapshot)
        if hold:
            return {**hold, "job_id": other.job_id}
    return None


def confirm_assembly_release(job, *, submit_key: str) -> dict | None:
    """Resolve one recorded release against the runtime that ran the attempt (§5).

    A confirmation exists only when the runtime itself reports the attempt as free:
    an abort it accepted, a task in a terminal state, or a task it no longer knows.
    Silence is not a confirmation, so an unreachable runtime stays ``unconfirmed``.
    """
    record = job.assembly_releases.get(submit_key)
    if record is None or record.get("state") != RELEASE_UNCONFIRMED:
        return record
    snapshot = dict(job.video_engine_snapshot or {})
    snapshot["engine_id"] = record.get("engine_id")
    if record.get("endpoint_reference"):
        snapshot["endpoint_reference"] = record["endpoint_reference"]
    try:
        from adapters.providers import providers

        engine = providers.video_engine()
        state = engine.release_state(submit_key)
    except Exception as error:  # noqa: BLE001 - an unreadable state stays unconfirmed
        return record
    if state not in {RELEASE_RELEASED, RELEASE_NOT_APPLIED}:
        return record
    return release_assembly_release(
        job, submit_key, state=RELEASE_RELEASED,
        reason="abort_accepted" if state == RELEASE_RELEASED else "no_remote_runtime",
    )


def _handle_assembly_result(job, result: dict, artifacts: list[dict]) -> None:
    """Import a verified render as the immutable video version it was made for.

    Everything is checked before any side effect: the attempt must still be the
    dispatched one, the artifact must be exactly one decodable video whose
    declared contract matches the received bytes and whose version is the one that
    was dispatched. A rejected result leaves the previous version current.
    """
    from core.file_validation import validate_media_contract, validate_signature

    task_id = result["task_id"]
    version = job.assembly_task_ids.get(task_id)
    if version is None:
        raise ValueError(f"Assembly result {task_id} does not match a dispatched attempt")
    if not result.get("success"):
        raise ValueError(result.get("error") or "Assembly failed")
    artifacts = list(artifacts or [])
    if len(artifacts) != 1:
        raise ValueError("Assembly must return exactly one video artifact")
    artifact = artifacts[0]
    contract = artifact.get("contract") or {}
    if contract.get("format") != "media_contract/v1" or contract.get("kind") != "video":
        raise ValueError("Assembly artifact must declare a video media_contract/v1")
    try:
        data = base64.b64decode(artifact.get("data_base64") or "", validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("Assembly artifact is not valid base64") from error
    if not data:
        raise ValueError("Assembly artifact is empty")
    if len(data) > _max_video_artifact_bytes():
        raise ValueError("Assembly artifact exceeds the accepted video size")
    if contract.get("sha256") != hashlib.sha256(data).hexdigest():
        raise ValueError("Assembly artifact sha256 does not match its payload")
    if int(contract.get("size") or 0) != len(data):
        raise ValueError("Assembly artifact size does not match its payload")
    if contract.get("video_version") is not None and int(contract["video_version"]) != version:
        raise ValueError("Assembly artifact declares a different video version")
    suffix = ".mp4"
    validate_signature(data, suffix)
    validate_media_contract(contract, data, expected="video")
    if any(int(existing.version or 0) == version for existing in job.video_versions):
        raise ValueError(f"Video version {version} already exists")

    final_dir = store.root / job.job_id / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    path = final_dir / f"video-v{version}.mp4"
    temporary = final_dir / f".{path.name}.{task_id[:8]}.part"
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Could not import the assembled video: {error}") from error
    record = register_artifact(job, store.root, path, "video", task_id=task_id,
                               node_name=result.get("worker_name"),
                               workflow=f"assembly:{contract.get('engine_id') or (job.video_engine_snapshot or {}).get('engine_id')}")
    from ..pipeline import _finish_video_version

    _finish_video_version(job, store, path, version)
    job.assembly_task_ids.pop(task_id, None)
    if job.assembly_task_id == task_id:
        job.assembly_task_id = None
    _clear_assembly_staging(store.root, job.job_id)


def _assembly_retry_task(job) -> dict | None:
    """Rebuild the identical assembly attempt after a failed or rejected result.

    The approved inputs and the durable submit key stay the same, so a retry can
    never create a second upstream task for the same approved render: the runtime
    answers the repeat submit from its submit record (Issue #122 §5, §9.8). Any
    change of approved input produces a new version, a new key and a new attempt.
    """
    from ..pipeline import assembly_plan

    snapshot = job.video_engine_snapshot or {}
    version = int(snapshot.get("video_version") or 0)
    if not version:
        return None
    materials = _ordered_scene_files(job)
    if not materials:
        return None
    try:
        plan = assembly_plan(store, job, materials)
    except (ValueError, OSError):
        return None
    if plan["version"] != version:
        # The approved inputs changed under the attempt: it is a new version, not a
        # retry, and it must be dispatched with its own key.
        return None
    return _assembly_task_for(job, submit_key=str(snapshot["submit_key"]),
                              snapshot=snapshot, **plan)


#: Every snapshot field the claiming node has to report back unchanged. The attempt was
#: dispatched against one exact engine configuration, so any drift in the engine identity,
#: the bridge contract, the configuration revision or the external references keeps the task
#: queued instead of rendering an approved Job somewhere else. Fields the dispatched
#: snapshot does not pin are skipped: a node cannot be rejected over an undecided fact.
ENGINE_CLAIM_FIELDS = (
    "engine_id",
    "bridge_schema_version",
    "config_revision",
    "upstream_reference",
    "endpoint_reference",
    "secret_reference",
)


def assembly_worker_mismatch(reported: dict | None, snapshot: dict | None) -> str | None:
    """Why this node may not run the dispatched attempt, or ``None`` when it may.

    Issue #122 P5/P7: the attempt was decided against one exact engine configuration
    and must not be rendered anywhere else. CORE compares what the claiming node
    reports about its own effective engine with the snapshot of the attempt, so a
    node with a different revision, a repointed endpoint, a different pinned upstream
    or a runtime that is not ready keeps the task queued instead of producing an
    approved Job. The reported fields are non-secret by construction.
    """
    if not isinstance(reported, dict) or not reported:
        return "engine_state_not_reported"
    if not isinstance(snapshot, dict) or not snapshot:
        return "snapshot_missing"
    for name in ENGINE_CLAIM_FIELDS:
        if snapshot.get(name) in (None, ""):
            continue
        if name not in reported:
            return "engine_state_incomplete"
        if reported.get(name) != snapshot.get(name):
            return f"{name}_mismatch"
    if reported.get("ready") is not True:
        return f"engine_not_ready:{reported.get('reason') or 'unknown'}"
    return None


def _select_worker(workers: list[dict], job, task_type: str | None = None, min_vram_mb: int | None = None,
                   voice_requirements: dict | None = None):
    dispatcher_url = os.getenv("DISPATCHER_URL", "").rstrip("/")
    if not dispatcher_url:
        return available_worker(workers, job, task_type=task_type, min_vram_mb=min_vram_mb,
                                voice_requirements=voice_requirements)
    try:
        payload = {"workers": workers, "job": job.model_dump(mode="json")}
        if task_type:
            payload["task_type"] = task_type
        if min_vram_mb is not None:
            payload["min_vram_mb"] = min_vram_mb
        if voice_requirements:
            payload["voice_requirements"] = voice_requirements
        response = httpx.post(f"{dispatcher_url}/select",
                              json=payload, timeout=3)
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


def _tts_task_for(job, scene) -> dict:
    character = load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), job.character_id)
    voice_config = character.voice or {}
    return {"job_id": job.job_id, "task": "voice", "priority": job.priority,
            "min_vram_mb": 0, "workflow": job.workflow or "",
            "topic": scene.voiceover or scene.prompt or job.topic,
            "task_id": None, "scene_id": scene.scene_id,
            "character_id": job.character_id,
            "voice": voice_config.get("voice"),
            "provider": voice_config.get("provider"),
            "language": voice_config.get("language") or character.language or "uk",
            "model": voice_config.get("model") or voice_config.get("engine"),
            "engine": voice_config.get("engine"),
            "speed": voice_config.get("speed", 150)}


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
        raise ValueError("Audio contract must declare format 'audio_contract/v1'")
    expected = contract.get("sha256")
    if not isinstance(expected, str) or not expected:
        raise ValueError("Audio contract must declare sha256")
    if expected != hashlib.sha256(data).hexdigest():
        raise ValueError("Audio contract sha256 does not match artifact payload")
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
            raise ValueError(f"Audio contract is missing required field {field!r}")
        if str(actual) != str(required):
            raise ValueError(
                f"Audio contract {field!r}={actual!r} does not match task requirement {required!r}")


def _persist_tts_contract(store, job, scene, result, audio_path, data, contract) -> list:
    """Validate and persist a verifiable audio contract for a Voice Worker artifact.

    The contract is produced by the Voice Worker and declares which character
    voice config (provider/voice/...) actually drove the synthesis plus a sha256
    of the audio bytes.  We re-check the digest against what CORE received and
    store the contract as a sidecar artifact so the audio is verifiable after the
    fact.  Validation is performed by :func:`_validate_tts_contract` before any
    file is written here.
    """
    _validate_tts_contract(_tts_task_for(job, scene), data, contract)
    contract_path = audio_path.with_suffix(".contract.json")
    contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    return [register_artifact(job, store.root, contract_path, "audio_contract",
                              scene_id=scene.scene_id, task_id=result.task_id,
                              node_name=result.node_name)]


def _enqueue_tts_task(job, scene) -> dict:
    task = _tts_task_for(job, scene)
    queued = task_queue.enqueue(task)
    scene.task_id = queued["task_id"]
    job.tts_active_task_ids[queued["task_id"]] = scene.scene_id
    store.repository.record_task(queued, "QUEUED")
    store.event(job, f"TTS TASK {queued['task_id']} QUEUED FOR {scene.scene_id}")
    return queued


def _script_task_for(job, system_prompt: str, character: dict | None, revision: str | None = None) -> dict:
    return {"job_id": job.job_id, "task": "script", "priority": job.priority,
            "topic": job.topic, "task_id": None,
            "system_prompt": system_prompt, "character": character or {},
            "task_type": "text", "revision": revision}


def _enqueue_script_task(job_store, job, system_prompt: str = "", character: dict | None = None, revision: str | None = None) -> dict | None:
    workers = list(job_store.workers.values())
    has_text_worker = any(
        "text" in (worker.get("supported_tasks") or []) or worker.get("role") == "text"
        for worker in workers
    )
    if not has_text_worker and os.getenv("LOCAL_WORKER_FALLBACK", "false").lower() == "true":
        _generate_script_local(job_store, job, system_prompt, character)
        return None
    task = _script_task_for(job, system_prompt, character, revision)
    queued = task_queue.enqueue(task)
    job.script_task_id = queued["task_id"]
    job.active_task_id = queued["task_id"]
    job_store.repository.record_task(queued, "QUEUED")
    job_store.event(job, f"SCRIPT TASK {queued['task_id']} QUEUED")
    return queued


def _load_character_prompt(job) -> tuple[str, dict | None]:
    character = load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), job.character_id)
    system_prompt = (character.system_prompt or "").strip()
    character_dict = character.model_dump() if character else None
    return system_prompt, character_dict


def _generate_script_local(job_store, job, system_prompt: str, character: dict | None) -> None:
    from ..script_agent import ScriptAgent
    script = ScriptAgent().generate_script(job.topic, system_prompt, character)
    job.script = normalize_script(script, job.topic)
    job.script_attempt = 0
    job.script_error = None
    job_store.update(job, JobStatus.SCRIPT_GENERATING, "SCRIPT GENERATING (LOCAL FALLBACK)")
    _write_script_files(job_store, job)
    job_store.update(job, JobStatus.SCRIPT_PENDING_APPROVAL, "SCRIPT GENERATED (LOCAL FALLBACK)")


def _write_script_files(store, job) -> None:
    if job.script:
        (store.root / job.job_id / "script.json").write_text(json.dumps(job.script, indent=2), encoding="utf-8")
        (store.root / job.job_id / "metadata.json").write_text(json.dumps({
            "title": job.script.get("title", job.topic), "language": "uk",
            "source": job.source, "character_id": job.character_id
        }, indent=2), encoding="utf-8")


def _handle_script_result(store, job, result, artifacts) -> None:
    success = result.get("success", False)
    task_id = result.get("task_id")
    worker_name = result.get("worker_name")

    if task_id and job.script_task_id == task_id:
        job.script_task_id = None

    if not success:
        error = result.get("error") or "UNKNOWN SCRIPT GENERATION ERROR"
        job.script_error = error
        store.event(job, f"SCRIPT TASK FAILED: {error}")
        job.script_attempt = (job.script_attempt or 0) + 1
        if job.script_attempt <= job.max_retries:
            system_prompt, character = _load_character_prompt(job)
            _enqueue_script_task(store, job, system_prompt, character, revision=job.revision)
        else:
            task_queue.dead_letter({"job_id": job.job_id, "task_id": task_id, "task": "script"},
                                   error)
            store.update(job, JobStatus.SCRIPT_FAILED, f"SCRIPT FAILED AFTER {job.script_attempt} ATTEMPTS")
        return

    script_artifact = next((a for a in artifacts if a.get("kind") == "script"), None)
    if not script_artifact:
        store.event(job, "SCRIPT TASK COMPLETED BUT NO SCRIPT ARTIFACT")
        job.script_attempt = (job.script_attempt or 0) + 1
        if job.script_attempt <= job.max_retries:
            system_prompt, character = _load_character_prompt(job)
            _enqueue_script_task(store, job, system_prompt, character, revision=job.revision)
        else:
            store.update(job, JobStatus.SCRIPT_FAILED, "SCRIPT FAILED: NO ARTIFACT AFTER RETRIES")
            task_queue.dead_letter({"job_id": job.job_id, "task_id": task_id, "task": "script"},
                                   "NO SCRIPT ARTIFACT AFTER RETRIES")
        return

    try:
        import base64
        data = base64.b64decode(script_artifact["data_base64"])
        data_dict = json.loads(data.decode("utf-8"))
        job.script = normalize_script(data_dict, job.topic)
    except (KeyError, ValueError, UnicodeDecodeError) as exc:
        store.event(job, f"SCRIPT ARTIFACT MALFORMED: {exc}")
        job.script_attempt = (job.script_attempt or 0) + 1
        if job.script_attempt <= job.max_retries:
            system_prompt, character = _load_character_prompt(job)
            _enqueue_script_task(store, job, system_prompt, character, revision=job.revision)
        else:
            store.update(job, JobStatus.SCRIPT_FAILED, f"SCRIPT FAILED: MALFORMED ARTIFACT AFTER RETRIES")
            task_queue.dead_letter({"job_id": job.job_id, "task_id": task_id, "task": "script"},
                                   f"MALFORMED ARTIFACT: {exc}")
        return

    job.active_task_id = None
    job.script_attempt = 0
    job.script_error = None
    _write_script_files(store, job)
    store.update(job, JobStatus.SCRIPT_PENDING_APPROVAL, "SCRIPT GENERATED; AWAITING APPROVAL")
    if job.source.startswith("telegram:"):
        from ..pipeline import _send_script_approval_to_telegram
        try:
            _send_script_approval_to_telegram(job)
        except Exception as exc:
            store.event(job, f"TELEGRAM SCRIPT APPROVAL NOTIFICATION FAILED: {exc}")


def _has_publisher_worker(job_store) -> bool:
    return any(
        "publish" in (worker.get("supported_tasks") or []) or worker.get("role") == "publisher"
        for worker in job_store.workers.values()
    )


def _publish_task_for(job, channel: str) -> dict:
    """Build a publish task carrying the durable delivery contract.

    The contract pins the exact video version (path + sha256 + size) that the
    Publisher Worker must upload, so a retry after a lost ack or a restart
    re-publishes the same artifact instead of a stale/different one.  The
    contract also declares the receipt correlation keys (job_id, version,
    channel) that CORE uses to match an incoming receipt back to the intent.
    """
    delivery = _publish_delivery_contract(job)
    intent = {
        "job_id": job.job_id,
        "channel": channel,
        "video_version": delivery.get("version"),
        "video_sha256": delivery.get("sha256"),
        "video_size": delivery.get("size"),
        "attempt": (job.publish_retry_count.get(channel, 0) + 1),
    }
    job.publish_intent[channel] = intent
    return {"job_id": job.job_id, "task": "publish", "priority": job.priority,
            "channel": channel, "topic": job.topic, "task_id": None,
            "video_path": job.output_path or "",
            "delivery_contract": delivery,
            "publish_intent": intent,
            "metadata": {"job_id": job.job_id, "topic": job.topic,
                         "character_id": job.character_id, "brand_id": job.brand_id,
                         **(job.script or {})},
            "task_type": "publish"}


def _publish_delivery_contract(job) -> dict:
    """Compute the immutable delivery contract for the current job video.

    Returns a dict with the version, path, sha256, size and mime type of the
    artifact the Publisher Worker must upload.  Missing files degrade to a
    best-effort contract (path only) so a retry still targets the same path.
    """
    path = job.output_path or ""
    contract = {"version": job.active_video_version, "path": path,
                "mime_type": "video/mp4"}
    if path and os.path.isfile(path):
        try:
            data = Path(path).read_bytes()
            contract["sha256"] = hashlib.sha256(data).hexdigest()
            contract["size"] = len(data)
        except OSError:
            contract["sha256"] = None
            contract["size"] = None
    else:
        contract["sha256"] = None
        contract["size"] = None
    job.publish_delivery_contract = contract
    return contract


def _match_publish_receipt(job, channel: str, receipt: dict) -> tuple[str, str | None]:
    """Correlate a receipt with the durable publish intent.

    Returns (decision, reason) where decision is one of:
      - "accept"   — receipt matches the intent (or no intent was recorded yet,
                     e.g. a direct test/helper call) and the channel is not yet published
      - "skip"     — channel already published with an identical version (idempotent)
      - "reject"   — receipt does not match the intent (wrong version/owner/lease)
    """
    intent = job.publish_intent.get(channel)
    if intent:
        receipt_version = receipt.get("video_version") or receipt.get("version")
        if receipt_version is not None and intent.get("video_version") is not None \
                and str(receipt_version) != str(intent["video_version"]):
            return "reject", (f"Receipt video_version {receipt_version!r} does not match "
                              f"intent version {intent['video_version']!r}")
        receipt_sha = receipt.get("video_sha256") or receipt.get("sha256")
        if receipt_sha and intent.get("video_sha256") and receipt_sha != intent["video_sha256"]:
            return "reject", "Receipt video_sha256 does not match delivery contract"
    if channel in job.published_channels:
        return "skip", "Channel already published (idempotent)"
    return "accept", None


def _enqueue_publish_task(job_store, job, channel: str) -> dict | None:
    has_publisher = _has_publisher_worker(job_store)
    if not has_publisher and os.getenv("LOCAL_WORKER_FALLBACK", "false").lower() == "true":
        _publish_local(job_store, job, channel)
        return None
    task = _publish_task_for(job, channel)
    queued = task_queue.enqueue(task)
    job.publish_task_ids[queued["task_id"]] = channel
    job_store.repository.record_task(queued, "QUEUED")
    job_store.event(job, f"PUBLISH TASK {queued['task_id']} QUEUED FOR {channel}")
    return queued


def _publish_local(job_store, job, channel: str) -> dict:
    from ..pipeline import _do_publish_single
    result = _do_publish_single(job, channel)
    job.publication_results[channel] = result
    if result.get("status") == "PUBLISHED":
        channel_str = ",".join(result.get("channels", [channel]))
        job.published_to.append(channel_str)
    elif result.get("status") == "NOT_CONFIGURED":
        job.publish_error = result.get("error", "NOT_CONFIGURED")
    job.publish_attempt = 0
    job_store.event(job, f"PUBLISHED {channel}: {result.get('status')}")
    return result


def _validate_publication_receipt(receipt: dict, expected_channel: str) -> tuple[bool, str | None]:
    """Validate a publication receipt schema and channel match.

    Returns (is_valid, error_message). Receipt must have:
    - channel (matches expected)
    - status (PUBLISHED, FAILED, NOT_CONFIGURED)
    - remote_id (for PUBLISHED)
    - url (for PUBLISHED)
    - timestamp
    - error (for FAILED/NOT_CONFIGURED)
    """
    if not isinstance(receipt, dict):
        return False, "Receipt must be a dict"
    if receipt.get("channel") != expected_channel:
        return False, f"Receipt channel mismatch: expected {expected_channel}, got {receipt.get('channel')}"
    status = receipt.get("status")
    if status not in {"PUBLISHED", "FAILED", "NOT_CONFIGURED"}:
        return False, f"Invalid receipt status: {status}"
    if not isinstance(receipt.get("timestamp"), (int, float)):
        return False, "Receipt must have numeric timestamp"
    if status == "PUBLISHED":
        if not receipt.get("remote_id"):
            return False, "PUBLISHED receipt must have remote_id"
        if not receipt.get("url"):
            return False, "PUBLISHED receipt must have url"
    elif status in {"FAILED", "NOT_CONFIGURED"}:
        if not receipt.get("error"):
            return False, f"{status} receipt must have error"
    return True, None


def _handle_publish_result(job_store, job, result, artifacts, channel: str) -> None:
    success = result.get("success", False)
    task_id = result.get("task_id")
    if task_id and job.publish_task_ids.get(task_id) == channel:
        job.publish_task_ids.pop(task_id, None)
    if job.active_task_id == task_id:
        job.active_task_id = None

    # Initialize per-channel retry tracking
    if not hasattr(job, 'publish_retry_count'):
        job.publish_retry_count = {}
    if channel not in job.publish_retry_count:
        job.publish_retry_count[channel] = 0

    # Initialize published channels set for idempotency
    if not hasattr(job, 'published_channels'):
        job.published_channels = set()

    def _backoff_seconds(attempt: int) -> float:
        """Bounded exponential backoff for transient channel failures."""
        base = float(os.getenv("PUBLISH_RETRY_BACKOFF_BASE", "2"))
        cap = float(os.getenv("PUBLISH_RETRY_BACKOFF_CAP", "60"))
        return min(cap, base ** max(0, attempt))

    def _enqueue_with_backoff(reason: str) -> None:
        """Retry a failed publish channel with bounded exponential backoff.

        ``max_retries`` counts *additional* attempts after the first failure, so
        ``max_retries=0`` fails immediately and ``max_retries=2`` allows two
        retries (three total attempts).  The counter is incremented only when a
        retry is actually scheduled, so a lost ack does not burn a retry slot.
        """
        attempt = job.publish_retry_count.get(channel, 0) + 1
        if attempt <= job.max_retries:
            delay = _backoff_seconds(attempt)
            job.publish_retry_count[channel] = attempt
            job_store.event(job, f"PUBLISH {channel}: RETRY {attempt}/{job.max_retries} "
                                  f"(backoff {delay:.1f}s): {reason}")
            job_store.update(job, JobStatus.PUBLISHING,
                             f"PUBLISH RETRY {attempt}/{job.max_retries}: {channel} ({reason})")
            _enqueue_publish_task(job_store, job, channel)
        else:
            job.publish_retry_count[channel] = attempt
            job.publication_results.setdefault(channel, {"channel": channel, "status": "FAILED",
                                                          "error": f"{reason} AFTER RETRIES"})
            job_store.update(job, JobStatus.FAILED, f"PUBLISH {channel} FAILED: {reason} AFTER RETRIES")

    if success:
        receipt_artifact = next((a for a in artifacts if a.get("kind") == "publication_receipt"), None)
        if not receipt_artifact:
            job_store.event(job, f"PUBLISH {channel}: NO RECEIPT ARTIFACT")
            job.publish_error = "NO RECEIPT ARTIFACT"
            _enqueue_with_backoff("no receipt")
            return

        import base64 as _b64
        try:
            data = _b64.b64decode(receipt_artifact["data_base64"])
            receipt = json.loads(data.decode("utf-8"))
        except (KeyError, ValueError, UnicodeDecodeError) as exc:
            job_store.event(job, f"PUBLISH {channel}: MALFORMED RECEIPT: {exc}")
            job.publish_error = f"MALFORMED RECEIPT: {exc}"
            _enqueue_with_backoff(f"malformed receipt: {exc}")
            return

        # Validate receipt schema
        valid, error = _validate_publication_receipt(receipt, channel)
        if not valid:
            job_store.event(job, f"PUBLISH {channel}: INVALID RECEIPT: {error}")
            job.publish_error = f"INVALID RECEIPT: {error}"
            _enqueue_with_backoff(f"invalid receipt: {error}")
            return

        # Correlate the receipt with the durable publish intent (owner/lease/version).
        decision, reason = _match_publish_receipt(job, channel, receipt)
        if decision == "reject":
            job_store.event(job, f"PUBLISH {channel}: RECEIPT REJECTED: {reason}")
            job.publish_error = f"RECEIPT REJECTED: {reason}"
            job.publication_results.setdefault(channel, {"channel": channel, "status": "FAILED",
                                                          "error": f"RECEIPT REJECTED: {reason}"})
            job_store.update(job, JobStatus.FAILED, f"PUBLISH {channel} FAILED: RECEIPT REJECTED")
            return
        if decision == "skip":
            job_store.event(job, f"PUBLISH {channel}: ALREADY PUBLISHED (idempotent skip)")
            return

        receipt_status = receipt.get("status")
        job.publication_results[channel] = receipt

        if receipt_status == "PUBLISHED":
            job.published_channels.add(channel)
            if channel not in job.published_to:
                job.published_to.append(channel)
            job.publish_retry_count[channel] = 0
            job.publish_intent.pop(channel, None)
            job_store.update(job, JobStatus.PUBLISHING, f"PUBLISH TASK {task_id} COMPLETED FOR {channel}")
        elif receipt_status == "NOT_CONFIGURED":
            job.publish_error = receipt.get("error", "NOT_CONFIGURED")
            job.publish_retry_count[channel] = 0
            job.publish_intent.pop(channel, None)
            job_store.event(job, f"PUBLISH {channel} NOT CONFIGURED; NO RETRY")
            return
        else:  # FAILED — permanent or transient; retry with bounded backoff
            job.publish_error = receipt.get("error", "UNKNOWN")
            _enqueue_with_backoff(f"FAILED: {job.publish_error}")
    else:
        error = result.get("error") or "UNKNOWN PUBLISH ERROR"
        job.publish_error = error
        job_store.event(job, f"PUBLISH TASK FAILED for {channel}: {error}")
        _enqueue_with_backoff(error)


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
        elif job and job.script_task_id == current_task and job.status in {JobStatus.SCRIPT_QUEUED, JobStatus.SCRIPT_GENERATING}:
            task_queue.release(current_task)
            job.script_task_id = None
            job.assigned_worker = None
            system_prompt, character = _load_character_prompt(job)
            _enqueue_script_task(store, job, system_prompt, character, revision=job.revision)
            store.event(job, f"{worker.get('node_name')} OFFLINE; SCRIPT TASK {current_task} REQUEUED")
            worker["current_job"] = None
            worker["current_task"] = None
        elif job and current_task in job.publish_task_ids and job.status == JobStatus.PUBLISHING:
            task_queue.release(current_task)
            channel = job.publish_task_ids.pop(current_task, None)
            job.assigned_worker = None
            _enqueue_publish_task(store, job, channel)
            store.event(job, f"PUBLISH TASK {current_task} REQUEUED FOR {channel}")
            worker["current_job"] = None
            worker["current_task"] = None
        elif job and current_task in job.tts_active_task_ids and job.status == JobStatus.TTS_GENERATING:
            # Deterministic worker-loss recovery for Voice tasks: release the
            # lease, mark the scene lost and re-dispatch the remaining TTS so
            # the Job is never silently dropped.
            scene = next((s for s in job.scenes if s.scene_id == job.tts_active_task_ids.get(current_task)), None)
            task_queue.release(current_task)
            job.tts_active_task_ids.pop(current_task, None)
            job.assigned_worker = None
            if scene:
                scene.assigned_worker = None
                interrupt_scene(scene, f"Worker {worker.get('node_name')} heartbeat timed out")
            _dispatch_tts(store, job)
            store.event(job, f"{worker.get('node_name')} OFFLINE; TTS TASK {current_task} REQUEUED")
            worker["current_job"] = None
            worker["current_task"] = None
        elif job and job.storyboard_task_id == current_task and job.status in {JobStatus.STORYBOARD_QUEUED,
                                                                               JobStatus.STORYBOARD_GENERATING}:
            # Worker loss during storyboard generation: release the lease and
            # re-queue the storyboard so the Job stays on the lifecycle path.
            task_queue.release(current_task)
            job.storyboard_task_id = None
            job.assigned_worker = None
            from ..pipeline import queue_storyboard as _queue_storyboard
            _queue_storyboard(store, job, revision=job.video_revision_upstream)
            store.event(job, f"{worker.get('node_name')} OFFLINE; STORYBOARD TASK {current_task} REQUEUED")
            worker["current_job"] = None
            worker["current_task"] = None
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
            store.event(job, f"{worker.get('node_name')} OFFLINE; ASSEMBLY TASK "
                             f"{current_task} REQUEUED")
            retry = _assembly_retry_task(job) if job.assembly_attempt <= job.max_retries else None
            if retry is not None:
                _enqueue_assembly_task(job, retry, new_attempt=True)
            else:
                transition_stage(job, StageName.ASSEMBLY, StageStatus.FAILED,
                                 "assembly worker lost")
                store.update(job, JobStatus.FAILED,
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
                interrupt_scene(scene, f"Worker {worker.get('node_name')} heartbeat timed out")
            store.event(job, f"{worker.get('node_name')} OFFLINE; TASK {current_task} RELEASED")
            executor.submit(_prepare_and_dispatch, job)
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
    if os.getenv("LOCAL_WORKER_FALLBACK", "false").lower() == "true":
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
        character = load_character(Path(os.getenv("CHARACTERS_ROOT", "characters")), job.character_id)
        provider = (character.voice or {}).get("provider")
    except Exception:
        return False
    return bool(provider) and str(provider).strip().lower() not in {"disabled", "off", "false"}


def _has_voice_worker(store: JobStore) -> bool:
    return any(
        "speech_synthesis" in (worker.get("capabilities") or []) or worker.get("role") == "voice"
        for worker in store.workers.values()
    )


def _dispatch_tts(store, job) -> None:
    initialize_plan(job)
    if job.stages[StageName.TTS.value].status == StageStatus.PENDING:
        transition_stage(job, StageName.TTS, StageStatus.RUNNING)
    if _pending_voice_scenes(job) and not _character_voice_enabled(job):
        transition_stage(job, StageName.TTS, StageStatus.READY)
        if job.task_type in {"image", "video"}:
            store.transition(job, JobStatus.ASSETS_READY, "TTS SKIPPED; VOICE DISABLED")
        return
    if not job.tts_active_task_ids and not _pending_voice_scenes(job):
        transition_stage(job, StageName.TTS, StageStatus.READY)
        if job.task_type in {"image", "video"}:
            store.transition(job, JobStatus.ASSETS_READY, "TTS SKIPPED; NO VOICEOVER")
        return
    queued = [_enqueue_tts_task(job, scene)
              for scene in _pending_voice_scenes(job)]


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
        _PRE_STORYBOARD = {JobStatus.SCRIPT_APPROVED, JobStatus.STORYBOARD_QUEUED,
                           JobStatus.STORYBOARD_GENERATING, JobStatus.STORYBOARD_FAILED}
        if job.status in _PRE_STORYBOARD:
            return False
        if not job.storyboards or not job.active_storyboard_version:
            store.event(job, "STORYBOARD GATE: No active storyboard; asset generation blocked")
            return True
        sb = next((s for s in job.storyboards if s.version == job.active_storyboard_version), None)
        if sb is None:
            store.event(job, f"STORYBOARD GATE: Active storyboard {job.active_storyboard_version} not found; asset generation blocked")
            return True
        if sb.image_status != "approved":
            from ..image_storyboard import queue_image_storyboard
            if not all(s.image_artifact_id for s in sb.scenes):
                queue_image_storyboard(store, job, sb.version)
                if os.getenv("LOCAL_WORKER_FALLBACK", "false").lower() == "true":
                    from ..image_storyboard import handle_image_result as _handle
                    import base64
                    demo_ppm = b"P6\n2 2\n255\n" + bytes((80, 120, 90)) * 4
                    b64 = base64.b64encode(demo_ppm).decode()
                    for tid in list(job.image_storyboard_task_ids.keys()):
                        _handle(store, job, tid, True, image_base64=b64)
                        from ..state import task_queue as _tq
                        _tq.ack(tid)
                    store.event(job, f"IMAGE STORYBOARD {sb.version}:{sb.image_version} READY (fallback)")
            store.event(job, f"IMAGE STORYBOARD {sb.version} NOT APPROVED (status={sb.image_status}); waiting for approval")
            return True
    return False


def _prepare_and_dispatch(job) -> None:
    while True:
        if job.script and job.status == JobStatus.NEW:
            initialize_plan(job)
            assets_stage = job.stages[StageName.ASSETS.value]
            if assets_stage.status in {StageStatus.PENDING, StageStatus.FAILED, StageStatus.PAUSED}:
                transition_stage(job, StageName.ASSETS, StageStatus.RUNNING)
            # Issue #36/#60: gate asset generation until image storyboard approved
            if _image_storyboard_gate(store, job):
                return
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
    if job.status in {JobStatus.VIDEO_PENDING_APPROVAL, JobStatus.VIDEO_REVISION_REQUESTED}:
        _progress(job, job.status.value)
        return
    if job.status == JobStatus.SCRIPT_APPROVED:
        # Issue #81 R1: a free-text video revision routed through script
        # regeneration is applied again to the storyboard that follows.
        queue_storyboard(store, job, revision=job.video_revision_upstream)
        return
    if job.status == JobStatus.STORYBOARD_APPROVED:
        sb = next((s for s in job.storyboards if s.version == job.active_storyboard_version), None)
        if sb and sb.image_status != "approved":
            store.event(job, f"IMAGE STORYBOARD NOT APPROVED (status={sb.image_status}); enqueue preview images")
            from ..image_storyboard import queue_image_storyboard
            if not all(s.image_artifact_id for s in sb.scenes):
                queue_image_storyboard(store, job, sb.version)
                if os.getenv("LOCAL_WORKER_FALLBACK", "false").lower() == "true":
                    from ..image_storyboard import handle_image_result as _handle
                    import base64
                    demo_ppm = b"P6\n2 2\n255\n" + bytes((80, 120, 90)) * 4
                    b64 = base64.b64encode(demo_ppm).decode()
                    for tid in list(job.image_storyboard_task_ids.keys()):
                        _handle(store, job, tid, True, image_base64=b64)
                        from ..state import task_queue as _tq
                        _tq.ack(tid)
                    store.event(job, f"IMAGE STORYBOARD {sb.version}:{sb.image_version} READY (fallback)")
            # No explicit image approval yet — wait for user action (Issue #6 rule)
            return
        # Issue #36/#60: gate asset generation until image storyboard approved
        if _image_storyboard_gate(store, job):
            return
        store.transition(job, JobStatus.ASSET_GENERATION, "ASSET GENERATION STARTED")
        _dispatch_assets(store, job)
        return
    if job.status == JobStatus.ASSET_GENERATION:
        # Issue #36/#60: gate asset generation until image storyboard approved
        if _image_storyboard_gate(store, job):
            return
        _dispatch_assets(store, job)
    if job.status == JobStatus.TTS_GENERATING:
        _dispatch_tts(store, job)


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
        for task_id, scene_id in list(job.tts_active_task_ids.items()):
            scene = next((item for item in job.scenes if item.scene_id == scene_id), None)
            worker = scene.assigned_worker if scene else None
            if worker:
                task_queue.request_cancel(worker, task_id)
            task_queue.discard(task_id)
            store.repository.record_task(_tts_task_for(job, scene) | {"task_id": task_id},
                                         task_status, worker)
            if scene:
                scene.task_id = None
        if job.storyboard_task_id:
            task_id = job.storyboard_task_id
            worker = job.assigned_worker
            if worker:
                task_queue.request_cancel(worker, task_id)
            task_queue.discard(task_id)
            store.repository.record_task({"job_id": job.job_id, "task_id": task_id, "task": "storyboard"},
                                         task_status, worker)
            job.storyboard_task_id = None
        if job.script_task_id:
            task_id = job.script_task_id
            worker = job.assigned_worker
            if worker:
                task_queue.request_cancel(worker, task_id)
            task_queue.discard(task_id)
            store.repository.record_task({"job_id": job.job_id, "task_id": task_id, "task": "script"},
                                         task_status, worker)
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
                    state, reason = _cancel_and_release_state(job, submit_key=submit_key)
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
                transition_stage(job, StageName.ASSEMBLY,
                                 StageStatus.CANCELLED if status == JobStatus.CANCELLED
                                 else StageStatus.PAUSED)
        for task_id, channel in list(job.publish_task_ids.items()):
            worker = job.assigned_worker
            if worker:
                task_queue.request_cancel(worker, task_id)
            task_queue.discard(task_id)
            store.repository.record_task({"job_id": job.job_id, "task_id": task_id, "task": "publish",
                                           "channel": channel}, task_status, worker)
        job.publish_task_ids.clear()
        job.active_task_ids.clear()
        job.tts_active_task_ids.clear()
        job.active_task_id = None
        job.assigned_worker = None
        if job.stages and job.stages[StageName.ASSETS.value].status == StageStatus.RUNNING:
            transition_stage(job, StageName.ASSETS,
                             StageStatus.CANCELLED if status == JobStatus.CANCELLED else StageStatus.PAUSED)
        if job.stages and job.stages[StageName.TTS.value].status == StageStatus.RUNNING:
            transition_stage(job, StageName.TTS,
                             StageStatus.CANCELLED if status == JobStatus.CANCELLED else StageStatus.PAUSED)
        if job.stages and job.stages.get(StageName.SCRIPT.value) and job.stages[StageName.SCRIPT.value].status == StageStatus.RUNNING:
            transition_stage(job, StageName.SCRIPT,
                             StageStatus.CANCELLED if status == JobStatus.CANCELLED else StageStatus.PAUSED)
    elif status == JobStatus.NEW:
        for scene in job.scenes:
            if scene.status == StageStatus.PAUSED:
                scene.status = StageStatus.PENDING
        if job.stages:
            if job.stages[StageName.ASSETS.value].status == StageStatus.PAUSED:
                transition_stage(job, StageName.ASSETS, StageStatus.RUNNING)
            if job.stages[StageName.TTS.value].status == StageStatus.PAUSED:
                transition_stage(job, StageName.TTS, StageStatus.RUNNING)
    return store.update(job, status, event)
