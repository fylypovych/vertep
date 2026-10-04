"""Issue #122 P5/P6: one dispatch, one Worker attempt, one imported version.

The external engine is executed by a Worker, never inside CORE (P5). This module
covers the whole path CORE owns:

* ``finalize_job`` dispatches an assembly task instead of rendering locally, and
  delivers only Job-relative references (P3/P5);
* the attempt carries an immutable engine/config snapshot and one durable submit
  key (P6/P7), and the same key is reused by a retry of identical inputs;
* claim, renew, result import, fencing, rejection, retry, cancellation and stale
  recovery of that attempt (P5/P6).

Nothing here calls an external engine: the Worker side is faked at the boundary of
``worker.role_executor.execute_assembly``, which is what a real node runs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import zlib
from pathlib import Path

import pytest
from fastapi import HTTPException

from core.api import job_helpers, tasks as tasks_api
from core.artifacts import register_artifact
from core.models import (Job, JobStatus, StageName, StageStatus, TaskClaim, TaskRenew,
                         TaskResult, utc_now)
from core.pipeline import JobStore, finalize_job
from core.queue import TaskQueue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _png(path: Path) -> Path:
    """A real, decodable PNG so media validation is exercised end to end."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    width = height = 2
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes((30 + 40 * y,) * 3) * width for y in range(height))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return path


def _mp4(path: Path, size: int = 512) -> Path:
    """A minimal but structurally valid MP4 (ftyp + payload) for signature checks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + bytes(size))
    return path


class _ExternalEngine:
    """A stand-in external engine: identifiable, and never rendering in CORE."""

    engine_id = "money-printer"
    provider = "money-printer"
    name = "money-printer"

    def __init__(self) -> None:
        self.rendered = 0

    def render(self, *args, **kwargs):  # pragma: no cover - must never be called
        self.rendered += 1
        raise AssertionError("CORE must not execute an external engine")

    def capabilities(self) -> dict:
        return {"can_render": True}

    def cancel(self, job_id=None, *, submit_key=None) -> bool:
        return True


class _UnidentifiableEngine:
    """An engine without a usable id: CORE keeps its Native control-plane route."""

    engine_id = None
    provider = "native"

    def render(self, output: Path, **kwargs) -> Path:
        return _mp4(Path(output))


class _Request:
    """Minimal request stand-in; no worker token is configured in the tests."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.client = None
        self.url = type("U", (), {"path": "/api/tasks"})()


def _job(job_id: str = "job-122", status: JobStatus = JobStatus.VIDEO_GENERATION) -> Job:
    return Job(
        job_id=job_id, topic="MoneyPrinter bridge", character_id="test_char", priority=5,
        status=status, created_at=utc_now(), source="web", aspect_ratio="16:9",
        task_type="video",
        script={"title": "Approved", "scenes": [
            {"prompt": "Shot", "text": "Approved first line", "duration": 3},
        ]},
    )


@pytest.fixture
def assembly_state(tmp_path, monkeypatch):
    """Isolate CORE state: a local queue and a JobStore whose Job has one scene."""
    job = _job()
    job_dir = tmp_path / "jobs" / job.job_id
    for name in ("references", "images", "video", "audio", "subtitles", "final", "frames",
                 "storyboard"):
        (job_dir / name).mkdir(parents=True, exist_ok=True)
    store = JobStore(root=str(tmp_path / "jobs"), repository=None)
    store.jobs[job.job_id] = job

    scene_clip = _mp4(job_dir / "video" / "scene-000.mp4")
    artifact = register_artifact(job, store.root, scene_clip, "video_scene", workflow="ffmpeg")
    job.scenes = job.scenes or []
    from core.orchestration import initialize_plan

    initialize_plan(job)
    job.scenes[0].artifact_ids.append(artifact.artifact_id)
    job.scenes[0].status = StageStatus.READY

    queue = TaskQueue()
    monkeypatch.setattr(job_helpers, "store", store)
    monkeypatch.setattr(job_helpers, "task_queue", queue)
    monkeypatch.setattr(tasks_api, "store", store)
    monkeypatch.setattr(tasks_api, "task_queue", queue)
    return {"store": store, "job": job, "queue": queue, "tmp_path": tmp_path,
            "materials": [scene_clip]}


