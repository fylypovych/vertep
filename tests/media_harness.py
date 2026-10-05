"""Issue #82 вЂ” reusable driver for the real media acceptance harness.

Nothing in the orchestration layer is stubbed here. Jobs are created through the
real FastAPI app, workers register by heartbeat, tasks travel through the real
``claim``/``renew``/``result`` endpoints and results are imported by the real CORE
boundaries. Only *generators and external providers* are replaced вЂ” the LLM, the
ComfyUI compute provider, the TTS HTTP runtime, the FFmpeg binary and the
publishing platform вЂ” which is exactly the substitution the acceptance contract
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
import struct
import time
import wave
from pathlib import Path

from adapters.ffmpeg import FFmpegAdapter
from core.models import JobStatus, StoryboardScene, StoryboardVersion
from core.script_agent import ScriptAgent
from core.state import store
from core.storyboard import StoryboardService
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
    (directory / "system_prompt.txt").write_text("РўРё РІРµРґСѓС‡РёР№.", encoding="utf-8")
    return directory


_SCRIPT_COUNTER = {"value": 0}
_RENDER_COUNTER = {"value": 0}


def reset_counters() -> None:
    _SCRIPT_COUNTER["value"] = 0
    _RENDER_COUNTER["value"] = 0


def _next_render_marker() -> str:
    _RENDER_COUNTER["value"] += 1
    return f"render-{_RENDER_COUNTER['value']}"


def _script_stub(topic: str, scenes: int) -> dict:
    """Deterministic LLM output; a marker makes every regeneration distinguishable."""
    _SCRIPT_COUNTER["value"] += 1
    marker = f"[script-{_SCRIPT_COUNTER['value']}] "
    return {
        "title": f"{marker}{topic}",
        "description": "РѕРїРёСЃ",
        "hashtags": ["#test"],
        "scenes": [
            {"prompt": f"{marker}РєР°РґСЂ {index}",
             "voiceover": f"{marker}РѕР·РІСѓС‡РєР° {index}",
             "duration": 2.0}
            for index in range(1, scenes + 1)
        ],
    }


def _storyboard_stub(self, job, revision: str | None = None):
    script = job.script or {"title": job.topic, "scenes": []}
    scenes = []
    source = script.get("scenes") or [{"prompt": job.topic, "voiceover": "", "duration": 1}]
    for index, item in enumerate(source, 1):
        scenes.append(StoryboardScene(
            index=index, prompt=item.get("prompt", ""),
            video_prompt=item.get("prompt", ""),
            voiceover=item.get("voiceover", ""),
            duration=float(item.get("duration", 1))))
    version = (job.storyboards[-1].version + 1 if job.storyboards else 1)
    board = StoryboardVersion(
        version=version, title=script.get("title", job.topic),
        description=script.get("description", ""),
        hashtags=script.get("hashtags", []), scenes=scenes,
        status="pending_approval", image_status="approved", image_version=1)
    for scene in board.scenes:
        scene.scene_id = f"sb-{version}-{scene.index}"
        scene.image_prompt = scene.prompt
        scene.image_version = 1
        scene.image_artifact_id = f"mock-art-{scene.index}"
        scene.artifact_id = scene.image_artifact_id
    job.storyboards.append(board)
    job.active_storyboard_version = version
    job.active_image_version = board.image_version
    job.storyboard_error = None
    self.store.update(job, JobStatus.STORYBOARD_PENDING_APPROVAL,
                      f"STORYBOARD {version} PENDING APPROVAL")
    return board


def install_generator_stubs(monkeypatch, *, scenes: int = 2, on_render=None) -> None:
    """Replace only generators/providers; the control plane stays real."""

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
    monkeypatch.setattr(StoryboardService, "queue", _storyboard_stub)
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
    """First Runв†’CORE: isolated storage, characters and the node fleet."""
    if monkeypatch is not None:
        monkeypatch.setenv("CHARACTERS_ROOT", str(characters_root))
        monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
        monkeypatch.setenv("DEMO_MODE", "true")
    write_character(characters_root, character_id)
    if monkeypatch is not None:
        install_generator_stubs(monkeypatch, scenes=scenes, on_render=on_render)
    heartbeat(client, IMAGE_NODE, supported_tasks=["image"],
              capabilities=["image_generation"])
    heartbeat(client, VOICE_NODE, supported_tasks=["voice"], role="voice", vram_mb=0,
              capabilities=["speech_synthesis"],
              voice_catalog={"voices": ["uk"], "models": ["uk_male"]})


def create_job(client, *, topic: str = "Harness topic",
               character_id: str = "harnesschar") -> str:
    response = client.post("/api/jobs", json={"topic": topic, "character_id": character_id})
    assert response.status_code == 200, response.text
    return response.json()["job_id"]


def create_telegram_job(*, topic: str = "Telegram topic",
                        character_id: str = "harnesschar",
                        chat_id: str = "424242") -> str:
    """A Job owned by a Telegram chat, created by the real CORE create path."""
    job = store.create(topic=topic, character_id=character_id, priority=5,
                       source=f"telegram:{chat_id}")
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
    for _ in range(attempts):
        body = client.post("/api/tasks/claim",
                           json={"node_name": node_name, "vram_mb": 0, **claim}).json()
        candidate = body.get("task")
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
    raise AssertionError(f"task {task or '*'} for {job_id} was never offered to {node_name}")


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


def approve_script(client, job_id: str, *, revision: str | None = None) -> None:
    """Script approval (first pass) or a script revision loop, then storyboard approval."""
    if revision is not None:
        response = client.post(f"/api/jobs/{job_id}/script/revision",
                               json={"revision": revision, "actor": "harness"})
        assert response.status_code == 200, response.text
    wait_for_status(client, job_id, "SCRIPT_QUEUED")
    run_script_task(client, job_id, expect_revision=revision)
    wait_for_status(client, job_id, "SCRIPT_PENDING_APPROVAL")
    response = client.post(f"/api/jobs/{job_id}/script/approve", json={"actor": "harness"})
    assert response.status_code == 200, response.text
    wait_for_status(client, job_id, "STORYBOARD_PENDING_APPROVAL")
    boards = client.get(f"/api/jobs/{job_id}/storyboards").json()
    version = boards[-1]["version"] if boards else 1
    response = client.post(f"/api/jobs/{job_id}/storyboards/approve",
                           json={"version": version, "actor": "harness"})
    assert response.status_code == 200, response.text


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
    """Immutable versions with their on-disk checksums вЂ” the render artifact proof."""
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


def record_evidence(name: str, payload) -> None:
    """Append one evidence record for ``scripts/media-acceptance-report.py``."""
    target = os.environ.get(EVIDENCE_ENV)
    if not target:
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {}
    if path.is_file():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            document = {}
    document[name] = payload
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def drain_executor() -> None:
    """Wait until the CORE background executor is idle."""
    from core.app import executor
    for _ in range(2):
        executor.submit(lambda: None).result(timeout=30)
