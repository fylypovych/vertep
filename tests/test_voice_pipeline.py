"""Integration coverage for Issue i.0.0.0.6 — TTS pipeline on the Voice Worker.

Verifies that:

1. The character voice configuration (provider / voice / language / model /
   speed) flows into the dispatched voice task delivered to the Voice Worker.
2. The Voice Worker ``execute_role_task`` produces a real audio artifact that
   carries a verifiable ``audio_contract`` (provider / voice / sha256 ...).
3. CORE persists and validates that contract as a sidecar artifact, rejecting a
   tampered payload (sha256 mismatch).

The Text/Storyboard → Voice Worker → audio → downstream video-pipeline chain is
exercised end-to-end by ``tests/test_features.py``; this module focuses on the
voice contract seam added for this issue.
"""

import hashlib
import json
import math
import struct
import time
import os
import base64
import io
import wave
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from adapters.ffmpeg import FFmpegAdapter
from core.app import app, store
from core.dispatcher import available_worker
from core.models import Job, JobStatus, StoryboardScene, StoryboardVersion, utc_now
from core.script_agent import ScriptAgent
from core.storyboard import StoryboardService

from worker import role_executor
from core.api.job_helpers import _persist_tts_contract, _tts_task_for
from core.orchestration import initialize_plan


def _complete_script_task(client, job_id, node_name="text-worker"):
    """Simulate a Text Worker claiming, executing, and submitting a script task."""
    client.post("/api/workers/heartbeat", json={"node_name": node_name, "vram_mb": 0,
                 "role": "text",
                 "capabilities": ["text_generation"], "supported_tasks": ["text"]})
    task = None
    for _ in range(100):
        resp = client.post("/api/tasks/claim", json={"node_name": node_name, "vram_mb": 0})
        task = resp.json().get("task")
        if task and task.get("task") == "script" and task.get("job_id") == job_id:
            break
        time.sleep(0.01)
    assert task and task.get("task") == "script"
    [artifact] = role_executor.execute_role_task("text", task)
    response = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"], "node_name": node_name,
        "success": True, "artifacts": [artifact]})
    assert response.status_code == 200


def _write_character(root: Path, character_id: str) -> Path:
    directory = root / character_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "character.json").write_text(
        json.dumps({"name": "Voice Char", "language": "uk"}), encoding="utf-8")
    (directory / "voice.json").write_text(json.dumps({
        "provider": "mock", "voice": "uk", "language": "uk",
        "model": "uk_male", "speed": 160}), encoding="utf-8")
    (directory / "system_prompt.txt").write_text("Ти ведучий.", encoding="utf-8")
    return directory


def _new_job_with_voiceover(character_id: str):
    job = store.create(topic="привіт світе", character_id=character_id, priority=1)
    job.script = {"title": "Привіт", "scenes": [
        {"prompt": "кадр", "voiceover": "озвучка одна", "duration": 1}]}
    initialize_plan(job)
    store.save_worker({"node_name": "voice-worker", "role": "voice",
                       "capabilities": ["speech_synthesis"], "status": "ONLINE",
                       "last_seen": "2026-01-01T00:00:00+00:00"})
    return job, store.jobs[job.job_id].scenes[0]


class _Response:
    headers = {"content-type": "application/json"}
    content = b""

    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


def test_character_voice_config_reaches_voice_task(monkeypatch, tmp_path):
    monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path))
    character_id = "voicechar"
    _write_character(tmp_path, character_id)
    job, scene = _new_job_with_voiceover(character_id)
    task = _tts_task_for(job, scene)
    assert task["task"] == "voice"
    assert task["character_id"] == character_id
    assert task["provider"] == "mock"
    assert task["voice"] == "uk"
    assert task["language"] == "uk"
    assert task["model"] == "uk_male"
    assert task["speed"] == 160
    assert task["topic"] == "озвучка одна"