def _dispatch(state, engine=None) -> Job:
    """Run finalize_job with the given effective engine."""
    store, job = state["store"], state["job"]
    monkey = pytest.MonkeyPatch()
    chosen = engine if engine is not None else _ExternalEngine()
    monkey.setattr("core.pipeline.providers", type("_P", (), {
        "video_engine": staticmethod(lambda: chosen),
    }))
    try:
        finalize_job(store, job, state["materials"])
    finally:
        monkey.undo()
    return store.jobs[job.job_id]


def _register_worker(store, name: str = "gpu-01") -> dict:
    worker = {
        "node_name": name, "role": "gpu", "status": "READY", "desired_state": None,
        "capabilities": ["video_assembly", "video_generation", "image_generation"],
        "supported_tasks": ["image", "video", "assembly"], "current_job": None,
        "current_task": None, "last_seen": utc_now(),
    }
    store.workers[name] = worker
    store.save_worker(worker)
    return worker


def _claim(name: str = "gpu-01") -> dict:
    return tasks_api.claim_task(TaskClaim(node_name=name, capabilities=[]), _Request())


def _artifact_for(task_id: str, data: bytes, version: int = 1) -> dict:
    return {
        "kind": "video",
        "filename": f"video-v{version}.mp4",
        "data_base64": base64.b64encode(data).decode("ascii"),
        "contract": {
            "format": "media_contract/v1", "kind": "video", "task_type": "video",
            "mime_type": "video/mp4", "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "workflow": "money-printer", "video_version": version,
            "engine_id": "money-printer",
        },
    }


def _result(job_id: str, task_id: str, data: bytes, *, version: int = 1,
            node: str = "gpu-01") -> TaskResult:
    return TaskResult(job_id=job_id, task_id=task_id, node_name=node, success=True,
                      artifacts=[_artifact_for(task_id, data, version)])


# ---------------------------------------------------------------------------
# P5: dispatch to a Worker, never execution in CORE
# ---------------------------------------------------------------------------


def test_external_engine_dispatches_to_a_worker_without_rendering_in_core(assembly_state):
    store = assembly_state["store"]
    engine = _ExternalEngine()

    finalized = _dispatch(assembly_state, engine)

    assert engine.rendered == 0
    assert finalized.status == JobStatus.ASSEMBLY
    task_id = finalized.assembly_task_id
    assert task_id and task_id in finalized.assembly_task_ids
    # The decision is immutable and travels with the attempt.
    snapshot = finalized.video_engine_snapshot
    assert snapshot["engine_id"] == "money-printer"
    assert snapshot["video_version"] == 1
    assert snapshot["submit_key"]
    assert store.workers == {}, "no Worker is pre-assigned by CORE"
    assert assembly_state["queue"].find(task_id)["task"] == "assembly"


def test_dispatched_task_carries_only_job_relative_references(assembly_state):
    finalized = _dispatch(assembly_state)
    task = assembly_state["queue"].find(finalized.assembly_task_id)

    assert task["materials"] == ["video/scene-000.mp4"]
    assert task["output"] == "final/video-v1.mp4"
    for reference in [task["output"], *task["materials"], task["audio"], task["music"],
                      task["subtitles"], task["watermark"]]:
        if reference is None:
            continue
        assert not Path(reference).is_absolute()
        assert ".." not in Path(reference).parts
    # The approved narration is exactly the approved text; nothing is invented.
    assert task["script"] == "Approved first line"
    assert task["durations"] == [3.0]


def test_an_input_outside_the_job_directory_is_staged_not_leaked(assembly_state, tmp_path,
                                                                 monkeypatch):
    """A shared input becomes a Job-relative reference instead of a CORE path."""
    store, job = assembly_state["store"], assembly_state["job"]
    brand_root = tmp_path / "brands" / job.brand_id
    brand_root.mkdir(parents=True, exist_ok=True)
    watermark = _png(brand_root / "watermark.png")
    (brand_root / "brand.json").write_text(
        '{"metadata": {"watermark": "%s"}}' % watermark.as_posix(), encoding="utf-8",
    )
    monkeypatch.setenv("BRANDS_ROOT", str(tmp_path / "brands"))

    finalized = _dispatch(assembly_state)
    task = assembly_state["queue"].find(finalized.assembly_task_id)

    assert task["watermark"]
    assert not Path(task["watermark"]).is_absolute()
    assert task["watermark"].startswith(job_helpers.ASSEMBLY_STAGING_DIR)
    staged = store.root / job.job_id / task["watermark"]
    assert staged.is_file() and staged.read_bytes() == watermark.read_bytes()


