"""Issue #82 -> reusable driver for the real media acceptance harness.

Nothing in the orchestration layer is stubbed here. Jobs are created through the
real FastAPI app, workers register by heartbeat, tasks travel through the real
``claim``/``renew``/``result`` endpoints and results are imported by the real CORE
boundaries. Only *generators and external providers* are replaced -> the LLM, the
ComfyUI compute provider, the TTS HTTP runtime, the FFmpeg binary and the
publishing platform -> which is exactly the substitution the acceptance contract
allows.

The driver is single-sourced on purpose: ``test_full_media_harness.py`` owns the
Issue #82 acceptance matrix and reuses these steps instead of re-implementing
the flow.
"""

from __future__ import annotations

import base64
import io
import json
import os
import platform
import shutil
import struct
import subprocess
import time
import wave
from pathlib import Path

from adapters.ffmpeg import FFmpegAdapter
from core.script_agent import ScriptAgent
from core.state import store, task_queue
from worker import role_executor

TEXT_NODE = "text-worker"
IMAGE_NODE = "image-worker"
VOICE_NODE = "voice-worker"
PUBLISHER_NODE = "publisher-worker"

WAIT_ATTEMPTS = 600
WAIT_DELAY = 0.025

#: Environment variable holding the evidence file consumed by the CI report.
EVIDENCE_ENV = "VERTEP_MEDIA_EVIDENCE"


# ---------------------------------------------------------------------------
# Generator/provider stand-ins (the only substitutions the contract allows)
# ---------------------------------------------------------------------------


def make_wav(duration: float = 0.5, rate: int = 22050) -> bytes:
    buffer = io.BytesIO()
    handle = wave.open(buffer, "wb")
    handle.setnchannels(1)
    handle.setsampwidth(2)
    handle.setframerate(rate)
    frames = b"".join(struct.pack("<h", 0) for _ in range(int(rate * duration)))
    handle.writeframes(frames)
    handle.close()
    return buffer.getvalue()


class StubHTTPResponse:
    """Minimal stand-in for an ``httpx`` response of a provider runtime."""

    headers = {"content-type": "application/json"}
    content = b""

    def __init__(self, value):
        self.value = value

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.value


def write_character(root, character_id: str, voice: dict | None = None) -> Path:
    directory = Path(root) / character_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "character.json").write_text(
        json.dumps({"name": "Test Char", "language": "uk"}), encoding="utf-8")
    (directory / "voice.json").write_text(json.dumps(voice or {
        "provider": "mock", "voice": "uk", "language": "uk",
        "model": "uk_male", "speed": 160,
    }), encoding="utf-8")
    (directory / "system_prompt.txt").write_text("Ти операційний.", encoding="utf-8")
    return directory


def make_png(width: int = 2, height: int = 2, rgb: tuple[int, int, int] = (32, 96, 64)) -> bytes:
    """A real, minimal PNG so CORE's signature/contract validation sees valid bytes."""
    import zlib

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (len(data).to_bytes(4, "big") + tag + data
                + zlib.crc32(tag + data).to_bytes(4, "big"))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", width.to_bytes(4, "big") + height.to_bytes(4, "big")
                    + bytes((8, 2, 0, 0, 0)))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


_SCRIPT_COUNTER = {"value": 0}
_RENDER_COUNTER = {"value": 0}
#: Scene count per Job; the fake LLM keeps the storyboard aligned with the script.
_SCENE_COUNT = {"value": 2}
_JOB_SCENES: dict[str, int] = {}


def reset_counters() -> None:
    _SCRIPT_COUNTER["value"] = 0
    _RENDER_COUNTER["value"] = 0
    _JOB_SCENES.clear()


def _next_render_marker() -> str:
    _RENDER_COUNTER["value"] += 1
    return f"render-{_RENDER_COUNTER['value']}"


def _script_stub(topic: str, scenes: int) -> dict:
    """Deterministic LLM output; a marker makes every regeneration distinguishable."""
    _SCRIPT_COUNTER["value"] += 1
    marker = f"[script-{_SCRIPT_COUNTER['value']}] "
    return {
        "title": f"{marker}{topic}",
        "description": "опис",
        "hashtags": ["#test"],
        "scenes": [
            {"prompt": f"{marker}prompt сцени {index}",
             "voiceover": f"{marker}voiceover сцени {index}",
             "duration": 2.0}
            for index in range(1, scenes + 1)
        ],
    }


