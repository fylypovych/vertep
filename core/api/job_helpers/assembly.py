"""Video assembly executed by a Worker: snapshot, staging, release and results."""

import base64
import binascii
import hashlib
import shutil
from pathlib import Path

from adapters.providers.base import (
    RELEASE_NOT_APPLIED,
    RELEASE_RELEASED,
    RELEASE_UNCONFIRMED,
)

from ...artifacts import register_artifact
from ...models import utc_now
from ...state import (
    store,
    task_queue,
)
from .common import (
    ASSEMBLY_STAGING_DIR,
    _max_video_artifact_bytes,
)
from .dispatch import _ordered_scene_files


# ---------------------------------------------------------------------------
# Video assembly executed by a Worker (Issue #122 P5/P6/P7)
# ---------------------------------------------------------------------------


def _engine_snapshot(
        job,
        *,
        version: int,
        submit_key: str,
        engine=None) -> dict:
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
    reference = getattr(
        getattr(
            engine,
            "contract_profile",
            None),
        "upstream_reference",
        None)
    if reference:
        snapshot["upstream_reference"] = reference
    if getattr(engine, "engine_id", "native") != "native":
        # Only the *reference* of the endpoint and the secret travels with the task
        # (P7): a Worker can prove it has the same configuration without the CORE
        # ever handing out a credential.
        from ...engine_config import endpoint_reference, secret_reference

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
    from ...engine_config import engine_config_revision as shared_revision

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
    for label, value in (("audio", audio), ("music", music),
                         ("subtitles", subtitles), ("watermark", watermark)):
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


def _job_relative(
        job_root: Path,
        job_id: str,
        path: Path | None) -> str | None:
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
    staged = job_dir / ASSEMBLY_STAGING_DIR / \
        _file_digest(resolved) / resolved.name
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
    shutil.rmtree(
        Path(job_root) /
        job_id /
        ASSEMBLY_STAGING_DIR,
        ignore_errors=True)


def _snapshot_input(
        job_root: Path,
        job_id: str,
        reference: str | None) -> dict | None:
    """One delivered approved input: its Job-relative reference and its digest."""
    if not reference:
        return None
    path = (Path(job_root) / job_id / reference).resolve()
    return {"reference": reference, "sha256": _file_digest(path)}


def _with_delivered_inputs(snapshot: dict,
                           job_root: Path,
                           job_id: str,
                           *,
                           materials: list[str],
                           inputs: dict[str,
                                        str | None]) -> dict:
    """Add what the Worker will actually receive to the attempt snapshot (§9.15).

    A snapshot that names only the engine cannot prove *which* approved inputs the
    render used. Every staged reference therefore travels with its SHA256, so the
    executor verifies the delivered bytes against the approved ones instead of
    trusting whatever happens to be in its Job directory (Issue #122 P3/P5).
    """
    snapshot = dict(snapshot)
    snapshot["materials"] = [
        entry for entry in (
            _snapshot_input(
                job_root,
                job_id,
                reference) for reference in materials) if entry]
    snapshot["inputs"] = {
        label: _snapshot_input(job_root, job_id, reference)
        for label, reference in inputs.items()
    }
    return snapshot


def _assembly_task_for(
        job,
        *,
        version: int,
        output: Path,
        materials: list[Path],
        audio: Path | None,
        music: Path | None,
        subtitles: Path | None,
        watermark: Path | None,
        script: str,
        submit_key: str,
        snapshot: dict,
        durations: list[float],
        aspect_ratio: str,
        preset: str | None,
        task_type: str,
        job_store=None) -> dict:
    job_root = (job_store.root if job_store is not None else store.root)
    references = [_stage_input(job_root, job.job_id, path)
                  for path in materials]
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
        "output": _job_relative(
            job_root,
            job.job_id,
            output),
        "materials": references,
        "audio": delivered["audio"],
        "music": delivered["music"],
        "subtitles": delivered["subtitles"],
        "watermark": delivered["watermark"],
        "durations": [
            float(value) for value in durations],
        "aspect_ratio": aspect_ratio,
        "preset": preset,
        "task_type": task_type,
        "script": script,
        "submit_key": submit_key,
        "engine_snapshot": _with_delivered_inputs(
            snapshot,
            job_root,
            job.job_id,
            materials=references,
            inputs=delivered),
    }