def test_an_engine_without_a_usable_id_keeps_the_native_route(assembly_state):
    """CORE never dispatches an attempt it cannot identify (P5)."""
    finalized = _dispatch(assembly_state, _UnidentifiableEngine())

    assert finalized.assembly_task_ids == {}
    assert finalized.active_video_version == 1, "the Native control-plane route still ran"


# ---------------------------------------------------------------------------
# P5/P6: claim, renew, result
# ---------------------------------------------------------------------------


def test_a_worker_claims_the_attempt_and_fences_the_result(assembly_state):
    store, job = assembly_state["store"], assembly_state["job"]
    _register_worker(store)
    finalized = _dispatch(assembly_state)
    task_id = finalized.assembly_task_id

    claimed = _claim()
    assert claimed["task"]["task_id"] == task_id
    assert store.workers["gpu-01"]["current_task"] == task_id

    data = _mp4(assembly_state["tmp_path"] / "render.mp4").read_bytes()

    with pytest.raises(HTTPException) as refusal:
        tasks_api.task_result(_result(job.job_id, task_id, data, node="gpu-02"), _Request())
    assert refusal.value.status_code == 409
    assert store.jobs[job.job_id].active_video_version is None

    accepted = tasks_api.task_result(_result(job.job_id, task_id, data), _Request())
    assert accepted.status == JobStatus.VIDEO_PENDING_APPROVAL
    assert accepted.active_video_version == 1
    assert accepted.video_versions[0].sha256 == hashlib.sha256(data).hexdigest()
    imported = store.root / job.job_id / accepted.video_versions[0].path
    assert imported.is_file() and imported.read_bytes() == data
    # Staged inputs are released once the render was accepted.
    assert not (store.root / job.job_id / job_helpers.ASSEMBLY_STAGING_DIR).exists()


def test_a_result_for_another_version_is_rejected_and_keeps_the_current_one(assembly_state):
    store, job = assembly_state["store"], assembly_state["job"]
    _register_worker(store)
    first = _dispatch(assembly_state)
    _claim()
    v1 = _mp4(assembly_state["tmp_path"] / "v1.mp4", 600).read_bytes()
    tasks_api.task_result(_result(job.job_id, first.assembly_task_id, v1), _Request())
    assert store.jobs[job.job_id].active_video_version == 1

    # A revision produces version 2; a Worker that reports version 9 is refused.
    job = store.jobs[job.job_id]
    job.status = JobStatus.VIDEO_REVISION_REQUESTED
    second = _dispatch(assembly_state)
    assert second.assembly_task_ids[second.assembly_task_id] == 2
    _register_worker(store)
    _claim()
    v2 = _mp4(assembly_state["tmp_path"] / "v2.mp4", 700).read_bytes()
    rejected = tasks_api.task_result(
        _result(job.job_id, second.assembly_task_id, v2, version=9), _Request())

    assert rejected.active_video_version == 1, "a rejected result must not become current"
    assert not (store.root / job.job_id / "final" / "video-v2.mp4").exists()
    assert any("REJECTED" in event for event in rejected.events)
    # The rejected attempt is retried with the same durable key, so the runtime
    # cannot create a second upstream task for this render.
    retried = assembly_state["queue"].find(
        next(iter(rejected.assembly_task_ids), ""))
    assert retried is not None
    assert retried["submit_key"] == second.video_engine_snapshot["submit_key"]


def test_a_retry_of_identical_inputs_reuses_the_same_submit_key(assembly_state):
    finalized = _dispatch(assembly_state)
    first = assembly_state["queue"].find(finalized.assembly_task_id)
    key = finalized.video_engine_snapshot["submit_key"]

    retry = job_helpers._assembly_retry_task(assembly_state["store"].jobs[finalized.job_id])

    assert retry is not None
    assert retry["submit_key"] == key
    assert retry["version"] == first["version"]


def test_a_changed_input_produces_a_new_attempt_key(assembly_state):
    first_key = _dispatch(assembly_state).video_engine_snapshot["submit_key"]

    job = assembly_state["store"].jobs[assembly_state["job"].job_id]
    job.status = JobStatus.VIDEO_REVISION_REQUESTED
    job.script["scenes"][0]["text"] = "Approved but revised line"
    second_key = _dispatch(assembly_state).video_engine_snapshot["submit_key"]

    assert second_key != first_key