def _storyboard_llm_stub(job_id: str, revision: str | None = None) -> dict:
    """Deterministic Ollama output for a storyboard task.

    This replaces only the external LLM runtime. The storyboard still travels
    queue → Text Worker claim → ``/api/tasks/result`` → ``StoryboardService``,
    so version approval and the image-preview gate are exercised for real.
    """
    scenes = _JOB_SCENES.get(job_id, _SCENE_COUNT["value"])
    marker = f"[sb-rev:{revision}] " if revision else ""
    return {
        "title": f"{marker}Storyboard for {job_id}",
        "description": "Storyboard description",
        "hashtags": ["#test"],
        "scenes": [
            {"prompt": f"{marker}Storyboard frame {index}",
             "video_prompt": f"{marker}Storyboard camera {index}",
             "voiceover": f"{marker}Storyboard narration {index}",
             "duration": 2.0}
            for index in range(1, scenes + 1)
        ],
    }


def install_generator_stubs(monkeypatch, *, scenes: int = 2, on_render=None) -> None:
    """Replace only generators/providers; the control plane stays real."""
    _SCENE_COUNT["value"] = scenes

    def _assemble(self, output, **kwargs):
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = bytearray(b"ftypmp42" + b"\x00" * 96)
        payload.extend(_next_render_marker().encode("utf-8")[:32].ljust(32, b"\x00"))
        output.write_bytes(bytes(payload))
        if on_render is not None:
            on_render(output)
        return output

    monkeypatch.setattr(FFmpegAdapter, "assemble", _assemble)
    monkeypatch.setattr(FFmpegAdapter, "probe",
                        lambda path: {"format": "mp4", "validated": "container"})
    from adapters.providers import get_providers
    from adapters.providers.video_engines import NativeVertepEngine
    get_providers().replace("video_engine", NativeVertepEngine())
    monkeypatch.setattr(ScriptAgent, "generate_script",
                        lambda self, topic, system_prompt="", character=None: _script_stub(
                            topic, scenes))
    # The worker's storyboard executor rejects a board whose duration misses the
    # target, so the target follows the scripted scene budget.
    monkeypatch.setenv("STORYBOARD_TARGET_DURATION", str(scenes * 2))
    monkeypatch.setenv("STORYBOARD_DURATION_TOLERANCE", "0.2")
    reset_counters()


# ---------------------------------------------------------------------------
# Fleet registration and Job creation
# ---------------------------------------------------------------------------


def heartbeat(client, node_name: str, **overrides):
    payload = {"node_name": node_name, "vram_mb": 4096}
    payload.update(overrides)
    return client.post("/api/workers/heartbeat", json=payload)


def register_fleet(client, *, characters_root, character_id: str,
                   scenes: int = 2, on_render=None, monkeypatch=None) -> None:
    """First Run->CORE: isolated storage, characters and the node fleet."""
    if monkeypatch is not None:
        monkeypatch.setenv("CHARACTERS_ROOT", str(characters_root))
        monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
        monkeypatch.setenv("DEMO_MODE", "true")
    write_character(characters_root, character_id)
    if monkeypatch is not None:
        install_generator_stubs(monkeypatch, scenes=scenes, on_render=on_render)
    heartbeat(client, TEXT_NODE, role="text", vram_mb=0, supported_tasks=["text"],
              capabilities=["text_generation"])
    heartbeat(client, IMAGE_NODE, supported_tasks=["image"],
              capabilities=["image_generation"])
    heartbeat(client, VOICE_NODE, supported_tasks=["voice"], role="voice", vram_mb=0,
              capabilities=["speech_synthesis"],
              voice_catalog={"voices": ["uk"], "models": ["uk_male"]})


def create_job(client, *, topic: str = "Harness topic",
               character_id: str = "harnesschar") -> str:
    response = client.post("/api/jobs", json={"topic": topic, "character_id": character_id})
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    _JOB_SCENES[job_id] = _SCENE_COUNT["value"]
    return job_id


def create_telegram_job(*, topic: str = "Telegram topic",
                        character_id: str = "harnesschar",
                        chat_id: str = "424242") -> str:
    """A Job owned by a Telegram chat, created by the real CORE create path."""
    job = store.create(topic=topic, character_id=character_id, priority=5,
                       source=f"telegram:{chat_id}")
    _JOB_SCENES[job.job_id] = _SCENE_COUNT["value"]
    from core.api.job_helpers import _prepare_and_dispatch
    _prepare_and_dispatch(job)
    return job.job_id