def test_worker_audio_contract_is_validated_and_persisted_by_core(
        monkeypatch, tmp_path):
    monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path))
    character_id = "voicechar"
    _write_character(tmp_path, character_id)
    job, scene = _new_job_with_voiceover(character_id)
    task = _tts_task_for(job, scene)

    # Voice Worker produces the artifact + contract from the character config.
    audio = b"RIFF\x0c\x00\x00\x00WAVE\x00\x00\x00\x00"
    monkeypatch.setattr(role_executor.httpx, "post", lambda *args, **kwargs:
                        _Response({"audio_base64": __import__("base64").b64encode(audio).decode(),
                                   "mime_type": "audio/wav", "engine": "espeak-ng"}))
    [artifact] = role_executor.execute_role_task("voice", task)
    data = __import__("base64").b64decode(artifact["data_base64"])
    assert data == audio
    contract = artifact["contract"]

    # CORE validates + persists the contract as a sidecar artifact.
    result = SimpleNamespace(task_id="task-1", node_name="voice-worker")
    audio_path = store.root / job.job_id / "audio" / "voice-scene-001.wav"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(data)
    created = _persist_tts_contract(store, job, scene, result, audio_path, data, contract)
    assert len(created) == 1 and created[0].kind == "audio_contract"
    sidecar = audio_path.with_suffix(".contract.json")
    assert sidecar.exists()
    written = json.loads(sidecar.read_text(encoding="utf-8"))
    assert written["provider"] == "mock"
    assert written["voice"] == "uk"
    assert written["sha256"] == hashlib.sha256(audio).hexdigest()


def test_core_rejects_tampered_audio_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path))
    character_id = "voicechar"
    _write_character(tmp_path, character_id)
    job, scene = _new_job_with_voiceover(character_id)
    audio = b"RIFF\x0c\x00\x00\x00WAVE\x00\x00\x00\x00"
    contract = {"provider": "mock", "voice": "uk",
                "sha256": hashlib.sha256(b"different").hexdigest()}
    result = SimpleNamespace(task_id="task-1", node_name="voice-worker")
    audio_path = store.root / job.job_id / "audio" / "voice-scene-001.wav"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(audio)
    try:
        _persist_tts_contract(store, job, scene, result, audio_path, audio, contract)
    except ValueError as error:
        assert "sha256" in str(error)
    else:
        raise AssertionError("tampered audio contract was accepted")
def _voice_worker(name: str, catalog: dict) -> dict:
    return {
        "node_name": name,
        "last_seen": datetime.now(timezone.utc).isoformat(),
        "status": "FREE",
        "capabilities": ["speech_synthesis"],
        "supported_tasks": ["voice"],
        "voice_catalog": catalog,
        "vram_mb": 0,
    }


def test_dispatcher_selects_voice_worker_by_catalog():
    job = Job(job_id="2026-000001", topic="t", character_id="c", priority=1,
              status=JobStatus.NEW, created_at=utc_now())
    uk = _voice_worker("uk-voice", {"voices": ["uk"], "models": ["uk_male"]})
    en = _voice_worker("en-voice", {"voices": ["en"], "models": ["en_female"]})
    wildcard = _voice_worker("any-voice", {"voices": ["*"]})
    chosen = available_worker([uk, en], job, task_type="voice",
                              voice_requirements={"voice": "uk", "model": "uk_male"})
    assert chosen and chosen["node_name"] == "uk-voice"
    assert available_worker([uk, en], job, task_type="voice",
                            voice_requirements={"voice": "de", "model": "de_female"}) is None
    assert available_worker([wildcard], job, task_type="voice",
                            voice_requirements={"voice": "de"})["node_name"] == "any-voice"
    bare = _voice_worker("bare-voice", {})
    chosen = available_worker([bare], job, task_type="voice",
                              voice_requirements={"voice": "uk"})
    assert chosen and chosen["node_name"] == "bare-voice"


def _make_wav(duration: float = 0.5, rate: int = 22050) -> bytes:
    buffer = io.BytesIO()
    w = wave.open(buffer, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    frames = []
    samples = int(rate * duration)
    for i in range(samples):
        frames.append(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate))))
    w.writeframes(b"".join(frames))
    w.close()
    return buffer.getvalue()