def test_a_failed_attempt_is_retried_and_then_fails_terminally(assembly_state):
    store, job = assembly_state["store"], assembly_state["job"]
    job.max_retries = 1
    _register_worker(store)
    _dispatch(assembly_state)
    key = store.jobs[job.job_id].video_engine_snapshot["submit_key"]

    seen_keys = []
    for _ in range(3):
        current = store.jobs[job.job_id]
        if current.status == JobStatus.FAILED:
            break
        _register_worker(store)
        claimed = _claim()
        if claimed["task"] is None:
            break
        task_id = claimed["task"]["task_id"]
        seen_keys.append(current.video_engine_snapshot["submit_key"])
        tasks_api.task_result(
            TaskResult(job_id=job.job_id, task_id=task_id, node_name="gpu-01",
                       success=False, error="engine refused the approved input"),
            _Request())

    assert store.jobs[job.job_id].status == JobStatus.FAILED
    assert seen_keys and set(seen_keys) == {key}, f"one render keeps one key: {seen_keys}"
    assert store.jobs[job.job_id].stages[StageName.ASSEMBLY.value].status == StageStatus.FAILED
    # No retry will ever reuse the staged copies of a terminally failed Job.
    assert not (store.root / job.job_id / job_helpers.ASSEMBLY_STAGING_DIR).exists()


def test_cancellation_fences_the_attempt_and_refuses_a_late_result(assembly_state):
    store, job = assembly_state["store"], assembly_state["job"]
    _register_worker(store)
    finalized = _dispatch(assembly_state)
    task_id = finalized.assembly_task_id
    _claim()

    job_helpers._job_action(job.job_id, JobStatus.CANCELLED, "JOB CANCELLED")

    cancelled = store.jobs[job.job_id]
    assert cancelled.status == JobStatus.CANCELLED
    assert cancelled.assembly_cancel_requested is True
    assert cancelled.assembly_task_ids == {}

    late = _mp4(assembly_state["tmp_path"] / "late.mp4").read_bytes()
    with pytest.raises(HTTPException) as refusal:
        tasks_api.task_result(_result(job.job_id, task_id, late), _Request())
    assert refusal.value.status_code == 409
    assert store.jobs[job.job_id].active_video_version is None
    assert not (store.root / job.job_id / "final" / "video-v1.mp4").exists()


def test_a_restart_drops_the_stale_attempt_and_replays_the_same_key(assembly_state):
    from core.orchestration import recover_after_restart

    store, job = assembly_state["store"], assembly_state["job"]
    _register_worker(store)
    finalized = _dispatch(assembly_state)
    key = finalized.video_engine_snapshot["submit_key"]

    recovered = recover_after_restart(store.jobs[job.job_id])

    assert recovered.assembly_task_ids == {}
    assert recovered.assembly_task_id is None
    assert recovered.video_engine_snapshot["submit_key"] == key, \
        "a replayed render keeps its submit key, so the runtime stays idempotent"


# ---------------------------------------------------------------------------
# P7: the Worker refuses a drifted effective configuration
# ---------------------------------------------------------------------------


def _worker_task(**overrides) -> dict:
    task = {
        "job_id": "job-122", "task_id": "tsk-assembly-1", "task": "assembly",
        "version": 1, "output": "final/video-v1.mp4",
        "materials": ["video/scene-000.mp4"], "durations": [3.0],
        "aspect_ratio": "16:9", "task_type": "video", "script": "Approved first line",
        "submit_key": "job-122-v1-abc",
        "engine_snapshot": {"engine_id": "money-printer", "submit_key": "job-122-v1-abc"},
    }
    task.update(overrides)
    return task


def _worker_job_dir(tmp_path, monkeypatch) -> Path:
    from worker import role_executor

    monkeypatch.setenv("JOB_ROOT", str(tmp_path / "jobs"))
    (tmp_path / "jobs" / "job-122" / "final").mkdir(parents=True, exist_ok=True)
    _mp4(tmp_path / "jobs" / "job-122" / "video" / "scene-000.mp4")
    monkeypatch.setattr(role_executor, "providers", type("_P", (), {
        "video_engine": staticmethod(_ExternalEngine),
    }))
    return tmp_path


def test_worker_refuses_an_attempt_whose_engine_drifted(tmp_path, monkeypatch):
    from worker import role_executor

    _worker_job_dir(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="does not match the dispatched snapshot"):
        role_executor.execute_assembly(
            _worker_task(engine_snapshot={"engine_id": "shortgpt"}))


def test_worker_refuses_a_reference_that_escapes_the_job_directory(tmp_path, monkeypatch):
    from worker import role_executor

    _worker_job_dir(tmp_path, monkeypatch)
    _mp4(tmp_path / "outside" / "secret.mp4")

    with pytest.raises(RuntimeError, match="must be a Job-scoped relative path"):
        role_executor.execute_assembly(_worker_task(materials=["../../outside/secret.mp4"]))