def register_publisher(client, *, node_name: str = PUBLISHER_NODE) -> None:
    heartbeat(client, node_name, role="publisher", vram_mb=0,
              supported_tasks=["publish"], capabilities=["publishing"])


# ---------------------------------------------------------------------------
# Real task-queue steps
# ---------------------------------------------------------------------------


def claim_task(client, job_id: str, node_name: str, *, task: str | None = None,
               attempts: int = WAIT_ATTEMPTS, **claim):
    """Claim through ``/api/tasks/claim``; workers are registered per task type."""
    last: dict = {}
    for _ in range(attempts):
        body = client.post("/api/tasks/claim",
                           json={"node_name": node_name, "vram_mb": 0, **claim}).json()
        candidate = body.get("task")
        last = body
        if not candidate:
            time.sleep(WAIT_DELAY)
            continue
        if candidate.get("job_id") != job_id:
            raise AssertionError(
                f"{node_name} claimed a foreign task {candidate.get('task_id')} "
                f"for job {candidate.get('job_id')} while waiting for {job_id}")
        if task and candidate.get("task") != task:
            raise AssertionError(
                f"{node_name} was offered {candidate.get('task')} for {job_id}, expected {task}")
        return candidate
    raise AssertionError(
        f"task {task or '*'} for {job_id} was never offered to {node_name}; "
        f"last claim response: {json.dumps(last, default=str)[:400]}; "
        f"queue depth={task_queue.depth()}")


def run_script_task(client, job_id: str, *, node_name: str = TEXT_NODE,
                    expect_revision: str | None = None) -> dict:
    """Text Worker generates the script; the revision must travel with the task."""
    heartbeat(client, node_name, role="text", vram_mb=0,
              capabilities=["text_generation"], supported_tasks=["text"])
    task = claim_task(client, job_id, node_name, task="script")
    if expect_revision is not None:
        assert task.get("revision") == expect_revision, (
            f"revision did not reach the Text Worker: {task.get('revision')!r}")
    [artifact] = role_executor.execute_role_task("text", task)
    response = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"], "node_name": node_name,
        "success": True, "artifacts": [artifact]})
    assert response.status_code == 200, response.text
    return task


def submit_image(client, job_id: str, *, node_name: str = IMAGE_NODE) -> dict:
    task = claim_task(client, job_id, node_name, task="image",
                      supported_tasks=["image"], capabilities=["image_generation"])
    ppm = b"P6\n2 2\n255\n" + bytes((0, 0, 255)) * 4
    response = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"], "node_name": node_name,
        "success": True, "filename": "scene.ppm",
        "image_base64": base64.b64encode(ppm).decode()})
    assert response.status_code == 200, response.text
    return task


def submit_tts(client, job_id: str, monkeypatch, *, node_name: str = VOICE_NODE) -> dict:
    task = claim_task(client, job_id, node_name, task="voice",
                      supported_tasks=["voice"], capabilities=["speech_synthesis"],
                      voice_catalog={"voices": ["uk"], "models": ["uk_male"]})
    audio = make_wav()
    monkeypatch.setattr(role_executor.httpx, "post",
                        lambda *args, **kwargs: StubHTTPResponse(
                            {"audio_base64": base64.b64encode(audio).decode(),
                             "mime_type": "audio/wav", "engine": "espeak-ng"}))
    [artifact] = role_executor.execute_role_task("voice", task)
    response = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"], "node_name": node_name,
        "success": True, "artifacts": [artifact]})
    assert response.status_code == 200, response.text
    return task


def run_storyboard_task(client, job_id: str, monkeypatch, *, node_name: str = TEXT_NODE,
                        expect_revision: str | None = None) -> dict:
    """Text Worker generates the storyboard through the real queue boundary.

    Only the Ollama HTTP runtime is replaced; the task is claimed by the node and
    its result is imported by ``StoryboardService.handle_result``.
    """
    heartbeat(client, node_name, role="text", vram_mb=0,
              capabilities=["text_generation"], supported_tasks=["text"])
    task = claim_task(client, job_id, node_name, task="storyboard")
    if expect_revision is not None:
        assert task.get("revision") == expect_revision, (
            f"revision did not reach the Text Worker: {task.get('revision')!r}")
    board = _storyboard_llm_stub(job_id, task.get("revision"))
    monkeypatch.setattr(
        role_executor.httpx, "post",
        lambda *args, **kwargs: StubHTTPResponse(
            {"response": json.dumps(board, ensure_ascii=False)}))
    [artifact] = role_executor.execute_role_task("text", task)
    response = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"], "node_name": node_name,
        "success": True, "artifacts": [artifact]})
    assert response.status_code == 200, response.text
    return task