def _enqueue_assembly_task(
        job,
        task: dict,
        *,
        new_attempt: bool = False) -> dict:
    queued = task_queue.enqueue(task, new_attempt=new_attempt)
    job.assembly_task_id = queued["task_id"]
    job.assembly_task_ids[queued["task_id"]] = int(task["version"])
    job.video_engine_snapshot = task.get("engine_snapshot")
    store.repository.record_task(queued, "QUEUED")
    store.event(
        job,
        f"ASSEMBLY TASK {queued['task_id']} QUEUED FOR v{task['version']}")
    return queued


def _release_worker(worker: dict | None) -> None:
    if not worker:
        return
    desired_status = worker.get("desired_state")
    next_status = desired_status if desired_status in {
        "DRAINING", "QUARANTINED"} else "READY"
    worker.update({"status": next_status, "current_job": None,
                  "current_task": None, "last_seen": utc_now()})
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
        from ...engine_config import endpoint_identity

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


def record_assembly_release(
        job,
        *,
        submit_key: str,
        snapshot: dict | None,
        state: str,
        reason: str,
        task_id: str | None = None) -> dict:
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


def release_assembly_hold(
        job,
        submit_key: str,
        *,
        state: str,
        reason: str) -> dict | None:
    """Move a recorded release to a proven state (a late fenced result ended it)."""
    record = job.assembly_releases.get(submit_key)
    if record is None or record.get("state") == state:
        return record
    record = {
        **record,
        "state": state,
        "reason": reason,
        "recorded_at": utc_now()}
    job.assembly_releases[submit_key] = record
    store.repository.save_job(job)
    store.event(
        job,
        f"ASSEMBLY RELEASE {submit_key} {state.upper()}: {reason}")
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
        if (record.get("engine_id") == identity["engine_id"] and str(
                record.get("endpoint_reference") or "") == identity["endpoint_reference"]):
            return {**record, "submit_key": submit_key}
    return None


def engine_release_hold(snapshot: dict | None) -> dict | None:
    """Unproven release of this runtime by *any* Job (§5).

    The compute of a cancelled attempt is a property of the runtime, not of one Job,
    so a second Job must not take the same runtime either.
    """
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
    except Exception:  # noqa: BLE001 - an unreadable state stays unconfirmed
        return record
    if state not in {RELEASE_RELEASED, RELEASE_NOT_APPLIED}:
        return record
    return release_assembly_hold(
        job,
        submit_key,
        state=RELEASE_RELEASED,
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
        raise ValueError(
            f"Assembly result {task_id} does not match a dispatched attempt")
    if not result.get("success"):
        raise ValueError(result.get("error") or "Assembly failed")
    # Cancel check BEFORE any side effect: discard the result without writing
    # the video file or registering a version (Issue #122 §5).
    if job.assembly_cancel_requested:
        raise ValueError("Assembly result discarded: attempt cancelled")
    artifacts = list(artifacts or [])
    if len(artifacts) != 1:
        raise ValueError("Assembly must return exactly one video artifact")
    artifact = artifacts[0]
    contract = artifact.get("contract") or {}
    if contract.get(
            "format") != "media_contract/v1" or contract.get("kind") != "video":
        raise ValueError(
            "Assembly artifact must declare a video media_contract/v1")
    try:
        data = base64.b64decode(
            artifact.get("data_base64") or "",
            validate=True)
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
    if contract.get("video_version") is not None and int(
            contract["video_version"]) != version:
        raise ValueError(
            "Assembly artifact declares a different video version")
    suffix = ".mp4"
    validate_signature(data, suffix)
    validate_media_contract(contract, data, expected="video")
    if any(int(existing.version or 0) ==
           version for existing in job.video_versions):
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
        raise RuntimeError(
            f"Could not import the assembled video: {error}") from error
    register_artifact(
        job,
        store.root,
        path,
        "video",
        task_id=task_id,
        node_name=result.get("worker_name"),
        workflow=f"assembly:{contract.get('engine_id') or (job.video_engine_snapshot or {}).get('engine_id')}")
    from ...pipeline import _finish_video_version

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
    from ...pipeline import assembly_plan

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


def assembly_worker_mismatch(
        reported: dict | None,
        snapshot: dict | None) -> str | None:
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
        from ...engine_config import same_engine_fact
        if not same_engine_fact(name, snapshot.get(name), reported.get(name)):
            return f"{name}_mismatch"
    if reported.get("ready") is not True:
        return f"engine_not_ready:{reported.get('reason') or 'unknown'}"
    return None