def test_worker_renders_the_dispatched_version_and_returns_a_verifiable_contract(
    tmp_path, monkeypatch
):
    from worker import role_executor

    _worker_job_dir(tmp_path, monkeypatch)
    seen: dict = {}

    class _Renderer(_ExternalEngine):
        def render(self, output: Path, **kwargs) -> Path:
            seen.update(kwargs)
            seen["output"] = Path(output)
            return _mp4(Path(output))

    monkeypatch.setattr(role_executor, "providers", type("_P", (), {
        "video_engine": staticmethod(_Renderer),
    }))

    artifacts = role_executor.execute_assembly(
        _worker_task(version=2, output="final/video-v2.mp4", submit_key="job-122-v2-abc",
                     engine_snapshot={"engine_id": "money-printer",
                                      "submit_key": "job-122-v2-abc"}))

    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact["contract"]["video_version"] == 2
    assert artifact["contract"]["engine_id"] == "money-printer"
    assert artifact["contract"]["sha256"] == hashlib.sha256(
        base64.b64decode(artifact["data_base64"])).hexdigest()
    # The Worker renders into an attempt-scoped temporary file: CORE promotes the
    # verified bytes into the immutable version itself.
    assert seen["output"].name.startswith(".") and seen["output"].name.endswith(".part")
    assert not (tmp_path / "jobs" / "job-122" / "final" / "video-v2.mp4").exists()
    assert seen["submit_key"] == "job-122-v2-abc"


def test_renew_requires_the_owning_worker(assembly_state):
    store, job = assembly_state["store"], assembly_state["job"]
    _register_worker(store)
    task_id = _dispatch(assembly_state).assembly_task_id
    _claim()

    with pytest.raises(HTTPException) as refusal:
        tasks_api.renew_task(TaskRenew(node_name="gpu-02", task_id=task_id), _Request())
    assert refusal.value.status_code == 409

    assert tasks_api.renew_task(TaskRenew(node_name="gpu-01", task_id=task_id), _Request())["renewed"]


# ---------------------------------------------------------------------------
# P7: the snapshot describes the engine it was handed, with references only
# ---------------------------------------------------------------------------


def test_the_snapshot_records_the_dispatched_engine_and_its_revision(assembly_state, monkeypatch):
    monkeypatch.setenv("MONEY_PRINTER_URL", "http://user:secret@runtime:8098/internal")
    monkeypatch.setenv("MONEY_PRINTER_TOKEN", "token-value")

    engine = _ExternalEngine()
    engine._url = "http://user:secret@runtime:8098/internal"
    finalized = _dispatch(assembly_state, engine)

    snapshot = finalized.video_engine_snapshot
    assert snapshot["engine_id"] == "money-printer"
    # The revision describes the engine this attempt was dispatched with, even
    # though the global registry still resolves the Native engine.
    assert snapshot["config_revision"] == job_helpers.engine_config_revision(engine)
    assert snapshot["config_revision"] != job_helpers.engine_config_revision()
    # Only references travel with the task: no endpoint credential, no token.
    assert snapshot["endpoint_reference"] == {
        "env": "MONEY_PRINTER_URL", "endpoint": "http://runtime:8098", "configured": True}
    assert snapshot["secret_reference"] == {
        "env": "MONEY_PRINTER_TOKEN", "configured": True, "source": "env"}
    serialized = json.dumps(snapshot)
    assert "secret" not in serialized.replace("secret_reference", "") \
        and "token-value" not in serialized


def test_a_native_attempt_snapshot_carries_no_external_reference(assembly_state, monkeypatch):
    monkeypatch.setattr("core.pipeline.providers", type("_P", (), {
        "video_engine": staticmethod(lambda: _UnidentifiableEngine()),
    }))

    finalized = _dispatch(assembly_state, _UnidentifiableEngine())

    snapshot = finalized.video_engine_snapshot
    assert snapshot["engine_id"] == "native"
    assert "endpoint_reference" not in snapshot
    assert "secret_reference" not in snapshot