def claim_storyboard_preview(client, job_id: str, *, node_name: str = IMAGE_NODE,
                             attempts: int = WAIT_ATTEMPTS) -> dict:
    for _ in range(attempts):
        body = client.post("/api/tasks/claim", json={
            "node_name": node_name, "vram_mb": 4096,
            "supported_tasks": ["image"], "capabilities": ["image_generation"]}).json()
        task = body.get("task")
        if not task or task.get("job_id") != job_id or task.get("kind") != "image_storyboard":
            time.sleep(WAIT_DELAY)
            continue
        return task
    raise AssertionError(f"no image_storyboard preview was offered to {node_name}")


def submit_storyboard_previews(client, job_id: str, *, count: int = 2,
                               node_name: str = IMAGE_NODE) -> list[dict]:
    """GPU Worker renders every scene preview of the active storyboard."""
    submitted: list[dict] = []
    for index in range(count):
        task = claim_storyboard_preview(client, job_id, node_name=node_name)
        png = make_png(rgb=(20 + index * 10, 90, 70))
        response = client.post("/api/tasks/result", json={
            "job_id": job_id, "task_id": task["task_id"], "node_name": node_name,
            "success": True, "filename": f"scene-{index}.png",
            "image_base64": base64.b64encode(png).decode()})
        assert response.status_code == 200, response.text
        submitted.append(task)
    return submitted


def active_storyboard(client, job_id: str) -> dict:
    boards = client.get(f"/api/jobs/{job_id}/storyboards").json()
    assert boards, f"job {job_id} has no storyboard"
    return boards[-1]


def approve_storyboard_previews(client, job_id: str) -> dict:
    """Approve the reviewed image version; a stale image_version must be refused."""
    board = active_storyboard(client, job_id)
    assert board["image_status"] == "ready", (
        f"previews are not ready: image_status={board['image_status']}")
    stale = client.post(f"/api/jobs/{job_id}/storyboards/images/approve", json={
        "version": board["version"], "actor": "harness",
        "image_version": board["image_version"] + 1})
    assert stale.status_code == 409, (
        f"a stale image_version must be refused, got {stale.status_code}")
    response = client.post(f"/api/jobs/{job_id}/storyboards/images/approve", json={
        "version": board["version"], "actor": "harness",
        "image_version": board["image_version"]})
    assert response.status_code == 200, response.text
    return active_storyboard(client, job_id)


def approve_storyboard(client, job_id: str, *, expect_image_version: int | None = None) -> dict:
    board = approve_storyboard_previews(client, job_id)
    if expect_image_version is not None:
        assert board["image_version"] == expect_image_version, (
            f"image version drifted: {board['image_version']} != {expect_image_version}")
    response = client.post(f"/api/jobs/{job_id}/storyboards/approve", json={
        "version": board["version"], "actor": "harness",
        "image_version": board["image_version"]})
    assert response.status_code == 200, response.text
    return board


def get_job(client, job_id: str) -> dict:
    response = client.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


def wait_for_status(client, job_id: str, *statuses: str, attempts: int = WAIT_ATTEMPTS) -> dict:
    wanted = set(statuses)
    for _ in range(attempts):
        job = get_job(client, job_id)
        if job["status"] in wanted:
            return job
        time.sleep(WAIT_DELAY)
    raise AssertionError(
        f"job {job_id} never reached {sorted(wanted)}; last status={job['status']}")