def _heartbeat(client, node_name, **overrides):
    payload = {"node_name": node_name, "vram_mb": 4096}
    payload.update(overrides)
    response = client.post("/api/workers/heartbeat", json=payload)
    assert response.status_code == 200


def _wait_for(client, job_id, statuses=("READY", "FAILED")):
    job = None
    for _ in range(400):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.025)
    return job


def _approve_script(client, job_id):
    local_fallback = os.getenv("LOCAL_WORKER_FALLBACK", "true").lower() == "true"
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if not local_fallback and job["status"] == "SCRIPT_QUEUED":
            _complete_script_task(client, job_id)
        if job["status"] == "SCRIPT_PENDING_APPROVAL":
            break
        time.sleep(0.025)
    assert job["status"] == "SCRIPT_PENDING_APPROVAL"
    response = client.post(f"/api/jobs/{job_id}/script/approve", json={"actor": "test"})
    assert response.status_code == 200
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "STORYBOARD_PENDING_APPROVAL":
            break
        time.sleep(0.025)
    assert job["status"] == "STORYBOARD_PENDING_APPROVAL"
    storyboards = client.get(f"/api/jobs/{job_id}/storyboards").json()
    version = storyboards[-1]["version"] if storyboards else 1
    response = client.post(f"/api/jobs/{job_id}/storyboards/approve", json={"version": version, "actor": "test"})
    assert response.status_code == 200
    return version


def _fake_script(self, topic, system_prompt="", character=None):
    return {"title": topic, "description": "опис", "hashtags": ["#t"],
            "scenes": [{"prompt": "кадр", "voiceover": "озвучка персонажа", "duration": 1}]}


def _fake_storyboard_queue(self, job, revision=None):
    script = job.script or {"title": job.topic, "scenes": []}
    scenes = []
    source = script.get("scenes") or [{"prompt": job.topic, "voiceover": "", "duration": 1}]
    for index, item in enumerate(source, 1):
        scenes.append(StoryboardScene(
            index=index, prompt=item.get("prompt", ""), video_prompt=item.get("prompt", ""),
            voiceover=item.get("voiceover", item.get("text", "")),
            duration=float(item.get("duration", 1)),
        ))
    version = (job.storyboards[-1].version + 1 if job.storyboards else 1)
    storyboard = StoryboardVersion(
        version=version, title=script.get("title", job.topic), description=script.get("description", ""),
        hashtags=script.get("hashtags", []), scenes=scenes, status="pending_approval",
        image_status="approved", image_version=1,
    )
    for scene in storyboard.scenes:
        scene.scene_id = f"sb-{version}-{scene.index}"
        scene.image_prompt = scene.prompt
        scene.image_version = 1
        scene.image_artifact_id = f"mock-art-{scene.index}"
        scene.artifact_id = scene.image_artifact_id
    job.storyboards.append(storyboard)
    job.active_storyboard_version = version
    job.active_image_version = storyboard.image_version
    job.storyboard_error = None
    self.store.update(job, JobStatus.STORYBOARD_PENDING_APPROVAL, f"STORYBOARD {version} PENDING APPROVAL")
    return storyboard