def test_a_switch_during_an_active_attempt_does_not_change_the_dispatched_snapshot(
    assembly_state, monkeypatch
):
    """P8: the effective configuration may change while an attempt is in flight.

    The attempt keeps the engine and the revision it was dispatched with — its snapshot
    is immutable, and a Worker already holding it is not re-routed. The next attempt is
    decided by the configuration that is effective at that moment.
    """
    store = assembly_state["store"]
    job = assembly_state["job"]
    monkeypatch.setenv("MONEY_PRINTER_URL", "http://runtime:8098")
    monkeypatch.setenv("MONEY_PRINTER_TOKEN", "token")

    first_engine = _ExternalEngine()
    first_engine._url = "http://runtime:8098"
    dispatched = _dispatch(assembly_state, first_engine)
    first_revision = dispatched.video_engine_snapshot["config_revision"]
    assert first_revision
    _register_worker(store)
    claim = _claim()["task"]
    assert claim["task_id"]
    in_flight = assembly_state["queue"].find(claim["task_id"])
    assert in_flight["engine_snapshot"]["engine_id"] == "money-printer"
    assert in_flight["engine_snapshot"]["config_revision"] == first_revision

    # The operator applies a different configuration in Settings while the attempt runs.
    monkeypatch.setenv("MONEY_PRINTER_URL", "http://other-runtime:8098")
    second_engine = _ExternalEngine()
    second_engine._url = "http://other-runtime:8098"

    assert assembly_state["queue"].find(claim["task_id"])["engine_snapshot"] == \
        in_flight["engine_snapshot"], \
        "an attempt that was already dispatched keeps the snapshot it was given"

    job.status = JobStatus.VIDEO_REVISION_REQUESTED
    later = _dispatch(assembly_state, second_engine)
    assert later.video_engine_snapshot["engine_id"] == "money-printer"
    assert later.video_engine_snapshot["config_revision"] != first_revision, \
        "a new attempt is decided by the configuration that is effective now"
    assert job.assembly_task_id != claim["task_id"]


# ---------------------------------------------------------------------------
# P9: the external engine passes through the same approval gates and history
# ---------------------------------------------------------------------------


class _RecordingPublisher:
    """A Publisher stand-in: the route may reach it only through a queued task."""

    def __init__(self) -> None:
        self.published: list[Path] = []
        self.channels = ["youtube"]

    def available_channels(self) -> list[str]:
        return list(self.channels)

    def configured(self, channel: str) -> bool:
        return True

    def publish(self, channel: str, output: Path, metadata: dict | None = None, **kwargs):
        self.published.append(Path(output))
        return {"status": "PUBLISHED", "channel": channel}

    def cancel(self, *args, **kwargs) -> bool:  # pragma: no cover - not used here
        return True


def _publish_route(assembly_state, monkeypatch):
    """Point the publish route at the isolated store, queue and publisher of this test."""
    from core.api import jobs as jobs_api

    publisher = _RecordingPublisher()
    monkeypatch.setattr(jobs_api, "store", assembly_state["store"])
    monkeypatch.setattr(jobs_api, "task_queue", assembly_state["queue"])
    monkeypatch.setattr(jobs_api, "providers", type("_P", (), {
        "publisher": staticmethod(lambda: publisher),
    }))
    monkeypatch.setattr(job_helpers, "task_queue", assembly_state["queue"])
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    return jobs_api.publish_job, publisher


def _queued_tasks(queue) -> list[dict]:
    """Every task the isolated local queue currently holds, ready or in flight."""
    return [row[2] for row in queue._local] + [entry[1] for entry in queue._inflight.values()]


def _publish_tasks(assembly_state, job) -> list[dict]:
    return [task for task in _queued_tasks(assembly_state["queue"])
            if task.get("task_type") == "publish" and task.get("job_id") == job.job_id]


def _publish_contract(assembly_state, job) -> dict:
    """The delivery contract of the publish task the route queued for this Job."""
    tasks = _publish_tasks(assembly_state, job)
    assert len(tasks) == 1, f"expected one publish task for {job.job_id}, got {len(tasks)}"
    return tasks[0]["delivery_contract"]


def _render_v(assembly_state, job, data: bytes, node: str = "gpu-01") -> Job:
    """Claim and accept the Job's current external render."""
    store = assembly_state["store"]
    _register_worker(store, node)
    current = job.assembly_task_id
    claimed = _claim(node)["task"]
    assert claimed is not None, "the dispatched assembly attempt was not claimable"
    assert claimed["task_id"] == current, \
        f"CORE offered attempt {claimed['task_id']} instead of the current {current}"
    return tasks_api.task_result(
        _result(job.job_id, current, data,
                version=job.assembly_task_ids[current], node=node),
        _Request())