def approve_script(client, job_id: str, monkeypatch, *, revision: str | None = None) -> dict:
    """Script approval (first pass) or a script revision loop, then the real
    storyboard queue → Text Worker → GPU previews → image approval → approval."""
    if revision is not None:
        response = client.post(f"/api/jobs/{job_id}/script/revision",
                               json={"revision": revision, "actor": "harness"})
        assert response.status_code == 200, response.text
    wait_for_status(client, job_id, "SCRIPT_QUEUED")
    run_script_task(client, job_id, expect_revision=revision)
    wait_for_status(client, job_id, "SCRIPT_PENDING_APPROVAL")
    response = client.post(f"/api/jobs/{job_id}/script/approve", json={"actor": "harness"})
    assert response.status_code == 200, response.text
    wait_for_status(client, job_id, "STORYBOARD_QUEUED")
    run_storyboard_task(client, job_id, monkeypatch)
    wait_for_status(client, job_id, "STORYBOARD_PENDING_APPROVAL")
    submit_storyboard_previews(client, job_id, count=_JOB_SCENES.get(job_id, 2))
    return approve_storyboard(client, job_id)


def generate_assets(client, job_id: str, monkeypatch, scenes: int = 2) -> None:
    """GPU image generation and Voice narration through the real task queue."""
    wait_for_status(client, job_id, "ASSET_GENERATION", "TTS_GENERATING",
                    "VIDEO_GENERATION")
    for _ in range(scenes):
        submit_image(client, job_id)
    wait_for_status(client, job_id, "TTS_GENERATING", "VIDEO_GENERATION")
    for _ in range(scenes):
        submit_tts(client, job_id, monkeypatch)


def wait_for_video(client, job_id: str, *, min_version: int = 1,
                   attempts: int = WAIT_ATTEMPTS) -> dict:
    for _ in range(attempts):
        job = get_job(client, job_id)
        if job["status"] == "VIDEO_PENDING_APPROVAL" \
                and (job.get("active_video_version") or 0) >= min_version:
            return job
        time.sleep(WAIT_DELAY)
    raise AssertionError(
        f"job {job_id} never produced video v>={min_version}; "
        f"status={job['status']} version={job.get('active_video_version')}")


def approve_video(client, job_id: str, *, version: int | None = None) -> dict:
    job = get_job(client, job_id)
    expected = version if version is not None else job.get("active_video_version") or 1
    response = client.post(f"/api/jobs/{job_id}/video/approve",
                           json={"actor": "harness", "expected_video_version": expected})
    assert response.status_code == 200, response.text
    return wait_for_status(client, job_id, "READY")


def request_video_revision(client, job_id: str, revision: str) -> None:
    response = client.post(f"/api/jobs/{job_id}/video/revision",
                           json={"revision": revision, "actor": "harness"})
    assert response.status_code == 200, response.text


def regenerate_video(client, job_id: str, *, min_version: int) -> dict:
    """Pure re-render loop through the shared Web regeneration endpoint."""
    response = client.post(f"/api/jobs/{job_id}/video/regenerate",
                           json={"actor": "harness"})
    assert response.status_code == 200, response.text
    return wait_for_video(client, job_id, min_version=min_version)


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------


def claim_publish(client, job_id: str, *, node_name: str = PUBLISHER_NODE,
                  channel: str | None = None, attempts: int = WAIT_ATTEMPTS) -> dict:
    for _ in range(attempts):
        body = client.post("/api/tasks/claim",
                           json={"node_name": node_name, "vram_mb": 0}).json()
        task = body.get("task")
        if not task or task.get("job_id") != job_id or task.get("task") != "publish":
            time.sleep(WAIT_DELAY)
            continue
        if channel and task.get("channel") != channel:
            raise AssertionError(
                f"publish task for {task.get('channel')} offered while waiting for {channel}")
        return task
    raise AssertionError(f"publish task for {job_id} was never offered to {node_name}")


def sandbox_receipt(channel: str, task: dict, *, status: str = "PUBLISHED",
                    error: str | None = None) -> dict:
    """A publisher receipt correlated with the task's durable publish intent."""
    receipt = {"channel": channel, "status": status, "timestamp": time.time()}
    if status == "PUBLISHED":
        receipt.update({"id": f"mock-{channel}", "remote_id": f"mock-{channel}",
                        "url": f"https://example.invalid/{channel}/mock"})
    else:
        receipt["error"] = error or f"{channel} unavailable"
    intent = task.get("publish_intent") or {}
    if intent.get("video_version") is not None:
        receipt["video_version"] = intent["video_version"]
    if intent.get("video_sha256"):
        receipt["video_sha256"] = intent["video_sha256"]
    return receipt