def test_full_voice_dispatch_end_to_end(monkeypatch, tmp_path):
    """Text/Storyboard → Voice Worker → audio(contract) → video pipeline."""
    monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path))
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("DEMO_MODE", "true")
    _write_character(tmp_path, "voicechar")
    monkeypatch.setattr(ScriptAgent, "generate_script", _fake_script)
    monkeypatch.setattr(StoryboardService, "queue", _fake_storyboard_queue)

    def _fake_assemble(self, output, **kwargs):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"ftypmp42" + b"\x00" * 100)
        return output

    def _fake_probe(self, path):
        return {"format": "mp4", "validated": "container"}

    monkeypatch.setattr(FFmpegAdapter, "assemble", _fake_assemble)
    monkeypatch.setattr(FFmpegAdapter, "probe", _fake_probe)
    # Pin the final render to the native engine so the assembly uses the mocked
    # FFmpegAdapter (and never a leaked remote engine left in the module-level
    # providers registry by an earlier test in the suite — those engines call
    # httpx.post, which is temporarily patched below and would break status_code).
    from adapters.providers import get_providers
    from adapters.providers.video_engines import NativeVertepEngine
    get_providers().replace("video_engine", NativeVertepEngine())

    client = TestClient(app)
    _heartbeat(client, "image-worker", supported_tasks=["image"],
               capabilities=["image_generation"])
    _heartbeat(client, "voice-worker", supported_tasks=["voice"], role="voice",
               capabilities=["speech_synthesis"], voice_catalog={"voices": ["uk"], "models": ["uk_male"]})

    job_id = client.post("/api/jobs", json={"topic": "Voice e2e", "character_id": "voicechar"}).json()["job_id"]
    _approve_script(client, job_id)

    # Image worker generates the storyboard scene.
    image_task = None
    for _ in range(300):
        body = client.post("/api/tasks/claim", json={"node_name": "image-worker", "vram_mb": 4096}).json()
        image_task = body.get("task")
        if image_task and image_task.get("job_id") == job_id:
            break
        time.sleep(0.02)
    assert image_task and image_task.get("task") == "image"
    ppm = b"P6\n2 2\n255\n" + bytes((0, 0, 255)) * 4
    image_result = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": image_task["task_id"], "node_name": "image-worker",
        "success": True, "filename": "scene.ppm", "image_base64": base64.b64encode(ppm).decode()})
    assert image_result.status_code == 200

    # Voice Worker claims the TTS task; the character voice config reached it.
    claim_body = {"node_name": "voice-worker", "vram_mb": 0, "supported_tasks": ["voice"],
                  "capabilities": ["speech_synthesis"],
                  "voice_catalog": {"voices": ["uk"], "models": ["uk_male"]}}
    tts_task = None
    for _ in range(300):
        tts_task = client.post("/api/tasks/claim", json=claim_body).json().get("task")
        if tts_task and tts_task.get("job_id") == job_id:
            break
        time.sleep(0.02)
    assert tts_task
    assert tts_task.get("task") == "voice"
    assert tts_task.get("provider") == "mock"
    assert tts_task.get("voice") == "uk"
    assert tts_task.get("model") == "uk_male"

    # The real Voice Worker executor produces audio + a verifiable contract.
    wav = _make_wav()
    monkeypatch.setattr(role_executor.httpx, "post", lambda *a, **k: _Response(
        {"audio_base64": base64.b64encode(wav).decode(), "mime_type": "audio/wav", "engine": "espeak-ng"}))
    [artifact] = role_executor.execute_role_task("voice", tts_task)
    assert artifact["contract"]["provider"] == "mock"
    assert artifact["contract"]["voice"] == "uk"
    assert artifact["contract"]["sha256"] == hashlib.sha256(wav).hexdigest()

    tts_result = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": tts_task["task_id"], "node_name": "voice-worker",
        "success": True, "artifacts": [artifact]})
    assert tts_result.status_code == 200

    completed = _wait_for(client, job_id)
    if completed["status"] != "READY":
        import sys
        print(f"JOB FAILED: {json.dumps(completed, ensure_ascii=False)}", file=sys.stderr)
        pytest.fail(f"job did not reach READY: {json.dumps(completed, ensure_ascii=False)}")
    audio = [item for item in completed["artifacts"] if item["kind"] == "audio"]
    contracts = [item for item in completed["artifacts"] if item["kind"] == "audio_contract"]
    assert audio and contracts
    audio_path = Path(store.root) / job_id / "audio" / audio[0]["filename"]
    assert audio_path.exists()
    assert audio_path.read_bytes() == wav
    contract_path = audio_path.with_suffix(".contract.json")
    assert contract_path.exists()
    assert json.loads(contract_path.read_text(encoding="utf-8"))["sha256"] == hashlib.sha256(wav).hexdigest()