def test_an_external_render_cannot_be_published_before_approval(assembly_state, monkeypatch):
    """P9: the external engine does not own publication and cannot skip approval.

    A Worker may return a finished render, but the imported version stops at
    ``VIDEO_PENDING_APPROVAL``; publishing is refused until a reviewer accepted that
    exact version.
    """
    from core.pipeline import approve_video

    store, job = assembly_state["store"], assembly_state["job"]
    publish_job, _publisher = _publish_route(assembly_state, monkeypatch)
    accepted = _render_v(assembly_state, _dispatch(assembly_state),
                         _mp4(assembly_state["tmp_path"] / "v1.mp4").read_bytes())

    assert accepted.status == JobStatus.VIDEO_PENDING_APPROVAL
    with pytest.raises(HTTPException) as refusal:
        publish_job(job.job_id)
    assert refusal.value.status_code == 409
    assert _publish_tasks(assembly_state, job) == [], \
        "an unapproved external render was handed to the Publisher"

    approve_video(store, store.jobs[job.job_id], "reviewer")
    publish_job(job.job_id)
    delivery = _publish_contract(assembly_state, store.jobs[job.job_id])
    assert delivery["version"] == 1
    assert delivery["sha256"] == accepted.video_versions[0].sha256


def test_only_the_current_approved_artifact_is_publishable(assembly_state, monkeypatch):
    """P9: an approval covers one version; a newer render invalidates it.

    While the newer version waits for approval nothing is handed to the Publisher, and
    the delivery contract that finally leaves CORE pins the approved version — never
    the earlier one that the previous approval covered.
    """
    from core.pipeline import approve_video, request_video_revision

    store, job = assembly_state["store"], assembly_state["job"]
    publish_job, _publisher = _publish_route(assembly_state, monkeypatch)
    first = _render_v(assembly_state, _dispatch(assembly_state),
                      _mp4(assembly_state["tmp_path"] / "v1.mp4", 600).read_bytes())
    v1_hash = first.video_versions[0].sha256
    request_video_revision(store, first, "regenerate", "reviewer")
    second = _render_v(assembly_state, _dispatch(assembly_state),
                       _mp4(assembly_state["tmp_path"] / "v2.mp4", 700).read_bytes())
    v2_hash = second.video_versions[1].sha256

    assert second.status == JobStatus.VIDEO_PENDING_APPROVAL
    with pytest.raises(HTTPException) as refusal:
        publish_job(job.job_id)
    assert refusal.value.status_code == 409
    assert "approval" in refusal.value.detail
    assert _publish_tasks(assembly_state, job) == [], \
        "the superseded version was handed to the Publisher while v2 waits for approval"

    approve_video(store, second, "reviewer")
    publish_job(job.job_id)
    delivery = _publish_contract(assembly_state, store.jobs[job.job_id])
    assert delivery["version"] == 2
    assert delivery["sha256"] == v2_hash
    assert delivery["sha256"] != v1_hash


def test_two_revision_loops_keep_every_accepted_version_immutable(assembly_state, monkeypatch):
    """P9: two revision loops keep the accepted renders and their history intact."""
    from core.pipeline import approve_video, request_video_revision

    store, job = assembly_state["store"], assembly_state["job"]
    first = _render_v(assembly_state, _dispatch(assembly_state),
                      _mp4(assembly_state["tmp_path"] / "v1.mp4", 600).read_bytes())
    assert first.status == JobStatus.VIDEO_PENDING_APPROVAL
    v1 = first.video_versions[0]
    v1_bytes = (store.root / job.job_id / v1.path).read_bytes()

    request_video_revision(store, first, "regenerate", "reviewer")
    second = _render_v(assembly_state, _dispatch(assembly_state),
                       _mp4(assembly_state["tmp_path"] / "v2.mp4", 700).read_bytes())

    request_video_revision(store, second, "regenerate", "reviewer")
    third = _render_v(assembly_state, _dispatch(assembly_state),
                      _mp4(assembly_state["tmp_path"] / "v3.mp4", 800).read_bytes())

    # Every accepted render is still on disk with its own hash, and none of them was
    # accepted without a reviewer: only the last one carries the approval.
    assert [v.version for v in third.video_versions] == [1, 2, 3]
    assert third.video_versions[0].sha256 == hashlib.sha256(v1_bytes).hexdigest()
    assert (store.root / job.job_id / v1.path).read_bytes() == v1_bytes
    assert [v.approved for v in third.video_versions] == [False, False, False]
    assert third.active_video_version == 3

    approve_video(store, third, "reviewer")
    final = store.jobs[job.job_id]
    assert final.active_video_version == 3
    assert final.status == JobStatus.READY
    assert (store.root / job.job_id / v1.path).read_bytes() == v1_bytes
    assert final.video_versions[0].approved is False, \
        "an old version must not inherit the approval given to the current one"