def submit_publish_receipt(client, job_id: str, task: dict, receipt: dict, *,
                           node_name: str = PUBLISHER_NODE) -> str:
    payload = json.dumps(receipt, sort_keys=True).encode("utf-8")
    artifact = role_executor._artifact("publication.json", "publication_receipt", payload)
    response = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"], "node_name": node_name,
        "success": True, "artifacts": [artifact]})
    assert response.status_code == 200, response.text
    return task["task_id"]


# ---------------------------------------------------------------------------
# Persistence helpers and CI evidence
# ---------------------------------------------------------------------------


def restart_store():
    """Simulate a CORE restart: a fresh JobStore over the same persistent root."""
    from core.pipeline import JobStore
    from core.repository import FileRepository
    return JobStore(root=str(store.root), repository=FileRepository(store.root))


def job_root(job_id: str) -> Path:
    return store.root / job_id


def final_dir(job_id: str) -> Path:
    return job_root(job_id) / "final"


def version_paths(job_id: str) -> list[Path]:
    return sorted(final_dir(job_id).glob("video-v*.mp4"))


def artifact_hashes(paths) -> dict[str, str]:
    import hashlib
    digests: dict[str, str] = {}
    for path in paths:
        try:
            digests[str(path)] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            continue
    return digests


def video_evidence(job_id: str, job: dict) -> dict:
    """Immutable versions with their on-disk checksums -> the render artifact proof."""
    return {
        "job_id": job_id,
        "active_video_version": job.get("active_video_version"),
        "versions": [
            {"version": version.get("version"),
             "path": version.get("path"),
             "sha256": version.get("sha256"),
             "approved": version.get("approved"),
             "approved_by": version.get("approved_by")}
            for version in job.get("video_versions") or []
        ],
        "files": artifact_hashes(version_paths(job_id)),
    }


def run_provenance() -> dict:
    """Identify the exact run: SHA, version and the configuration in effect.

    Issue #82 requires the CI report to be anchored to a concrete main SHA and to
    name the configuration it ran with. Nothing here is fabricated: the SHA comes
    from git, the version from ``VERSION`` and the config from the provider
    substitution matrix that is really installed for this run.
    """
    from adapters.providers import provider_matrix

    root = Path(__file__).resolve().parents[1]
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                             text=True, timeout=30).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        sha = None
    version = (root / "VERSION").read_text(encoding="utf-8").strip() \
        if (root / "VERSION").is_file() else None
    return {"sha": sha, "version": version,
            "dirty": bool(sha and subprocess.run(
                ["git", "status", "--porcelain"], cwd=root, capture_output=True,
                text=True, timeout=30).stdout.strip()),
            "python": platform.python_version(), "platform": platform.system(),
            "config": {"backends": provider_matrix()}}


def _archive_artifacts(payload, archive_root: Path) -> None:
    """Copy the rendered versions next to the evidence so it stays verifiable.

    The Job storage root is a disposable temporary directory, so an evidence file
    that only points at it cannot be re-checked after the run. The copies keep
    their recorded sha256, which ``scripts/media-acceptance-report.py`` re-verifies.
    """
    video = payload.get("video") if isinstance(payload, dict) else None
    files = video.get("files") if isinstance(video, dict) else None
    if not files:
        return
    job_id = str(video.get("job_id") or "job")
    for name in list(files):
        source = Path(name)
        if not source.is_file():
            continue
        copy = archive_root / job_id / source.name
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, copy)
        files[str(copy)] = files.pop(name)


def record_evidence(name: str, payload) -> None:
    """Append one evidence record for ``scripts/media-acceptance-report.py``."""
    target = os.environ.get(EVIDENCE_ENV)
    if not target:
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    _archive_artifacts(payload, path.parent / "artifacts")
    document = {}
    if path.is_file():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            document = {}
    document[name] = dict(payload, provenance=run_provenance()) \
        if isinstance(payload, dict) else payload
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def record_row(name: str, expected, actual, **payload) -> None:
    """Record one acceptance row with the expected/actual pair the issue asks for.

    The equality is asserted here as well as in the test body, so a row can never
    be reported as proven when the observed value differs from the agreed one.
    """
    assert actual == expected, f"{name}: expected {expected!r}, got {actual!r}"
    record_evidence(name, {"expected": expected, "actual": actual, **payload})


def drain_executor() -> None:
    """Wait until the CORE background executor is idle."""
    from core.app import executor
    for _ in range(2):
        executor.submit(lambda: None).result(timeout=30)