def test_a_stale_or_duplicate_approval_cannot_accept_a_superseded_version(
    assembly_state, monkeypatch
):
    """P9: a late approval targets the version that was reviewed, not the current one."""
    from core.pipeline import approve_video, request_video_revision

    store, job = assembly_state["store"], assembly_state["job"]
    first = _render_v(assembly_state, _dispatch(assembly_state),
                      _mp4(assembly_state["tmp_path"] / "v1.mp4", 600).read_bytes())
    v1_hash = first.video_versions[0].sha256
    request_video_revision(store, first, "regenerate", "reviewer")
    second = _render_v(assembly_state, _dispatch(assembly_state),
                       _mp4(assembly_state["tmp_path"] / "v2.mp4", 700).read_bytes())

    # The reviewer still looks at the v1 they were sent.
    with pytest.raises(ValueError, match="Stale video approval"):
        approve_video(store, second, "reviewer", expected_version=1)
    with pytest.raises(ValueError, match="Stale video approval"):
        approve_video(store, second, "reviewer", expected_sha256=v1_hash)
    assert second.active_video_version == 2
    assert not any(v.approved for v in second.video_versions)

    approve_video(store, second, "reviewer", expected_version=2,
                  expected_sha256=second.video_versions[1].sha256)
    # A duplicate approval after READY is refused instead of approving again.
    with pytest.raises(ValueError, match="Cannot approve video"):
        approve_video(store, store.jobs[job.job_id], "reviewer")


def test_a_late_result_of_a_superseded_attempt_cannot_overwrite_the_accepted_version(
    assembly_state, monkeypatch
):
    """P9: a straggling Worker result never replaces what was already accepted."""
    store, job = assembly_state["store"], assembly_state["job"]
    first = _dispatch(assembly_state)
    stale_task_id = first.assembly_task_id
    stale_key = first.video_engine_snapshot["submit_key"]

    # An approved input changes, so the next attempt is a different render: a new
    # version, a new submit key, and the previous attempt is no longer the current one.
    job.status = JobStatus.VIDEO_REVISION_REQUESTED
    job.script = dict(job.script, scenes=[
        {"prompt": "Shot", "text": "Approved but changed line", "duration": 3},
    ])
    second = _dispatch(assembly_state)
    assert second.assembly_task_id != stale_task_id
    assert second.video_engine_snapshot["submit_key"] != stale_key, \
        "a changed approved input must produce a new attempt key"

    accepted = _render_v(assembly_state, second,
                         _mp4(assembly_state["tmp_path"] / "v2.mp4", 700).read_bytes())
    assert accepted.active_video_version == 1
    accepted_bytes = (store.root / job.job_id / accepted.video_versions[0].path).read_bytes()

    with pytest.raises(HTTPException) as refusal:
        tasks_api.task_result(
            _result(job.job_id, stale_task_id,
                    _mp4(assembly_state["tmp_path"] / "late.mp4", 600).read_bytes(),
                    version=1), _Request())
    assert refusal.value.status_code == 409

    final = store.jobs[job.job_id]
    assert final.active_video_version == 1
    assert len(final.video_versions) == 1
    assert (store.root / job.job_id / final.video_versions[0].path).read_bytes() \
        == accepted_bytes, "a late result replaced the accepted artifact"


def test_a_superseded_attempt_is_not_offered_to_a_worker(assembly_state):
    """P5/P9: a node never spends an upstream render on an attempt that was replaced.

    The superseded task is still mapped to a version, so only the current attempt may
    be leased; the old one is dropped from the queue instead of being executed and
    refused later.
    """
    store, job = assembly_state["store"], assembly_state["job"]
    _register_worker(store)
    first = _dispatch(assembly_state)
    stale_task_id = first.assembly_task_id

    job.status = JobStatus.VIDEO_REVISION_REQUESTED
    job.script = dict(job.script, scenes=[
        {"prompt": "Shot", "text": "Approved but changed line", "duration": 3},
    ])
    second = _dispatch(assembly_state)
    current_task_id = second.assembly_task_id
    assert stale_task_id in second.assembly_task_ids, "the superseded attempt is still mapped"

    claimed = _claim()["task"]

    assert claimed is not None and claimed["task_id"] == current_task_id
    assert assembly_state["queue"].find(stale_task_id) is None, \
        "the superseded attempt stayed claimable after the revision"